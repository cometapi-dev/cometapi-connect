import io
import os
import sys

import pytest

from cometapi_helper.cli import main
from cometapi_helper.runtime import system_program_environment


def test_cli_dry_run_never_writes_or_echoes_key(tmp_path, monkeypatch, capsys):
    key = "sk-cli-test-key-not-real"
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\n"))
    main(["--home", str(tmp_path), "configure", "--apps", "aider", "--key-stdin", "--dry-run"])
    output = capsys.readouterr().out
    assert key not in output
    assert "Dry run complete" in output
    assert not (tmp_path / ".aider.conf.yml").exists()
    assert not (tmp_path / ".cometapi-helper").exists()


def test_noninteractive_cli_requires_explicit_app_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-this-should-never-be-read\n"))
    with pytest.raises(SystemExit) as error:
        main(["--home", str(tmp_path), "configure", "--key-stdin", "--yes"])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "--apps" in output.err
    assert "sk-this" not in output.err + output.out


def test_invalid_home_is_a_safe_cli_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--home", str(tmp_path / "missing"), "scan"])
    assert error.value.code == 1
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.parametrize("original", [None, "/system/library"])
def test_linux_frozen_external_program_environment_is_restored(monkeypatch, original):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundled/library")
    if original:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)
    else:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    with system_program_environment():
        assert os.environ.get("LD_LIBRARY_PATH") == original
    assert os.environ["LD_LIBRARY_PATH"] == "/bundled/library"
