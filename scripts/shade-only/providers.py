#!/usr/bin/env python3
"""Stage explicitly public provider sources, or build/verify them on a native runner."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
import schema

PUBLIC = {
    "README.md", "sdk/protocol.md", "tools/build.py", "tools/package.py", "tools/documentation.py", "tools/verify_exports.py", "tests/test_providers.py",
    "java/lib/gson-2.13.2.jar", "java/src/nightshade/provider/Worker.java",
    "java/src/nightshade/sdk/Behavior.java", "java/src/nightshade/sdk/Context.java", "java/src/nightshade/sdk/Entity.java", "java/src/nightshade/sdk/Input.java",
    "python/worker.py", "python/nightshade_sdk/__init__.py", "cpp/worker.cpp", "cpp/include/nightshade/sdk.hpp",
    "csharp/Nightshade.Language.CSharp.csproj", "csharp/Program.cs", "csharp/Sdk.cs",
    "lua/worker.lua", "lua/compile.lua", "lua/nightshade_sdk.lua", "lua/json.lua",
    "vendor/json.hpp", "vendor/json.lua", "vendor/json.lua.LICENSE", "vendor/lua-5.5.1.tar.gz", "vendor/manifest.json",
    "shade/Documentation.shade", "shade/LanguageBuild.shade", "shade/LanguageSession.shade", "shade/ProviderInstallation.shade",
    "licenses/Apache-2.0.txt", "licenses/CPython-LICENSE.txt", "licenses/Lua-5.5.1-readme.html", "licenses/dotnet-LICENSE.txt", "licenses/dotnet-ThirdPartyNotices.txt", "licenses/json-lua-MIT.txt", "licenses/nlohmann-json-MIT.txt",
}
PUBLIC |= {language + "/ns-language.json" for language in schema.LANGUAGES}
PUBLIC |= {"examples/" + language + "/Spin" + suffix for language, suffix in {"java":".java","python":".py","cpp":".hpp","csharp":".cs","lua":".lua"}.items()}


def validate(root):
    metadata = schema.json_read(root / "public-provider-inputs.json")
    schema.require(metadata.get("formatVersion") == 1 and metadata.get("kind") == "nightshade.public-language-inputs", "Invalid public-provider input format")
    files = schema.inventory(metadata.get("files"))
    expected = {"language-plugins/" + name for name in PUBLIC}
    schema.require(set(files) == expected, "Public provider input contains unexpected or missing files")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    schema.require(actual == expected | {"public-provider-inputs.json"}, "Non-allowlisted files in public provider input")
    for name, item in files.items():
        path = root / name
        schema.require(path.is_file() and not path.is_symlink() and path.stat().st_size == item["bytes"] and schema.digest(path) == item["sha256"], "Public provider payload differs: " + name)
    return metadata


def stage(args):
    schema.require(not args.output.exists(), "Choose a fresh public provider input filename")
    entries=[]
    with tempfile.TemporaryDirectory(prefix="nightshade-public-providers-") as temporary:
        root=Path(temporary)
        for name in sorted(PUBLIC):
            source=args.sources / name
            schema.require(source.is_file() and not source.is_symlink(), "Missing or linked public provider file: " + name)
            relative="language-plugins/"+name
            destination=root/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,destination)
            entries.append({"path":relative,"bytes":destination.stat().st_size,"sha256":schema.digest(destination)})
        schema.json_write(root/"public-provider-inputs.json",{"formatVersion":1,"kind":"nightshade.public-language-inputs","privateCoreSourceIncluded":False,"files":entries})
        validate(root)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(args.output,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():archive.write(path,path.relative_to(root).as_posix())
        shutil.copy2(root/"public-provider-inputs.json",args.output.with_suffix(".inventory.json"))
    sha=schema.digest(args.output)
    args.output.with_suffix(".zip.sha256").write_text(sha+"  "+args.output.name+"\n")
    return {"archive":str(args.output),"sha256":sha,"files":len(entries),"privateCoreSourceIncluded":False}


def windows_compilers():
    """Use LLVM's native MSVC target; Lua alone uses the installed MinGW C tools."""
    vswhere=Path(os.environ.get("ProgramFiles(x86)","C:/Program Files (x86)"))/"Microsoft Visual Studio/Installer/vswhere.exe"
    installation=subprocess.check_output([str(vswhere),"-latest","-products","*","-requires","Microsoft.VisualStudio.Component.VC.Tools.x86.x64","-property","installationPath"],text=True).strip()
    script=Path(installation)/"Common7/Tools/VsDevCmd.bat"
    schema.require(script.is_file(),"Windows C++ verification requires the Visual Studio x64 SDK")
    with tempfile.TemporaryDirectory(prefix="nightshade-msvc-") as temporary:
        wrapper=Path(temporary)/"configure.cmd"
        wrapper.write_text('@call "'+str(script)+'" -no_logo -arch=x64 -host_arch=x64 >nul\n@if errorlevel 1 exit /b 1\n@set\n')
        result=subprocess.run([os.environ.get("COMSPEC","cmd.exe"),"/d","/c","configure.cmd"],cwd=temporary,check=True,text=True,capture_output=True)
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith("="):
            key,value=line.split("=",1);os.environ[key]=value
    llvm=Path(os.environ.get("ProgramFiles","C:/Program Files"))/"LLVM/bin"
    schema.require((llvm/"clang++.exe").is_file(),"LLVM clang++ is required")
    extra=[str(llvm)]
    if os.environ.get("MSYS2_LOCATION"):
        msys=Path(os.environ["MSYS2_LOCATION"])
        extra += [str(msys/"mingw64/bin"),str(msys/"usr/bin")]
    os.environ["PATH"]=os.pathsep.join([*extra,os.environ.get("PATH","")])
    os.environ["CXX"]=str(llvm/"clang++.exe")


