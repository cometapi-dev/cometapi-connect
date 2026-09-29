#!/usr/bin/env python3
"""Build a standalone artifact on the target OS. Never publishes a release."""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "CometAPI Connect"
ENTRY = """import json
from pathlib import Path
import sys

from cometapi_helper.cli import main

if len(sys.argv) == 3 and sys.argv[1] == "--packaging-self-test":
    import cometapi_helper
    import certifi
    from cometapi_helper.detection import catalog
    from cometapi_helper.adapters import AUTOMATIC_IDS
    import json5
    import tomlkit
    from cometapi_helper.cherry_store import binding
    import tempfile
    from cometapi_helper.n8n_store import crypt
    from ruamel.yaml import YAML
    entries = catalog()
    assert len(entries) == 35
    assert {item["id"] for item in entries} == AUTOMATIC_IDS
    assert Path(certifi.where()).is_file()
    page = Path(cometapi_helper.__file__).parent / "static" / "index.html"
    assert "<html" in page.read_text(encoding="utf-8").lower()
    assert (page.parent / "i18n.js").is_file()
    languages = {"en", "zh-TW", "ja", "ko", "fr", "de", "es", "it", "pt", "ru", "ar", "th", "vi", "id", "tr", "pl"}
    assert {path.stem for path in (page.parent / "locales").glob("*.json")} == languages
    for language in languages:
        assert json.loads((page.parent / "locales" / (language + ".json")).read_text(encoding="utf-8"))["Review your changes"]
    assert json5.loads("{ready: true}")["ready"]
    assert tomlkit.parse("ready = true")["ready"]
    assert YAML(typ="safe", pure=True).load("ready: true")["ready"]
    with tempfile.TemporaryDirectory(prefix="cometapi-leveldb-selftest-") as temp:
        with binding().DB(str(Path(temp) / "db"), create_if_missing=True) as db:
            with db.write_batch(transaction=True, sync=True) as batch:
                batch.put(b"fixture", b"ready")
            assert db.get(b"fixture") == b"ready"
    assert crypt(crypt(b'{"ready":true}', "frozen-crypto-test-key"), "frozen-crypto-test-key", decrypt=True) == b'{"ready":true}'
    from cometapi_helper.lmstudio_store import bundled_plugin
    assert bundled_plugin().is_file()
    assets = Path(cometapi_helper.__file__).parent / "assets"
    for name in ("sd_webui_cometapi.py", "comfy_nodes.py", "comfy_cloud.py", "dify_native.py"):
        source = assets / name
        assert source.is_file()
        compile(source.read_text(encoding="utf-8"), name, "exec")
    bridge = assets / "roo_bridge"
    assert json.loads((bridge / "package.json").read_text(encoding="utf-8"))["main"] == "extension.cjs"
    for name in ("extension.cjs", "runner.cjs", "store.cjs"):
        assert (bridge / name).is_file() and (bridge / name).stat().st_size > 20
    copilot = assets / "copilot_bridge"
    assert json.loads((copilot / "package.json").read_text(encoding="utf-8"))["main"] == "extension.cjs"
    assert (copilot / "extension.cjs").is_file()
    import hashlib
    licenses = Path(sys._MEIPASS) / "licenses"
    manifest = json.loads((licenses / "manifest.json").read_text(encoding="utf-8"))
    assert {"cometapi connect", "python", "pyinstaller"} <= {
        component["name"].lower() for component in manifest["components"]
    }
    for component in manifest["components"]:
        assert component["files"]
        for filename, digest in component["files"].items():
            content = (licenses / filename).read_bytes()
            assert content and hashlib.sha256(content).hexdigest() == digest
    Path(sys.argv[2]).write_text(json.dumps({"ok": True, "catalog_entries": len(entries), "license_components": len(manifest["components"])}))
else:
    raise SystemExit(main())
"""


