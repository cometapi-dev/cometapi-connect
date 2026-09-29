import json

import pytest

from cometapi_helper import copilot_store, roo_store
from cometapi_helper.common import Context, SetupError


def windows_install(tmp_path):
    app = tmp_path / "VSCode"
    resources = app / "88e44fa0e0/resources/app"
    resources.mkdir(parents=True)
    (app / "Code.exe").write_bytes(b"executable")
    (resources / "package.json").write_text(json.dumps({"name": "Code", "version": "1.136.2"}))
    (resources / "product.json").write_text(
        json.dumps({"applicationName": "code", "win32AppUserModelId": "Microsoft.VisualStudioCode"})
    )
    copilot = resources / "extensions/copilot"
    copilot.mkdir(parents=True)
    (copilot / "package.json").write_text(
        json.dumps({"publisher": "GitHub", "name": "copilot-chat", "version": "0.64.1"})
    )
    profile = app / "data/user-data"
    (profile / "User/globalStorage").mkdir(parents=True)
    (profile / "User/globalStorage/state.vscdb").write_bytes(b"db")
    extensions = app / "data/extensions"
    roo = extensions / "rooveterinaryinc.roo-cline-3.54.0"
    roo.mkdir(parents=True)
    (roo / "package.json").write_text(
        json.dumps({"publisher": "RooVeterinaryInc", "name": "roo-cline", "version": "3.54.0"})
    )
    return app, profile, extensions, resources


@pytest.mark.parametrize("adapter", [roo_store, copilot_store])
def test_windows_portable_discovery_accepts_version_changes(tmp_path, adapter):
    app, profile, extensions, resources = windows_install(tmp_path)
    ctx = Context(home=tmp_path, platform="win32", env={}, roots=[app], use_path=False)
    target = adapter.find(ctx)
    assert target["profile"] == str(profile) and target["extensions"] == str(extensions)
    assert adapter.validate(target) == (app, profile, extensions)
    (resources / "package.json").write_text('{"name":"Code","version":"9.9"}')
    for manifest in [
        resources / "extensions/copilot/package.json",
        next(extensions.glob("roo*/package.json")),
    ]:
        pkg = json.loads(manifest.read_text())
        pkg["version"] = "99.0.0"
        manifest.write_text(json.dumps(pkg))
    assert adapter.validate(target) == (app, profile, extensions)
    (resources / "package.json").write_text('{"name":"OtherApp","version":"9.9"}')
    with pytest.raises(SetupError):
        adapter.validate(target)


@pytest.mark.parametrize("adapter", [roo_store, copilot_store])
def test_portable_refuses_redirected_profile(tmp_path, adapter):
    app, profile, extensions, _ = windows_install(tmp_path)
    other = tmp_path / "other"
    (other / "User/globalStorage").mkdir(parents=True)
    (other / "User/globalStorage/state.vscdb").write_bytes(b"db")
    with pytest.raises(SetupError):
        adapter.find(
            Context(
                home=tmp_path,
                platform="win32",
                env={},
                roots=[app, other, extensions],
                use_path=False,
            )
        )


def test_windows_process_guard_matches_selected_profile_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cometapi_helper import windows_vscode as native

    app, profile, _, _ = windows_install(tmp_path)
    rows = [
        {
            "ExecutablePath": str(app / "Code.exe"),
            "CommandLine": f'"{app / "Code.exe"}" --user-data-dir "{profile}"',
        }
    ]
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(rows))

    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(SetupError, match="Close the selected"):
        native.require_closed(app, profile)
    assert "Get-CimInstance" in calls[0][0][-1]
    assert str(profile) not in calls[0][0][-1]
    rows[:] = [
        {
            "ExecutablePath": str(tmp_path / "different/Code.exe"),
            "CommandLine": f'Code.exe --user-data-dir "{tmp_path / "other"}"',
        }
    ]
    native.require_closed(app, profile)
    rows[:] = [{"ExecutablePath": None, "CommandLine": None}]
    with pytest.raises(SetupError, match="Cannot verify"):
        native.require_closed(app, profile)


def test_windows_owned_cleanup_never_targets_every_code_process(tmp_path, monkeypatch):
    from cometapi_helper import windows_vscode as native

    app, _, _, _ = windows_install(tmp_path)
    calls = []

    class Process:
        pid = 321

        def poll(self):
            return None

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(native.subprocess, "run", lambda args, **kw: calls.append(args))
    native.stop(Process(), app)
    assert calls[0][1:] == ["/PID", "321", "/T", "/F"]
    assert "/IM" not in calls[0]


def test_other_installation_sharing_default_profile_is_open(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from cometapi_helper import windows_vscode as native

    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(roaming))
    selected = tmp_path / "Selected"
    other = tmp_path / "Other"
    rows = [{"ExecutablePath": str(other / "Code.exe"), "CommandLine": f'"{other / "Code.exe"}"'}]
    monkeypatch.setattr(
        native.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(rows))
    )
    with pytest.raises(SetupError, match="Close the selected"):
        native.require_closed(selected, roaming / "Code")
    (other / "data").mkdir(parents=True)
    native.require_closed(selected, roaming / "Code")
    with pytest.raises(SetupError, match="Close the selected"):
        native.require_closed(selected, other / "data/user-data")