def run(args):
    schema.require(schema.current_platform()==args.platform,"Provider build must run on the actual target architecture")
    schema.require(not args.output.exists(),"Choose a fresh provider output folder")
    args.output.mkdir(parents=True);work=args.output/".work"
    schema.extract(args.inputs,work,args.sha256);validate(work)
    if os.name=="nt":windows_compilers()
    root=work/"language-plugins";cache=args.output/".cache";cache.mkdir()
    os.environ["NIGHTSHADE_TEST_CACHE"]=str(cache.resolve())
    def command(name,argv,timeout=1200):
        with (args.output/(name+".log")).open("wb") as output:
            subprocess.run([str(value) for value in argv],cwd=root,stdout=output,stderr=subprocess.STDOUT,check=True,timeout=timeout)
    command("provider-tests",[sys.executable,"-m","unittest","discover","-s","tests","-p","test_providers.py","-v"])
    command("runtime-relocation",[sys.executable,"tools/verify_exports.py","--evidence",args.output/"runtime-evidence","--cache",cache])
    verified=schema.json_read(args.output/"runtime-evidence/verification.json")
    schema.require(verified.get("passed") is True and verified.get("platform")==args.platform and len(verified.get("providers",[]))==5,"Five native provider relocation checks are required")
    archives=args.output/"packages";archives.mkdir();packages=[]
    for language in schema.LANGUAGES:
        manifest=schema.json_read(root/language/"ns-language.json")
        name="Nightshade-"+language.title()+"-"+schema.version(manifest["version"])+"-"+args.platform+".nslanguage"
        archive=archives/name
        command(language+"-package",[sys.executable,"tools/package.py","--install-provider",language,"--output",work/("installed-"+language),"--cache",cache,"--archive",archive])
        schema.provider(archive,args.platform)
        packages.append({"language":language,"name":name,"bytes":archive.stat().st_size,"sha256":schema.digest(archive)})
        shutil.rmtree(work/("installed-"+language))
    receipt={"formatVersion":1,"kind":"nightshade.public-provider-verification","platform":args.platform,"actualPlatform":schema.current_platform(),"machine":platform.machine(),"inputSha256":args.sha256,"privateCoreSourceIncluded":False,"passed":True,"checks":["real-language-compilation","independent-instances","source-errors","lifecycle-cleanup","empty-path-relocation","archive-inventory"],"packages":packages,"published":False}
    schema.json_write(args.output/"provider-verification.json",receipt)
    shutil.rmtree(work);shutil.rmtree(cache)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__);modes=parser.add_subparsers(dest="mode",required=True)
    staging=modes.add_parser("stage");staging.add_argument("--sources",type=Path,required=True);staging.add_argument("--output",type=Path,required=True)
    running=modes.add_parser("run")
    for name in ("inputs","output"):running.add_argument("--"+name,type=Path,required=True)
    running.add_argument("--sha256",required=True);running.add_argument("--platform",choices=schema.PLATFORMS,required=True)
    args=parser.parse_args()
    for key in ("sources","inputs","output"):
        if hasattr(args,key):setattr(args,key,getattr(args,key).resolve())
    import json
    print(json.dumps(stage(args) if args.mode=="stage" else run(args),indent=2))


if __name__=="__main__":main()
