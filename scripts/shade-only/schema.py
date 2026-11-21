"""Strict, source-private transport rules for the new Shade application release."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import struct
import zipfile

PLATFORMS = ("macos-arm64", "windows-x64", "linux-x64")
LANGUAGES = ("java", "python", "cpp", "csharp", "lua")
PROGRAMS = {"hub": "nightshade.hub.Hub", "editor": "nightshade.editor.Editor", "player": "nightshade.player.GamePlayer", "build-worker": "nightshade.tools.GameBuildWorker"}
REQUIRED_CHECKS = {"hub-project-workflow", "editor-authoring-workflow", "flow-shared-runtime", "core-export-relocation", "five-language-export"}
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_FILES = 50000


class InvalidInput(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidInput(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def current_platform():
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system(), "unsupported")
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower(), platform.machine().lower())
    return system + "-" + architecture


def version(value):
    require(isinstance(value, str) and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-dev(?:\.[0-9]+)?)?", value), "Invalid release version")
    return value


def relative(value):
    require(isinstance(value, str) and value and "\\" not in value and ":" not in value and "\x00" not in value, "Invalid relative path")
    require(not value.startswith("/") and all(p not in {"", ".", ".."} for p in value.split("/")), "Unsafe relative path: " + value)
    return value


def json_read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def archive_members(archive):
    entries = archive.infolist()
    require(len(entries) <= MAX_FILES, "Archive has too many files")
    names = set()
    total = 0
    for entry in entries:
        require(not entry.is_dir(), "Transport archives must contain ordinary files only")
        name = relative(entry.filename)
        require(name.casefold() not in names, "Duplicate or case-colliding archive path: " + name)
        names.add(name.casefold())
        require(not stat.S_ISLNK(entry.external_attr >> 16), "Symbolic links are forbidden in release inputs")
        require(not entry.flag_bits & 1, "Encrypted release inputs are unsupported")
        total += entry.file_size
        require(total <= MAX_ARCHIVE_BYTES, "Archive expands beyond the release input limit")
    return entries


def extract(archive_path, destination, expected_sha=None):
    destination = Path(destination)
    require(not destination.exists(), "Refusing to overwrite extraction destination: " + str(destination))
    if expected_sha is not None:
        require(re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""), "Expected SHA-256 is required")
        require(digest(archive_path) == expected_sha, "Release input SHA-256 differs")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive_members(archive)
        require(archive.testzip() is None, "Corrupt release archive")
        destination.mkdir(parents=True)
        for entry in entries:
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("xb") as output:
                while data := source.read(1024 * 1024):
                    output.write(data)
            target.chmod(0o755 if entry.external_attr >> 16 & 0o111 else 0o644)


def native_architecture(path):
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if len(header) >= 8 and header[:4] == b"\xcf\xfa\xed\xfe":
            machine = struct.unpack_from("<I", header, 4)[0]
            return "macos-arm64" if machine == 0x0100000C else "unsupported-macho"
        if len(header) >= 20 and header[:4] == b"\x7fELF" and header[4:6] == b"\x02\x01":
            return "linux-x64" if struct.unpack_from("<H", header, 18)[0] == 62 else "unsupported-elf"
        if len(header) >= 64 and header[:2] == b"MZ":
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            pe = stream.read(6)
            return "windows-x64" if pe[:4] == b"PE\x00\x00" and pe[4:] == b"\x64\x86" else "unsupported-pe"
    return "unrecognized"


def check_identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", value), "Invalid native QA identifier")
    return value


def inventory(items, key="path"):
    require(isinstance(items, list) and len(items) <= MAX_FILES, "Invalid inventory")
    result = {}
    folded = set()
    for item in items:
        require(isinstance(item, dict), "Invalid inventory entry")
        name = relative(item.get(key))
        require(name.casefold() not in folded, "Duplicate or case-colliding inventory path: " + name)
        require(isinstance(item.get("bytes"), int) and item["bytes"] >= 0, "Invalid inventory byte count")
        require(isinstance(item.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]), "Invalid inventory checksum")
        folded.add(name.casefold())
        result[name] = item
    return result


def allowed_input_path(name, target):
    suffix = ".exe" if target == "windows-x64" else ""
    if name in {"native/shade" + suffix, "native/shade-desktop" + suffix}:
        return "native"
    if name in {"programs/" + item + ".shadebin" for item in PROGRAMS}:
        return "program"
    if name in {"branding/hub.png", "branding/hub-transparent.png", "branding/studio.png", "branding/studio-transparent.png", "branding/nightshade.icns", "branding/nightshade.ico"}:
        return "branding"
    if re.fullmatch(r"notices/[a-zA-Z0-9_.-]+\.(txt|md)", name):
        return "notice"
    if name == "qa/checks.json" or re.fullmatch(r"qa/[a-z0-9-]+\.shadebin", name):
        return "verification"
    if name.startswith("qa/fixtures/") and PurePosixPath(name).suffix in {".json", ".nshade", ".nsscene", ".nsmeta", ".nstype", ".nsflow", ".png"}:
        return "public-fixture"
    if re.fullmatch(r"providers/Nightshade-[A-Za-z]+-[0-9.]+-" + re.escape(target) + r"\.nslanguage", name):
        return "optional-provider"
    return None


def provider(path, target):
    """Public worker/SDK source is explicit; core implementation source is forbidden."""
    with zipfile.ZipFile(path) as archive:
        entries = archive_members(archive)
        require(archive.testzip() is None, "Corrupt optional provider archive")
        meta = json.loads(archive.read("ns-language.json"))
        manifest = json.loads(archive.read("package-manifest.json"))
        language = meta.get("language")
        require(meta.get("formatVersion") == 1 and manifest.get("formatVersion") == 1, "Unsupported provider manifest format")
        require(language in LANGUAGES and meta.get("id") == "com.nightshade.language." + language, "Unexpected optional provider identity")
        require(meta.get("optional") is True and meta.get("platform") == target and meta.get("sdkApiVersion") == 1, "Incompatible optional provider package")
        require(manifest.get("platform") == target and manifest.get("id") == meta["id"], "Provider inventory identity differs")
        require(manifest.get("version") == meta.get("version") and str(path).endswith("-" + version(meta.get("version")) + "-" + target + ".nslanguage"), "Provider version/name differs")
        require(meta.get("protocol", {}).get("name") == "nightshade.language" and meta["protocol"].get("version") == 1, "Provider worker protocol differs")
        listed = inventory(manifest["files"])
        require(set(listed) | {"package-manifest.json"} == {entry.filename for entry in entries}, "Unexpected or missing provider payload")
        for name, item in listed.items():
            relative(name)
            require(public_provider_path(language, name), "Provider contains non-allowlisted implementation source or payload: " + name)
            data = archive.read(name)
            require(len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == item["sha256"], "Provider payload hash differs: " + name)
        return meta


def public_provider_path(language, name):
    if name in {"ns-language.json", "PROTOCOL.md", "nightshade-java-provider.jar", "gson-2.13.2.jar", "Nightshade.Language.CSharp.dll", "Nightshade.Language.CSharp.pdb", "Nightshade.Language.CSharp.deps.json", "Nightshade.Language.CSharp.runtimeconfig.json"}:
        return True
    if name.startswith(("licenses/", "runtime/", "export-runtime/")):
        return True  # Selected upstream runtimes/notices; never installed in the core applications.
    if name in {"shade-sdk/Documentation.shade", "shade-sdk/LanguageBuild.shade", "shade-sdk/LanguageSession.shade", "shade-sdk/ProviderInstallation.shade"}:
        return True
    if name == "examples/Spin" + {"java": ".java", "python": ".py", "cpp": ".hpp", "csharp": ".cs", "lua": ".lua"}[language]:
        return True
    public = {
        "java": set(), "csharp": set(),
        "python": {"worker.py", "nightshade_sdk/__init__.py"},
        "cpp": {"worker.cpp", "include/nightshade/sdk.hpp", "include/json.hpp"},
        "lua": {"lua", "lua.exe", "luac", "luac.exe", "lua55.dll", "worker.lua", "nightshade_sdk.lua", "compile.lua", "json.lua"},
    }
    return name in public[language]


def validate_inputs(root, target=None, expected_version=None):
    root = Path(root)
    meta = json_read(root / "input-manifest.json")
    require(meta.get("formatVersion") == 1 and meta.get("kind") == "nightshade.shade-release-inputs", "Wrong release input format")
    require(meta.get("platform") in PLATFORMS, "Unsupported input architecture")
    target = target or meta["platform"]
    require(meta["platform"] == target, "Input platform differs")
    version(meta.get("version"))
    require(expected_version is None or meta["version"] == expected_version, "Input release version differs")
    files = meta.get("files", [])
    require(isinstance(files, list) and len(files) <= MAX_FILES, "Invalid input inventory")
    listed = {}
    for entry in files:
        name = relative(entry["path"])
        require(name.casefold() not in {n.casefold() for n in listed}, "Duplicate input inventory path")
        require(allowed_input_path(name, target) == entry.get("role") and entry.get("role") is not None, "Source/private file or invalid role in binary input: " + name)
        path = root / name
        require(path.is_file() and not path.is_symlink(), "Missing or linked input file: " + name)
        require(path.stat().st_size == entry["bytes"] and digest(path) == entry["sha256"], "Input payload differs: " + name)
        listed[name] = entry
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    require(actual == set(listed) | {"input-manifest.json"}, "Unexpected input files outside the inventory")
    suffix = ".exe" if target == "windows-x64" else ""
    required = {"native/shade" + suffix, "native/shade-desktop" + suffix, "qa/checks.json", "notices/native-shade.txt"}
    required |= {"programs/" + name + ".shadebin" for name in PROGRAMS}
    required |= {"branding/" + name for name in ("hub.png", "hub-transparent.png", "studio.png", "studio-transparent.png")}
    require(required <= set(listed), "Required native/program/QA input is absent")
    for name in ("shade", "shade-desktop"):
        require(native_architecture(root / ("native/" + name + suffix)) == target, "Native input executable architecture differs")
    for name, entry_type in PROGRAMS.items():
        ir = json_read(root / ("programs/" + name + ".shadebin"))
        require(ir.get("format_version") == 1 and entry_type in ir.get("types", {}), "Wrong compiled application entry point: " + name)
        require(not ({"sources", "source_files", "files"} & set(ir)), "Compiled IR unexpectedly contains source files")
    selected = []
    for name in listed:
        if listed[name]["role"] == "optional-provider":
            selected.append(provider(root / name, target)["language"])
    require(sorted(selected) == sorted(LANGUAGES), "Exactly five separate optional language packages are required")
    checks = json_read(root / "qa/checks.json")
    require(checks.get("formatVersion") == 1 and isinstance(checks.get("checks"), list), "Invalid compiled QA contract")
    ids = [check_identifier(item.get("id")) for item in checks["checks"]]
    require(len(ids) == len(set(ids)) and REQUIRED_CHECKS <= set(ids), "Mandatory native release checks are missing")
    for item in checks["checks"]:
        require(item.get("mode") in {"test", "run"} and 1 <= item.get("timeoutSeconds", 0) <= 900, "Invalid QA execution mode/timeout")
        require(item.get("program") in listed and item["program"].startswith("qa/") and item["program"].endswith(".shadebin"), "QA must use a frozen compiled program")
        require(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*", item.get("type", "")), "Invalid QA entry type")
        qa_program = json_read(root / item["program"])
        require(qa_program.get("format_version") == 1 and item["type"] in qa_program.get("types", {}), "QA entry point is missing from its frozen compiled program")
    return meta
