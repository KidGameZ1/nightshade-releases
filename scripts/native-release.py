#!/usr/bin/env python3
"""Native packaging/QA from compiled inputs; never downloads the source checkout."""
import hashlib, json, os, platform, re, shutil, subprocess, sys, tarfile, urllib.request, zipfile
from pathlib import Path

REPOSITORY = 'KidGameZ1/nightshade-releases'
phase, version, target = sys.argv[1:4]
input_sha = sys.argv[4] if len(sys.argv) > 4 else ''
if phase not in ('package', 'verify') or target not in ('windows-x64', 'linux-x64', 'macos-arm64'):
    raise SystemExit('Invalid phase/platform')
if not all(part.isdigit() for part in version.split('.')) or len(version.split('.')) != 3:
    raise SystemExit('Invalid release version')
host = 'windows' if os.name == 'nt' else 'macos' if sys.platform == 'darwin' else 'linux'
architecture = 'arm64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x64'
if target != host+'-'+architecture:
    raise SystemExit('This runner does not match the requested native platform')
root = Path.cwd().resolve()
output = root/'output'; evidence = output/'evidence'; downloads = root/'downloads'
for directory in (evidence, downloads): directory.mkdir(parents=True, exist_ok=True)
java = Path(os.environ['JAVA_HOME'])/'bin'/('java.exe' if os.name == 'nt' else 'java')

def digest(path):
    with path.open('rb') as source: return hashlib.file_digest(source, 'sha256').hexdigest()

def run(command, name, env=None, cwd=None, timeout=1800):
    print(name, flush=True)
    with (evidence/(name+'.log')).open('w', encoding='utf-8') as log:
        completed = subprocess.run([str(arg) for arg in command], stdout=log, stderr=subprocess.STDOUT,
            env=env, cwd=cwd or root, timeout=timeout)
    print((evidence/(name+'.log')).read_text(errors='replace')[-6000:], flush=True)
    completed.check_returncode()

def fetch(pattern):
    run(['gh', 'release', 'download', 'v'+version, '--repo', REPOSITORY, '--pattern', pattern,
         '--dir', downloads, '--clobber'], 'download-'+pattern.replace('*','all').replace('/','_'))

