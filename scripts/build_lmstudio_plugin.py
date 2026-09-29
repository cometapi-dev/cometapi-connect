#!/usr/bin/env python3
"""Rebuild the vendored LM Studio revision 9 plugin with reviewed, local inputs.

No downloads or package lifecycle scripts are run. Use the reviewed repository
archive or its archived original as source; unchanged dependencies are trusted
from that input. Optional inputs support upgrading the original snapshot.
"""

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    return path


def read_archive(path):
    files = {}
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 3000 or sum(entry.file_size for entry in entries) > 15_000_000:
            raise ValueError("Plugin archive exceeds the installer size limit")
        for entry in entries:
            safe_name(entry.filename)
            if entry.is_dir() or entry.filename in files:
                raise ValueError("Expected unique, regular archive files")
            if (entry.external_attr >> 16) & 0o170000 != 0o100000:
                raise ValueError("Expected regular archive files")
            files[entry.filename] = archive.read(entry)
    return files


def checked(data, expected, label):
    if digest(data) != expected:
        raise ValueError(f"Integrity check failed: {label}")
    return data


def prepare_files(source, provenance, ws_tarball=None, undici_license=None):
    files = read_archive(source)
    for name, expected in provenance["source_sha256"].items():
        checked(files[name], expected, name)
    files = {
        name: data
        for name, data in files.items()
        if name != "install-state.json"
        and not {".turbo", ".DS_Store", "__MACOSX"}.intersection(PurePosixPath(name).parts)
    }
    ws = provenance["ws"]
    if ws_tarball:
        raw = ws_tarball.read_bytes()
        checked(raw, ws["tarball_sha256"], "ws tarball")
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()
        if integrity != ws["integrity"]:
            raise ValueError("ws npm integrity mismatch")
        files = {
            name: data for name, data in files.items() if not name.startswith("node_modules/ws/")
        }
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                path = safe_name(member.name)
                if not member.isfile() or path.parts[0] != "package":
                    raise ValueError("Unexpected ws package archive entry")
                name = str(PurePosixPath("node_modules/ws", *path.parts[1:]))
                if name in files:
                    raise ValueError("Duplicate ws package archive entry")
                files[name] = archive.extractfile(member).read()
    expected_ws_files = {"node_modules/ws/" + name: sha for name, sha in ws["files_sha256"].items()}
    actual_ws_files = {name for name in files if name.startswith("node_modules/ws/")}
    if actual_ws_files != set(expected_ws_files):
        raise ValueError("ws package files differ from the reviewed release")
    for name, expected in expected_ws_files.items():
        checked(files[name], expected, name)
    for name in ("package-lock.json", "node_modules/.package-lock.json"):
        lock = json.loads(files[name])
        entry = lock["packages"]["node_modules/ws"]
        entry.update(version=ws["version"], resolved=ws["tarball_url"], integrity=ws["integrity"])
        files[name] = (json.dumps(lock, indent=2) + "\n").encode()
    license_name = "node_modules/undici-types/LICENSE"
    if undici_license:
        files[license_name] = undici_license.read_bytes()
    checked(files[license_name], provenance["undici_license"]["sha256"], license_name)
    # Record exactly which upstream sources and security patch this archive uses.
    files["COMETAPI-PROVENANCE.json"] = (json.dumps(provenance, indent=2) + "\n").encode()
    return files


def compile_plugin(files, esbuild, provenance):
    esbuild = esbuild.resolve(strict=True)
    version = subprocess.run(
        [str(esbuild), "--version"], check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()
    if version != provenance["build"]["esbuild_version"]:
        raise ValueError("Use the exact esbuild version recorded in provenance")
    with tempfile.TemporaryDirectory(prefix="cometapi-lmstudio-build-") as temporary:
        stage = Path(temporary)
        for name in provenance["source_sha256"]:
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
        subprocess.run(
            [str(esbuild), *provenance["build"]["arguments"]],
            cwd=stage,
            check=True,
            timeout=60,
        )
        production = (stage / ".lmstudio/production.js").read_bytes()
        if b"sourceMappingURL=" in production or re.search(
            rb"(?:/Users/|[A-Z]:\\\\Users\\\\)", production
        ):
            raise ValueError("Compiled output contains a source map or personal build path")
        files[".lmstudio/production.js"] = production


def pack(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 29, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def main():
    script_dir = Path(__file__).resolve().parent
    default_asset = script_dir.parent / "cometapi_helper/assets/lmstudio-openai-compat-rev9.zip"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_asset, help="Reviewed source ZIP")
    parser.add_argument("--output", type=Path, default=default_asset, help="Destination ZIP")
    parser.add_argument(
        "--esbuild", type=Path, required=True, help="Local esbuild 0.25.10 executable"
    )
    parser.add_argument(
        "--ws-tarball", type=Path, help="Reviewed ws 8.21.0 npm tarball, if upgrading"
    )
    parser.add_argument("--undici-license", type=Path, help="Undici 5.26.5 LICENSE, if missing")
    parser.add_argument(
        "--check", action="store_true", help="Compare rebuilt ZIP without updating output"
    )
    args = parser.parse_args()
    provenance = json.loads((script_dir / "lmstudio-plugin-provenance.json").read_text())
    try:
        files = prepare_files(args.source, provenance, args.ws_tarball, args.undici_license)
        compile_plugin(files, args.esbuild, provenance)
        raw = pack(files)
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != raw:
                raise ValueError("Bundled plugin differs from the reproducible rebuild")
        else:
            args.output.write_bytes(raw)
        print(f"{digest(raw)}  {args.output.name} ({len(files)} files)")
    except (KeyError, ValueError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Plugin rebuild failed: {error}\n")


if __name__ == "__main__":
    main()
