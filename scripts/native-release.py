#!/usr/bin/env python3
"""Native packaging/QA from compiled inputs; never downloads the source checkout."""
import hashlib, json, os, platform, shutil, subprocess, sys, tarfile, zipfile
from pathlib import Path

REPOSITORY = 'KidGameZ1/nightshade-releases'
phase, version, target = sys.argv[1:4]
input_sha = sys.argv[4] if len(sys.argv) > 4 else ''
if phase not in ('package', 'verify') or target not in ('windows-x64', 'linux-x64'):
    raise SystemExit('Invalid phase/platform')
if not all(part.isdigit() for part in version.split('.')) or len(version.split('.')) != 3:
    raise SystemExit('Invalid release version')
if (os.name == 'nt') != target.startswith('windows'):
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
    run([java, '-cp', str(inputs/'editor/lib/*'), 'com.nightshade.tooling.BinaryReleaseInputs',
         'package', inputs, output/'installers'], 'native-package')
    validate_archives(output/'installers'/target.split('-')[0])
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
    else:
        run(['tar', '-xzf', downloads/(base+'.tar.gz'), '-C', suite_root], 'extract-suite')
    suite = suite_root/'Nightshade Studios'
    def app_paths(name):
        app = suite/('Nightshade '+name)
        if os.name == 'nt': return app/'runtime/bin/java.exe', app/'app/*'
        return app/'lib/runtime/bin/java', app/'lib/app/*'
    editor_java, editor_cp = app_paths('Editor'); hub_java, hub_cp = app_paths('Hub')
    for jar in suite.rglob('*.jar'):
        if 'framework' in jar.name and 'sources' in jar.name: raise RuntimeError('Framework sources were distributed')
    run([editor_java, '-cp', str(editor_cp), 'com.nightshade.tooling.TutorialVerification', evidence/'tutorials'],
        'tutorials', timeout=1800)
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
