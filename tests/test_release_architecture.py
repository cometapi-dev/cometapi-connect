import pytest

from scripts import build_release


def write_pe(path, machine=0x8664):
    data = bytearray(134)
    data[:2] = b"MZ"
    data[60:64] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    data[132:134] = machine.to_bytes(2, "little")
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("machine,expected", [(0x8664, "x64"), (0xAA64, "arm64")])
def test_pe_architecture_reads_machine_field(tmp_path, machine, expected):
    assert build_release.pe_architecture(write_pe(tmp_path / "python.exe", machine)) == expected


def test_emulated_windows_python_uses_pe_architecture(tmp_path, monkeypatch):
    executable = write_pe(tmp_path / "python.exe")
    monkeypatch.setattr(build_release.sys, "platform", "win32")
    monkeypatch.setattr(build_release.sys, "executable", str(executable))
    monkeypatch.setattr(build_release.platform, "machine", lambda: "ARM64")
    assert build_release.build_architecture() == "x64"


def test_final_executable_must_match_interpreter(tmp_path):
    executable = write_pe(tmp_path / "app.exe", 0xAA64)
    with pytest.raises(RuntimeError, match="does not match"):
        build_release.verify_windows_architecture(executable, "x64")
    build_release.verify_windows_architecture(executable, "arm64")


@pytest.mark.parametrize("malformation", ["short", "dos", "offset", "signature", "x86"])
def test_invalid_or_unsupported_pe_is_refused(tmp_path, malformation):
    executable = write_pe(tmp_path / "app.exe", 0x14C if malformation == "x86" else 0x8664)
    data = bytearray(executable.read_bytes())
    if malformation == "short":
        data = data[:20]
    elif malformation == "dos":
        data[:2] = b"NO"
    elif malformation == "offset":
        data[60:64] = (len(data)).to_bytes(4, "little")
    elif malformation == "signature":
        data[128:132] = b"NOPE"
    executable.write_bytes(data)
    with pytest.raises(RuntimeError):
        build_release.pe_architecture(executable)
