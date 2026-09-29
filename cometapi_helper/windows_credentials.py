"""Windows Credential Manager contracts for Jan and Zed.

Jan's Rust keyring stores a UTF-16LE password; Zed's gpui_windows stores raw
bytes. Secrets never enter process arguments or a plaintext fallback file.
"""

import base64
import ctypes as C
import json
import sys
from ctypes import wintypes as W

from .common import SetupError


class FILETIME(C.Structure):
    _fields_ = [("low", W.DWORD), ("high", W.DWORD)]


class CREDENTIAL(C.Structure):
    _fields_ = [
        ("Flags", W.DWORD),
        ("Type", W.DWORD),
        ("TargetName", W.LPWSTR),
        ("Comment", W.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", W.DWORD),
        ("CredentialBlob", C.c_void_p),
        ("Persist", W.DWORD),
        ("AttributeCount", W.DWORD),
        ("Attributes", C.c_void_p),
        ("TargetAlias", W.LPWSTR),
        ("UserName", W.LPWSTR),
    ]


class WindowsGenericPassword:
    password_encoding = "utf-16-le"
    default_persist = 3
    default_comment = "keyring v3.6.3"

    def __init__(self, account):
        if sys.platform != "win32":
            raise SetupError("This credential adapter requires Windows.")
        if not isinstance(account, str) or not account or "\0" in account:
            raise SetupError("Invalid Windows credential account.")
        self.account = account
        self.api = C.WinDLL("advapi32", use_last_error=True)
        self.api.CredReadW.argtypes = [
            W.LPCWSTR,
            W.DWORD,
            W.DWORD,
            C.POINTER(C.POINTER(CREDENTIAL)),
        ]
        self.api.CredReadW.restype = W.BOOL
        self.api.CredWriteW.argtypes = [C.POINTER(CREDENTIAL), W.DWORD]
        self.api.CredWriteW.restype = W.BOOL
        self.api.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
        self.api.CredDeleteW.restype = W.BOOL
        self.api.CredFree.argtypes = [C.c_void_p]
        self.api.CredFree.restype = None

    def check_target(self, target):
        if not isinstance(target, str) or not target or "\0" in target or len(target) > 32767:
            raise SetupError("Invalid Windows credential target.")

    def failed(self):
        raise SetupError(
            "Windows Credential Manager denied or failed the operation (status "
            + str(C.get_last_error())
            + "). No access controls were changed."
        )

    def native_read(self, target):
        self.check_target(target)
        result = C.POINTER(CREDENTIAL)()
        if not self.api.CredReadW(target, 1, 0, C.byref(result)):
            if C.get_last_error() == 1168:
                return None
            self.failed()
        try:
            item = result.contents
            if (
                item.Type != 1
                or item.Flags != 0
                or item.AttributeCount != 0
                or item.UserName != self.account
                or item.CredentialBlobSize > 2560
                or item.Persist not in (1, 2, 3)
            ):
                raise SetupError(
                    "Unexpected Windows credential attributes or account; the entry was preserved."
                )
            raw = C.string_at(item.CredentialBlob, item.CredentialBlobSize)
            try:
                password = (
                    raw.decode(self.password_encoding).encode("utf-8")
                    if self.password_encoding
                    else raw
                )
            except UnicodeError:
                raise SetupError(
                    "The Windows credential is not a supported keyring password."
                ) from None
            return {
                "password": password,
                "comment": item.Comment,
                "alias": item.TargetAlias,
                "persist": item.Persist,
            }
        finally:
            self.api.CredFree(result)

    def read(self, target):
        from .credentials import encode

        value = self.native_read(target)
        return None if value is None else encode(self.account, value["password"])

    def write(self, target, value):
        if value is not None:
            try:
                doc = json.loads(value)
                if set(doc) != {"username", "password"} or doc["username"] != self.account:
                    raise ValueError()
                password = base64.b64decode(doc["password"], validate=True)
                if self.password_encoding:
                    password = password.decode("utf-8").encode(self.password_encoding)
                if len(password) > 2560:
                    raise ValueError()
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise SetupError("Invalid Windows credential backup or account.") from None
        previous = self.native_read(target)
        if value is None:
            if previous is not None and not self.api.CredDeleteW(target, 1, 0):
                if C.get_last_error() != 1168:
                    self.failed()
            return
        blob = C.create_string_buffer(password)
        item = CREDENTIAL()
        item.Type = 1
        item.TargetName = target
        item.UserName = self.account
        item.Comment = previous["comment"] if previous else self.default_comment
        item.TargetAlias = previous["alias"] if previous else ""
        item.Persist = previous["persist"] if previous else self.default_persist
        item.CredentialBlobSize = len(password)
        item.CredentialBlob = C.cast(blob, C.c_void_p)
        try:
            if not self.api.CredWriteW(C.byref(item), 0):
                self.failed()
        finally:
            C.memset(blob, 0, len(blob))


class WindowsZedPassword(WindowsGenericPassword):
    """Zed gpui_windows stores raw bytes, not Rust keyring's UTF-16 password."""

    password_encoding = None
    default_persist = 2  # CRED_PERSIST_LOCAL_MACHINE, matching Zed 1.20.2.
    default_comment = None
