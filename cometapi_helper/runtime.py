"""Keep bundled library paths out of operating-system browser launchers."""

import contextlib
import os
import sys


@contextlib.contextmanager
def system_program_environment():
    if not getattr(sys, "frozen", False):
        yield
        return
    previous = os.environ.get("LD_LIBRARY_PATH")
    if sys.platform.startswith("linux"):
        original = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            os.environ.pop("LD_LIBRARY_PATH", None)
        else:
            os.environ["LD_LIBRARY_PATH"] = original
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        yield
    finally:
        if sys.platform.startswith("linux"):
            if previous is None:
                os.environ.pop("LD_LIBRARY_PATH", None)
            else:
                os.environ["LD_LIBRARY_PATH"] = previous
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetDllDirectoryW(sys._MEIPASS)