def reconstruct_inputs(inputs):
    transport_path = inputs/'transport.json'
    if not transport_path.exists():
        return
    transport = json.loads(transport_path.read_text())
    manifest = json.loads((inputs/'input-manifest.json').read_text())
    if transport.get('formatVersion') != 1 or not str(transport.get('baseRun', '')).isdigit():
        raise RuntimeError('Invalid compact input transport')
    artifact = transport.get('baseArtifact', '')
    if not re.fullmatch(r'Nightshade-[0-9.]+-linux-x64-package', artifact):
        raise RuntimeError('Invalid baseline artifact name')
    expected = {}
    for entry in manifest['files']:
        name = entry['path']
        if name.startswith('/') or '\\' in name or ':' in name or any(part in ('', '.', '..') for part in name.split('/')):
            raise RuntimeError('Unsafe input inventory path')
        if name in expected or not re.fullmatch(r'[a-f0-9]{64}', entry['sha256']) or entry['bytes'] < 0:
            raise RuntimeError('Invalid input inventory')
        expected[name] = entry
    def identity(name, entry):
        return (Path(name).name, entry['bytes'], entry['sha256'])
    candidates = {}
    for name, entry in expected.items():
        file = inputs/name
        if file.exists():
            if file.is_symlink() or not file.is_file() or file.stat().st_size != entry['bytes'] or digest(file) != entry['sha256']:
                raise RuntimeError('Compact payload differs: '+name)
            candidates[identity(name, entry)] = file
    missing = {identity(name, entry) for name, entry in expected.items()} - set(candidates)
    scratch = root/'compact-baseline'
    scratch.mkdir(exist_ok=True)
    run(['gh', 'run', 'download', str(transport['baseRun']), '--repo', REPOSITORY,
         '--name', artifact, '--dir', scratch/'artifact'], 'download-compact-baseline')
    archives = list((scratch/'artifact').rglob('Nightshade-Studios-*-linux-x64.tar.gz'))
    if len(archives) != 1:
        raise RuntimeError('Expected exactly one baseline Linux suite')
    # Read only regular files that can satisfy the inventory. Never extract archive paths or links.
    wanted = {(name, size) for name, size, sha in missing}
    with tarfile.open(archives[0], 'r:gz') as archive:
        for member in archive:
            name = Path(member.name).name
            if not member.isfile() or (name, member.size) not in wanted:
                continue
            pending = scratch/'candidate.tmp'
            with archive.extractfile(member) as source, pending.open('wb') as destination:
                shutil.copyfileobj(source, destination)
            key = (name, member.size, digest(pending))
            if key in missing:
                destination = scratch/(key[2]+'-'+name)
                pending.replace(destination)
                candidates[key] = destination
            elif pending.exists():
                pending.unlink()
    for name, entry in expected.items():
        key = identity(name, entry)
        if key in candidates:
            continue
        filename = Path(name).name
        url = transport.get('maven', {}).get(filename)
        native = re.fullmatch(r'imgui-java-natives-(macos|windows)-([0-9.]+)\.jar', filename)
        if not native:
            raise RuntimeError('Baseline lacks exact required input: '+name)
        artifact_id = 'imgui-java-natives-'+native.group(1)
        allowed = 'https://repo.maven.apache.org/maven2/io/github/spair/'+artifact_id+'/'+native.group(2)+'/'+filename
        if url != allowed:
            raise RuntimeError('Unapproved native dependency URL')
        destination = scratch/filename
        with urllib.request.urlopen(url, timeout=120) as source, destination.open('wb') as output_stream:
            shutil.copyfileobj(source, output_stream)
        if destination.stat().st_size != entry['bytes'] or digest(destination) != entry['sha256']:
            raise RuntimeError('Maven native dependency differs from frozen input: '+filename)
        candidates[key] = destination
    for name, entry in expected.items():
        destination = inputs/name
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidates[identity(name, entry)], destination)
        if destination.stat().st_size != entry['bytes'] or digest(destination) != entry['sha256']:
            raise RuntimeError('Reconstructed input differs: '+name)
    actual = {file.relative_to(inputs).as_posix() for file in inputs.rglob('*') if file.is_file()}
    if actual != set(expected) | {'input-manifest.json', 'transport.json'}:
        raise RuntimeError('Unexpected reconstructed input inventory')
    transport_path.unlink()
    print('Reconstructed all '+str(len(expected))+' exact input files from compact transport', flush=True)

def validate_archives(directory):
    manifests = list(directory.glob('*-manifest.json'))
    if len(manifests) != 1: raise RuntimeError('Expected one platform manifest')
    metadata = json.loads(manifests[0].read_text())
    if metadata['version'] != version or metadata['platform']+'-'+metadata['architecture'] != target:
        raise RuntimeError('Wrong release identity')
    for item in metadata['artifacts']:
        file = directory/item['file']
        if file.stat().st_size != item['bytes'] or digest(file) != item['sha256']:
            raise RuntimeError('Artifact hash/size mismatch: '+str(file))
        if file.suffix == '.zip':
            with zipfile.ZipFile(file) as archive:
                if archive.testzip() is not None: raise RuntimeError('ZIP CRC mismatch')
        elif file.name.endswith('.tar.gz'):
            with tarfile.open(file, 'r:gz') as archive:
                for member in archive:
                    stream = archive.extractfile(member) if member.isfile() else None
                    if stream:
                        while stream.read(1024*1024): pass
    sums = directory/f'Nightshade-Studios-{version}-{target}-SHA256SUMS.txt'
    for line in sums.read_text().splitlines():
        expected, filename = line.split('  ', 1)
        if digest(directory/filename) != expected: raise RuntimeError('Published checksum differs: '+filename)
    return manifests[0]

