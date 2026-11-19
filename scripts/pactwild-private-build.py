"""Generic native packaging orchestration. Payloads remain on an unpublished draft."""
import hashlib,json,os,re,shutil,subprocess,zipfile
from pathlib import Path
repo=os.environ['GITHUB_REPOSITORY'];tag=os.environ['DRAFT_TAG'];digest=os.environ['INPUT_SHA256']
assert re.fullmatch(r'pactwild-private-[a-zA-Z0-9-]+',tag)
assert re.fullmatch(r'[a-f0-9]{64}',digest)
def draft():
    release=json.loads(subprocess.check_output(['gh','release','view',tag,'-R',repo,'--json','isDraft']))
    assert release['isDraft'],'Refusing to use a published release'
draft()
base=Path('private-work').resolve();base.mkdir();evidence=base/'evidence';evidence.mkdir()
def run(args,name,**kwargs):
    with (evidence/(name+'.log')).open('w',encoding='utf-8') as f:
        result=subprocess.run([str(x) for x in args],stdout=f,stderr=subprocess.STDOUT,timeout=600,**kwargs)
    if result.returncode:raise RuntimeError(name+' failed; private log retained')
try:
    run(['gh','release','download',tag,'-R',repo,'--pattern','inputs.zip','--dir',base],'download')
    archive=base/'inputs.zip';assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in z.namelist())
        z.extractall(base/'frozen')
    frozen=base/'frozen';java=Path(os.environ['JAVA_HOME'])/'bin/java.exe'
    cp=';'.join(str(p) for p in [frozen/'runner/*',frozen/'input/lib/*',frozen/'input/nightshade-player.jar',frozen/'input/game/compiled/behaviors.jar'])
    run([java,'-cp',cp,'com.nightshade.tooling.FrozenGameExport','package',frozen,base/'native'],'package')
    relocated=base/'Relocated Game';shutil.move(str(base/'native/Pactwild'),relocated)
    # Remove the original staging location from the execution path.
    (frozen/'input').rename(frozen/'input-offline')
    game=relocated/'app/game';bundled=relocated/'runtime/bin/java.exe'
    cp=';'.join(str(p) for p in [frozen/'runner/verification.jar',relocated/'app/lib/*',game/'compiled/behaviors.jar'])
    run([bundled,'-Dnightshade.headless=true','-cp',cp,'pactwild.tests.PactwildVerification',game],'rules')
    shutil.copy2(game/'build/verification/rules.json',evidence/'rules.json')
    run([bundled,'-cp',cp,'pactwild.tests.PactwildGraphics',game,evidence/'graphics'],'graphics')
    env=dict(os.environ);env['JAVA_TOOL_OPTIONS']='-Dnightshade.smokeFrames=90 -Dnightshade.capture='+str(evidence/'launcher.png').replace('\\','/')
    run([relocated/'Pactwild.exe'],'native-launcher',env=env)
    assert (evidence/'launcher.png').is_file(),'Native executable did not render'
    # Test output is evidence, not part of the shipped game.
    shutil.rmtree(game/'build')
    artifact=base/'Pactwild-windows-x64.zip'
    with zipfile.ZipFile(artifact,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(relocated.rglob('*')):
            if p.is_file():z.write(p,Path('Pactwild')/p.relative_to(relocated))
    with zipfile.ZipFile(artifact) as z:assert z.testzip() is None
    manifest={'platform':'windows-x64','passed':True,'input_sha256':digest,'archive':artifact.name,'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),'bytes':artifact.stat().st_size,'native_runner':os.environ.get('ImageOS'),'relocated_execution':True}
    (evidence/'delivery.json').write_text(json.dumps(manifest,indent=2))
    (base/'Pactwild-windows-x64.sha256').write_text(manifest['sha256']+'  '+artifact.name+'\n')
    draft();run(['gh','release','upload',tag,'-R',repo,artifact,base/'Pactwild-windows-x64.sha256'],'upload-game')
finally:
    evidence_zip=base/'windows-evidence.zip'
    with zipfile.ZipFile(evidence_zip,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(evidence.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(evidence))
    draft();subprocess.run(['gh','release','upload',tag,'-R',repo,str(evidence_zip),'--clobber'],check=True)
