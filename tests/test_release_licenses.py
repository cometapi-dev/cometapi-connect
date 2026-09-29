"""Release artifacts must retain the license texts from their build environment."""

import hashlib
import json
from email.message import Message
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from scripts import build_release


def distribution(tmp_path, name, license_files, requirements=()):
    metadata = Message()
    metadata["Name"] = name
    files = []
    for relative, content in license_files.items():
        path = tmp_path / name / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append(PurePosixPath(relative))
    return SimpleNamespace(
        metadata=metadata,
        version="1.0",
        files=files,
        requires=requirements,
        locate_file=lambda relative: tmp_path / name / relative,
    )


def test_inventory_preserves_actual_license_bytes_without_install_paths(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nversion = "1.0"\ndependencies = []\n')
    (project / "LICENSE").write_bytes(b"project permission notice\n")
    (project / "THIRD_PARTY_NOTICES.md").write_bytes(b"component attribution\n")
    notices = project / "packaging" / "licenses"
    notices.mkdir(parents=True)
    (notices / "NOTICE").write_bytes(b"native copyright notice\n")
    (notices / "sources.json").write_text(
        json.dumps(
            [
                {
                    "name": "Native library",
                    "file": "NOTICE",
                    "source": "https://example.com/upstream/NOTICE",
                    "sha256": hashlib.sha256(b"native copyright notice\n").hexdigest(),
                }
            ]
        )
    )
    python_notice = tmp_path / "Python-LICENSE.txt"
    python_notice.write_bytes(b"Python runtime notice\n")
    package = distribution(
        tmp_path,
        "fixture",
        {
            "fixture-1.0.dist-info/licenses/LICENSE": b"upstream copyright\r\npermission\r\n",
            "fixture-1.0.dist-info/licenses/NOTICE": b"upstream notice\n",
        },
    )
    monkeypatch.setattr(build_release, "runtime_distributions", lambda project: [package])
    monkeypatch.setattr(build_release, "python_license", lambda: python_notice)
    destination = tmp_path / "licenses"
    inventory = build_release.collect_licenses(destination, project_root=project)
    encoded = (destination / "manifest.json").read_text()
    assert json.loads(encoded) == inventory
    assert str(tmp_path) not in encoded
    assert {component["name"] for component in inventory["components"]} == {
        "CometAPI Connect",
        "Python",
        "fixture",
        "Native library",
    }
    for component in inventory["components"]:
        for filename, digest in component["files"].items():
            assert hashlib.sha256((destination / filename).read_bytes()).hexdigest() == digest
    (notices / "NOTICE").write_bytes(b"changed notice")
    with pytest.raises(RuntimeError, match="digest mismatch"):
        build_release.collect_licenses(tmp_path / "invalid", project_root=project)
    for relative in package.files:
        assert (destination / "fixture" / relative).read_bytes() == package.locate_file(
            relative
        ).read_bytes()


@pytest.mark.parametrize("missing", ["unrecorded", "absent", "empty"])
def test_release_refuses_missing_dependency_license(tmp_path, missing):
    package = distribution(tmp_path, "fixture", {"fixture.dist-info/LICENSE": b"upstream terms"})
    if missing == "unrecorded":
        package.files = []
    elif missing == "absent":
        package.locate_file(package.files[0]).unlink()
    else:
        package.locate_file(package.files[0]).write_bytes(b"")
    with pytest.raises(RuntimeError, match="license"):
        build_release.distribution_licenses(package)


def test_runtime_dependency_inventory_honors_extras_and_excludes_dev_dependencies(
    tmp_path, monkeypatch
):
    packages = {
        "parent": distribution(
            tmp_path,
            "parent",
            {},
            requirements=[
                'child; python_version >= "3.9"',
                'extra-child; extra == "optional"',
                'dev-only; extra == "dev"',
                'not-supported; python_version < "3.0"',
            ],
        ),
        "child": distribution(tmp_path, "child", {}),
        "extra-child": distribution(tmp_path, "extra-child", {}),
        "PyInstaller": distribution(tmp_path, "PyInstaller", {}, requirements=["build-only"]),
    }
    monkeypatch.setattr(build_release.metadata, "distribution", lambda name: packages[name])
    result = build_release.runtime_distributions({"dependencies": ["parent[optional]"]})
    assert {item.metadata["Name"] for item in result} == {
        "parent",
        "child",
        "extra-child",
        "PyInstaller",
    }


@pytest.mark.parametrize("location", ["stdlib", "prefix"])
def test_python_license_uses_the_actual_interpreter_notice(tmp_path, monkeypatch, location):
    stdlib = tmp_path / "stdlib"
    prefix = tmp_path / "prefix"
    stdlib.mkdir()
    prefix.mkdir()
    monkeypatch.setattr(build_release.sysconfig, "get_path", lambda name: str(stdlib))
    monkeypatch.setattr(build_release.sys, "base_prefix", str(prefix))
    with pytest.raises(RuntimeError, match="Python LICENSE"):
        build_release.python_license()
    expected = (stdlib if location == "stdlib" else prefix) / "LICENSE.txt"
    expected.write_bytes(b"Actual Python runtime license\n")
    assert build_release.python_license() == expected