if phase == 'package':
    if len(input_sha) != 64: raise RuntimeError('The expected compiled-input SHA-256 is required')
    fetch('Nightshade-'+version+'-binary-inputs.zip')
    archive = downloads/('Nightshade-'+version+'-binary-inputs.zip')
    if digest(archive) != input_sha: raise RuntimeError('Compiled packaging inputs differ')
    inputs = root/'inputs'
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None: raise RuntimeError('Compiled inputs ZIP is corrupt')
        bundle.extractall(inputs)
    reconstruct_inputs(inputs)
    run([java, '-cp', str(inputs/'editor/lib/*'), 'com.nightshade.tooling.BinaryReleaseInputs',
         'package', inputs, output/'installers'], 'native-package')
    platform_files = output/'installers'/target.split('-')[0]
    validate_archives(platform_files)
    run(['gh','release','upload','v'+version,'--repo',REPOSITORY,'--clobber',
         *sorted(platform_files.iterdir())], 'upload-draft-packages', timeout=2700)
    (evidence/'package.json').write_text(json.dumps({'version':version,'platform':target,
        'inputSha256':input_sha,'passed':True,'runner':platform.platform()}, indent=2))
else:
    fetch('*'+version+'-'+target+'*')
    fetch('catalog.json'); fetch('catalog.sig')
    manifest = validate_archives(downloads)
    suite_root = root/'extracted'; suite_root.mkdir(exist_ok=True)
    base = 'Nightshade-Studios-'+version+'-'+target
    if os.name == 'nt':
        with zipfile.ZipFile(downloads/(base+'.zip')) as archive: archive.extractall(suite_root)
    elif target.startswith('macos'):
        run(['/usr/bin/ditto', '-x', '-k', downloads/(base+'.zip'), suite_root], 'extract-suite')
        run(['/usr/bin/hdiutil', 'verify', downloads/(base+'.dmg')], 'verify-dmg')
    else:
        run(['tar', '-xzf', downloads/(base+'.tar.gz'), '-C', suite_root], 'extract-suite')
    suite = suite_root/'Nightshade Studios'
    def app_paths(name):
        if target.startswith('macos'):
            app = suite/('Nightshade '+name+'.app')
            return app/'Contents/runtime/Contents/Home/bin/java', app/'Contents/app/*'
        app = suite/('Nightshade '+name)
        if os.name == 'nt': return app/'runtime/bin/java.exe', app/'app/*'
        return app/'lib/runtime/bin/java', app/'lib/app/*'
    editor_java, editor_cp = app_paths('Editor'); hub_java, hub_cp = app_paths('Hub')
    if target.startswith('macos'):
        for name in ('Hub', 'Editor'):
            run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2',
                 suite/('Nightshade '+name+'.app')], 'verify-signature-'+name.lower())
    for jar in suite.rglob('*.jar'):
        if 'framework' in jar.name and 'sources' in jar.name: raise RuntimeError('Framework sources were distributed')
    run([editor_java, '-cp', str(editor_cp), 'com.nightshade.tooling.TutorialVerification', evidence/'tutorials'],
        'tutorials', timeout=1800)
    for language in ('shade','flow','java','nsplugins'):
        log = (evidence/'tutorials'/language/'native.log').read_text(errors='replace')
        if 'NIGHTSHADE_SMOKE_OK' not in log or (not target.startswith('macos') and 'llvmpipe' not in log.lower()):
            raise RuntimeError('Actual expected OpenGL rendering was not verified: '+language)
    scratch = root/'native-update'; home = scratch/'home'; home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ('JAVA_HOME','JDK_HOME','JDK_JAVA_OPTIONS','_JAVA_OPTIONS'): env.pop(key, None)
    env['JAVA_TOOL_OPTIONS'] = '-Duser.home='+home.as_posix()
    try:
        run([hub_java, '-cp', str(hub_cp), 'com.nightshade.hub.update.HubUpdateVerification', suite,
             downloads/'catalog.json', downloads/'catalog.sig', downloads, scratch], 'native-update', env=env, cwd=scratch, timeout=360)
    finally:
        for file in scratch.rglob('*'):
            if file.is_file() and (file.suffix == '.log' or file.name in ('journal.json','job.json','verification.txt')):
                destination = evidence/'native-update'/file.relative_to(scratch)
                destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(file, destination)
    receipt = {'formatVersion':1, 'version':version, 'platform':target, 'passed':True,
        'runner':platform.platform(), 'manifestSha256':digest(manifest),
        'checks':['tutorial-gameplay','relocated-tutorial-exports','packaged-hub-editor','signed-hub-update','managed-editor-installation']}
    (evidence/(base+'-verification.json')).write_text(json.dumps(receipt, indent=2)+'\n')
    print('PASS native release verification: '+target, flush=True)