def runtime_distributions(project):
    """Resolve installed production dependencies, honoring platform and extra markers."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [Requirement(value) for value in project["dependencies"]]
    found = {}
    visited = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        extras = tuple(sorted(requirement.extras))
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        distribution = metadata.distribution(requirement.name)
        found[name] = distribution
        for value in distribution.requires or ():
            dependency = Requirement(value)
            if dependency.marker and not any(
                dependency.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                continue
            # The parent extra has already been evaluated. Child requirements
            # evaluate their own extras when their dependencies are visited.
            dependency.marker = None
            pending.append(dependency)
    # Only PyInstaller's bootloader is shipped, not its build dependencies.
    found["pyinstaller"] = metadata.distribution("PyInstaller")
    return [found[name] for name in sorted(found)]


def distribution_licenses(distribution):
    """Use license texts recorded by the installed wheel, never guessed notices."""
    result = []
    for recorded in distribution.files or ():
        parts = recorded.parts
        in_license_directory = any(part.endswith(".dist-info") for part in parts) and (
            "licenses" in parts
        )
        if in_license_directory or re.match(
            r"^(licen[sc]e|copying|notice|copyright)(?:$|[._-])", recorded.name, re.IGNORECASE
        ):
            source = Path(distribution.locate_file(recorded))
            if not source.is_file() or not source.stat().st_size:
                raise RuntimeError(
                    "Missing license file for "
                    + distribution.metadata["Name"]
                    + ": "
                    + str(recorded)
                )
            # Preserve the wheel-relative hierarchy without leaking install paths.
            if recorded.is_absolute() or ".." in parts:
                raise RuntimeError("Unsafe license path in " + distribution.metadata["Name"])
            result.append((recorded, source))
    if not result:
        raise RuntimeError("No installed license text found for " + distribution.metadata["Name"])
    return sorted(result, key=lambda item: str(item[0]))


def python_license():
    """CPython installers put the runtime notice in the stdlib or install root."""
    candidates = [
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE",
        Path(sys.base_prefix) / "LICENSE",
    ]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size:
            return candidate
    raise RuntimeError("The build interpreter does not include its Python LICENSE.txt.")


def collect_licenses(destination, project_root=ROOT):
    """Stage actual project/runtime notices and a verifiable, portable inventory."""
    import tomlkit
    from packaging.utils import canonicalize_name

    project = tomlkit.parse((project_root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    components = []

    def include(name, version, folder, sources):
        files = {}
        for relative, source in sources:
            target = destination / folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            content = source.read_bytes()
            if not content:
                raise RuntimeError("Empty license file for " + name)
            target.write_bytes(content)
            files[target.relative_to(destination).as_posix()] = hashlib.sha256(content).hexdigest()
        components.append({"name": name, "version": version, "files": files})

    include(
        "CometAPI Connect",
        project["version"],
        "cometapi-connect",
        [(Path(name), project_root / name) for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")],
    )
    include(
        "Python", platform.python_version(), "python", [(Path("LICENSE.txt"), python_license())]
    )
    for distribution in runtime_distributions(project):
        name = distribution.metadata["Name"]
        include(
            name, distribution.version, canonicalize_name(name), distribution_licenses(distribution)
        )
    # Native libraries bundled by the Plyvel wheels do not all ship their
    # notices in wheel metadata. Keep verified upstream texts alongside them.
    native_notices = project_root / "packaging" / "licenses"
    sources = json.loads((native_notices / "sources.json").read_text(encoding="utf-8"))
    for notice in sources:
        source = native_notices / notice["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != notice["sha256"]:
            raise RuntimeError("Native dependency notice digest mismatch: " + notice["name"])
        include(notice["name"], None, "native", [(Path(notice["file"]), source)])
        # The source revision identifies the notice, not a claim about which
        # library version an upstream wheel linked for this operating system.
        components[-1]["notice_source"] = notice["source"]
    manifest = {"components": components}
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def run(command, **kwargs):
    # Do not echo command arguments: signing arguments can contain credentials.
    return subprocess.run([str(item) for item in command], check=True, **kwargs)


@contextlib.contextmanager
def mac_keychain():
    encoded = os.environ.get("MACOS_CERTIFICATE_P12_BASE64")
    if sys.platform != "darwin" or not encoded:
        yield
        return
    original = shlex.split(
        run(["security", "list-keychains", "-d", "user"], capture_output=True, text=True).stdout
    )
    with tempfile.TemporaryDirectory(prefix="cometapi-signing-") as temporary:
        folder = Path(temporary)
        certificate = folder / "certificate.p12"
        certificate.write_bytes(base64.b64decode(encoded, validate=True))
        certificate.chmod(0o600)
        keychain = folder / "build.keychain-db"
        password = secrets.token_urlsafe(32)
        try:
            run(["security", "create-keychain", "-p", password, keychain])
            run(["security", "set-keychain-settings", "-lut", "21600", keychain])
            run(["security", "unlock-keychain", "-p", password, keychain])
            run(
                [
                    "security",
                    "import",
                    certificate,
                    "-k",
                    keychain,
                    "-P",
                    os.environ.get("MACOS_CERTIFICATE_PASSWORD", ""),
                    "-T",
                    "/usr/bin/codesign",
                    "-T",
                    "/usr/bin/security",
                ]
            )
            run(
                [
                    "security",
                    "set-key-partition-list",
                    "-S",
                    "apple-tool:,apple:",
                    "-k",
                    password,
                    keychain,
                ],
                stdout=subprocess.DEVNULL,
            )
            run(["security", "list-keychains", "-d", "user", "-s", *original, keychain])
            yield
        finally:
            run(["security", "list-keychains", "-d", "user", "-s", *original])
            if keychain.exists():
                run(["security", "delete-keychain", keychain])


def sign_windows(executable):
    encoded = os.environ.get("WINDOWS_CERTIFICATE_PFX_BASE64")
    if not encoded:
        return False
    signtool = os.environ.get("SIGNTOOL_PATH") or shutil.which("signtool")
    if not signtool:
        kits = (
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Windows Kits"
            / "10"
            / "bin"
        )
        matches = sorted(kits.glob("*/x64/signtool.exe"), reverse=True)
        signtool = str(matches[0]) if matches else None
    if not signtool:
        raise RuntimeError("Signing was requested but SignTool was not found.")
    with tempfile.TemporaryDirectory(prefix="cometapi-signing-") as temporary:
        certificate = Path(temporary) / "certificate.pfx"
        certificate.write_bytes(base64.b64decode(encoded, validate=True))
        run(
            [
                signtool,
                "sign",
                "/f",
                certificate,
                "/p",
                os.environ.get("WINDOWS_CERTIFICATE_PASSWORD", ""),
                "/fd",
                "SHA256",
                "/tr",
                os.environ.get("WINDOWS_TIMESTAMP_URL") or "http://timestamp.digicert.com",
                "/td",
                "SHA256",
                executable,
            ]
        )
        run([signtool, "verify", "/pa", "/all", executable])
    return True


def smoke_test(executable, folder):
    result = folder / "frozen-self-test.json"
    run([executable, "--packaging-self-test", result], timeout=90)
    if not result.exists() or json.loads(result.read_text()).get("ok") is not True:
        raise RuntimeError("The standalone executable failed its bundled-resource self-test.")
    run([sys.executable, ROOT / "scripts" / "smoke_frozen.py", executable], timeout=90)


def pe_architecture(executable):
    """Read the actual Windows executable architecture, including under emulation."""
    with Path(executable).open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise RuntimeError("The Windows executable has an invalid DOS header.")
        offset = int.from_bytes(header[60:64], "little")
        size = stream.seek(0, os.SEEK_END)
        if offset < 64 or offset > size - 6:
            raise RuntimeError("The Windows executable has an invalid PE offset.")
        stream.seek(offset)
        header = stream.read(6)
        if header[:4] != b"PE\0\0":
            raise RuntimeError("The Windows executable has an invalid PE signature.")
        architecture = {0x8664: "x64", 0xAA64: "arm64"}.get(int.from_bytes(header[4:6], "little"))
        if architecture is None:
            raise RuntimeError("Only Windows x64 and ARM64 executables are supported.")
        return architecture


def build_architecture():
    if sys.platform == "win32":
        # Windows ARM64 can report ARM64 for an emulated x64 Python process.
        return pe_architecture(sys.executable)
    machine = platform.machine().lower()
    architecture = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine
    )
    if architecture is None:
        raise RuntimeError("Only 64-bit x86 and ARM release builds are supported.")
    return architecture


def verify_windows_architecture(executable, expected):
    if pe_architecture(executable) != expected:
        raise RuntimeError(
            "The Windows executable architecture does not match the build interpreter."
        )


def build(args):
    if sys.platform not in {"darwin", "win32", "linux"}:
        raise RuntimeError("Build on macOS, Windows, or Linux; cross-compilation is not supported.")
    version = args.version or re.search(
        r'__version__ = "([^"]+)"', (ROOT / "cometapi_helper" / "__init__.py").read_text()
    ).group(1)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?", version):
        raise RuntimeError("Version must be a safe release version such as 0.1.0 or 0.1.0-rc.1.")
    identity = os.environ.get("MACOS_SIGNING_IDENTITY") or None
    notary = [
        os.environ.get(name) for name in ("MACOS_APPLE_ID", "MACOS_TEAM_ID", "MACOS_APP_PASSWORD")
    ]
    if any(notary) and not all(notary):
        raise RuntimeError("Provide all three Apple notarization credentials together.")
    if args.require_signing and sys.platform == "darwin" and not (identity and all(notary)):
        raise RuntimeError("A public macOS release requires signing and notarization credentials.")
    if (
        args.require_signing
        and sys.platform == "win32"
        and not os.environ.get("WINDOWS_CERTIFICATE_PFX_BASE64")
    ):
        raise RuntimeError("A public Windows release requires a code-signing certificate.")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    arch = build_architecture()
    target = {"darwin": "macos", "win32": "windows", "linux": "linux"}[sys.platform]
    if target == "macos":
        arch = (
            args.mac_arch
            if args.mac_arch != "native"
            else {"x64": "x86_64", "arm64": "arm64"}[arch]
        )
    asset_key = target + "-" + arch
    with tempfile.TemporaryDirectory(prefix="cometapi-build-") as temporary:
        folder = Path(temporary)
        entry = folder / "cometapi_entry.py"
        entry.write_text(ENTRY, encoding="utf-8")
        licenses = folder / "licenses"
        license_manifest = collect_licenses(licenses)
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--noupx",
            "--name",
            APP_NAME,
            "--paths",
            ROOT,
            "--collect-data",
            "cometapi_helper",
            "--collect-submodules",
            "ruamel.yaml",
            # collect-data excludes .py files by default. These sources
            # are installable vendor plug-ins, not imported app modules.
            "--add-data",
            str(ROOT / "cometapi_helper/assets") + os.pathsep + "cometapi_helper/assets",
            "--add-data",
            str(licenses) + os.pathsep + "licenses",
            "--exclude-module",
            "_ruamel_yaml",
            "--exclude-module",
            "ruamel.yaml.clib",
            "--distpath",
            folder / "dist",
            "--workpath",
            folder / "work",
            "--specpath",
            folder,
        ]
        if target == "macos":
            command += ["--icon", ROOT / "packaging" / "cometapi-connect.icns"]
            command += [
                "--onedir",
                "--windowed",
                "--target-arch",
                arch,
                "--osx-bundle-identifier",
                "com.cometapi.connect",
                "--exclude-module",
                "cryptography",
            ]
            if identity:
                command += ["--codesign-identity", identity]
        else:
            command += ["--onefile"]
            if target == "windows":
                command += ["--windowed", "--icon", ROOT / "packaging" / "cometapi-connect.ico"]
        with mac_keychain():
            build_env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(folder / "pyinstaller-cache"))
            run(command + [entry], cwd=ROOT, env=build_env)
            signing = "unsigned"
            if target == "macos":
                app = folder / "dist" / (APP_NAME + ".app")
                # The CLI-generated PyInstaller bundle otherwise reports 0.0.0
                # in Finder. Updating its sealed metadata requires re-signing
                # the outer bundle; nested binaries retain their signatures.
                info_path = app / "Contents" / "Info.plist"
                info = plistlib.loads(info_path.read_bytes())
                bundle_version = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", version).group(0)
                info.update(
                    CFBundleShortVersionString=bundle_version,
                    CFBundleVersion=bundle_version,
                    CometAPIReleaseVersion=version,
                )
                info_path.write_bytes(plistlib.dumps(info, sort_keys=False))
                resign = [
                    "/usr/bin/codesign",
                    "--force",
                    "--all-architectures",
                    "--sign",
                    identity or "-",
                ]
                if identity:
                    resign += ["--options=runtime", "--timestamp"]
                run(resign + [app])
                run(
                    [
                        "/usr/bin/codesign",
                        "--verify",
                        "--all-architectures",
                        "--deep",
                        "--strict",
                        app,
                    ]
                )
                executable = app / "Contents" / "MacOS" / APP_NAME
                smoke_test(executable, folder)
                if arch == "universal2":
                    run(["lipo", executable, "-verify_arch", "arm64", "x86_64"])
                if identity:
                    run(["codesign", "--verify", "--deep", "--strict", app])
                    signing = "signed"
                stage = folder / "dmg"
                stage.mkdir()
                run(["ditto", app, stage / app.name])
                (stage / "Applications").symlink_to("/Applications")
                suffix = (
                    ""
                    if identity and all(notary)
                    else ("-unnotarized" if identity else "-unsigned")
                )
                artifact = output / (
                    "CometAPI-Connect-" + version + "-" + asset_key + suffix + ".dmg"
                )
                run(
                    [
                        "hdiutil",
                        "create",
                        "-volname",
                        APP_NAME,
                        "-srcfolder",
                        stage,
                        "-ov",
                        "-format",
                        "UDZO",
                        artifact,
                    ]
                )
                if identity:
                    run(["codesign", "--force", "--sign", identity, "--timestamp", artifact])
                if identity and all(notary):
                    run(
                        [
                            "xcrun",
                            "notarytool",
                            "submit",
                            artifact,
                            "--apple-id",
                            notary[0],
                            "--team-id",
                            notary[1],
                            "--password",
                            notary[2],
                            "--wait",
                        ]
                    )
                    run(["xcrun", "stapler", "staple", artifact])
                    run(["xcrun", "stapler", "validate", artifact])
                    signing = "notarized"
            elif target == "windows":
                executable = folder / "dist" / (APP_NAME + ".exe")
                verify_windows_architecture(executable, arch)
                signed = sign_windows(executable)
                signing = "signed" if signed else "unsigned"
                smoke_test(executable, folder)
                artifact = output / (
                    "CometAPI-Connect-"
                    + version
                    + "-"
                    + asset_key
                    + ("" if signed else "-unsigned")
                    + ".exe"
                )
                shutil.copy2(executable, artifact)
            else:
                executable = folder / "dist" / APP_NAME
                smoke_test(executable, folder)
                artifact = output / ("CometAPI-Connect-" + version + "-" + asset_key + ".tar.gz")
                with tarfile.open(artifact, "w:gz") as archive:
                    archive.add(executable, arcname=APP_NAME)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (output / (artifact.name + ".sha256")).write_text(
            digest + "  " + artifact.name + "\n", encoding="utf-8"
        )
        metadata = {
            "version": version,
            "platform": asset_key,
            "filename": artifact.name,
            "sha256": digest,
            "signing": signing,
            "packaging_self_test": True,
            "license_components": len(license_manifest["components"]),
            "python": platform.python_version(),
            "build_os": platform.platform(),
        }
        (output / (asset_key + ".json")).write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        print("Built " + str(artifact))
        print("Signing status: " + signing + "; bundled-resource self-test passed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--output", default=str(ROOT / "dist" / "releases"))
    parser.add_argument(
        "--mac-arch", choices=["universal2", "arm64", "x86_64", "native"], default="universal2"
    )
    parser.add_argument(
        "--require-signing",
        action="store_true",
        help="Fail unless Windows is signed or macOS is signed and notarized.",
    )
    args = parser.parse_args()
    try:
        build(args)
    except subprocess.CalledProcessError as error:
        # CalledProcessError.__str__ includes the command and potentially secrets.
        parser.exit(
            1, "Release build failed in an external tool (exit " + str(error.returncode) + ").\n"
        )
    except (RuntimeError, OSError, ValueError) as error:
        parser.exit(1, "Release build failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
