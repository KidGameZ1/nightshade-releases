#!/usr/bin/env python3
"""Exercise shipped JVM-free Shade tools on their native OS, with no Java on PATH."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess


def verify(binaries, output, version, target, graphical=True):
    host = 'windows' if os.name == 'nt' else 'macos' if platform.system() == 'Darwin' else 'linux'
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'amd64': 'x64', 'x86_64': 'x64'}.get(platform.machine().lower())
    if target != host+'-'+str(arch):
        raise RuntimeError('Native Shade verification requires the exact target OS/architecture')
    binaries, output = Path(binaries).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    suffix = '.exe' if host == 'windows' else ''
    cli, player = binaries/('shade'+suffix), binaries/('shade-player'+suffix)
    for binary in (cli, player):
        if not binary.is_file():
            raise RuntimeError('Missing shipped native binary: '+str(binary))
    env = os.environ.copy()
    # Windows CI installs its pinned Mesa driver system-wide. Clearing PATH
    # leaves the Windows system DLL search intact without copying OS libraries.
    for key in ('JAVA_HOME', 'JDK_HOME', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'PYTHONPATH'):
        env.pop(key, None)
    # Absolute native executable paths work with no Java/Python command discoverable.
    env['PATH'] = ''
    unrelated = output/'Unrelated Working Directory'
    unrelated.mkdir(exist_ok=True)

    def run(args, name):
        result = subprocess.run([str(a) for a in args], cwd=unrelated, env=env,
                                capture_output=True, text=True, timeout=120)
        (output/(name+'.log')).write_text(result.stdout+result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(name+' failed: '+(result.stdout+result.stderr)[-6000:])
        return result.stdout

    source = output/'Independent.shade'
    source.write_text('type Independent { number main() { var total=0; for(var i=0;i<10;i++){total+=i;} console.log("native-cli-ok"); return total; } }\n')
    program = output/'Independent.shadebin'
    run([cli, 'build', source, '--output', program, '--json'], 'shade-build')
    source.unlink()
    result = json.loads(run([cli, 'run', program, '--json'], 'shade-run'))
    if (result.get('success') is not True or 'native-cli-ok' not in result.get('logs', [])
            or result.get('result') != {'kind':'Number','value':45.0}):
        raise RuntimeError('Standalone compiled Shade did not execute correctly')
    project = output/'Authored Native Game'
    (project/'assets').mkdir(parents=True, exist_ok=True)
    (project/'scenes').mkdir(exist_ok=True)
    (project/'project.nshade').write_text(json.dumps({'formatVersion':1, 'name':'Native Release Check',
        'runtimeTarget':'shade-native', 'logicalWidth':960, 'logicalHeight':540, 'startScene':'scenes/Main.nsscene'}))
    (project/'assets/Spin.shade').write_text('behavior Spin { number speed=90; number elapsed=0; on update(number dt){elapsed+=dt; entity.rotateY(speed*dt);} }\n')
    (project/'assets/Spin.shade.nsmeta').write_text('spin')
    entities = [{'id':'cube-'+str(i), 'name':'Cube '+str(i), 'kind':'cube',
        'transform':{'position':{'x':x,'y':0,'z':0}}, 'color':{'r':r,'g':g,'b':b,'a':1},
        'behaviors':[{'id':'spin-'+str(i),'assetId':'spin','overrides':{'speed':speed}}]}
        for i,x,r,g,b,speed in [(0,-1.4,.65,.3,.95,90),(1,1.4,.15,.75,.8,-45)]]
    entities.append({'id':'hud','name':'HUD','kind':'text','text':'Native Shade - no Java',
                     'transform':{'position':{'x':24,'y':20,'z':0}}})
    (project/'scenes/Main.nsscene').write_text(json.dumps({'formatVersion':1,'id':'scene','name':'Native','dimension':'3D','entities':entities}))
    run([player, '--project', project, '--preflight'], 'player-preflight')
    run([player, '--project', project, '--compile-output', project/'native'], 'player-compile')
    (project/'assets/Spin.shade').unlink()
    relocated = output/'Relocated Native Game With Spaces'
    if relocated.exists():
        shutil.rmtree(relocated)
    relocated.mkdir()
    shutil.move(str(project), relocated/'project')
    moved_player = relocated/('Native Game'+suffix)
    shutil.copy2(player, moved_player)
    if host != 'windows':
        moved_player.chmod(moved_player.stat().st_mode | 0o111)
    report = output/'native-state.json'
    run([moved_player, '--headless', '--frames', 60, '--dt', 1/60, '--report', report], 'player-headless')

    def check_state(path):
        state = json.loads(path.read_text())
        rotations = [e['transform']['rotation']['y'] for e in state['scene']['entities'][:2]]
        if state.get('javaRequired') is not False or state.get('frames') != 60 or any(abs(a-b)>1e-8 for a,b in zip(rotations,[90,-45])):
            raise RuntimeError('Native compiled game did not preserve independent 90/-45 overrides')
    check_state(report)
    checks = ['native-shade-cli', 'native-shade-relocated-runtime']
    if graphical:
        screenshot, graphic_report = output/'native-player.png', output/'native-graphics-state.json'
        run([moved_player, '--frames', 60, '--dt', 1/60, '--screenshot', screenshot, '--report', graphic_report], 'player-graphics')
        check_state(graphic_report)
        if not screenshot.is_file() or screenshot.stat().st_size < 4096 or screenshot.read_bytes()[:8] != b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('Native graphics smoke did not capture a rendered frame')
        checks.append('native-shade-graphics')
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {'formatVersion':1,'version':version,'platform':target,'passed':True,'javaRequired':False,
               'checks':checks,'binaries':{p.name:digest(p) for p in (cli,player)}}
    (output/'native-shade-verification.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--binaries',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--platform',required=True)
    parser.add_argument('--headless-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(verify(args.binaries,args.output,args.version,args.platform,not args.headless_only),indent=2))
