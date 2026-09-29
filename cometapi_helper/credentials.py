"""Native credential operations; secrets never enter command-line arguments."""

import base64
import ctypes as C
import json
import sys

from .common import BASE_URL, SetupError
from .windows_credentials import WindowsGenericPassword, WindowsZedPassword


def encode(username, password):
    return json.dumps(
        {"username": username, "password": base64.b64encode(password).decode("ascii")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class MacInternetPassword:
    """Match the SecItem contract used by Zed's gpui_macos backend."""

    def __init__(self):
        if sys.platform != "darwin":
            raise SetupError("This credential adapter requires macOS.")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        p, n = C.c_void_p, C.c_long
        signatures = {
            "CFStringCreateWithCString": (p, [p, C.c_char_p, C.c_uint32]),
            "CFDataCreate": (p, [p, p, n]),
            "CFDictionaryCreate": (p, [p, C.POINTER(p), C.POINTER(p), n, p, p]),
            "CFDictionaryGetValue": (p, [p, p]),
            "CFStringGetLength": (n, [p]),
            "CFStringGetMaximumSizeForEncoding": (n, [n, C.c_uint32]),
            "CFStringGetCString": (C.c_bool, [p, p, n, C.c_uint32]),
            "CFDataGetLength": (n, [p]),
            "CFDataGetBytePtr": (p, [p]),
            "CFGetTypeID": (C.c_ulong, [p]),
            "CFDictionaryGetTypeID": (C.c_ulong, []),
            "CFArrayGetTypeID": (C.c_ulong, []),
            "CFArrayGetCount": (n, [p]),
            "CFArrayGetValueAtIndex": (p, [p, n]),
            "CFStringGetTypeID": (C.c_ulong, []),
            "CFDataGetTypeID": (C.c_ulong, []),
            "CFRelease": (None, [p]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.cf, name)
            fn.restype = result
            fn.argtypes = args
        for name, args in {
            "SecItemCopyMatching": [p, C.POINTER(p)],
            "SecItemUpdate": [p, p],
            "SecItemAdd": [p, p],
            "SecItemDelete": [p],
        }.items():
            fn = getattr(self.sec, name)
            fn.restype = C.c_int32
            fn.argtypes = args

    def constant(self, name):
        return C.c_void_p.in_dll(self.cf if name.startswith("kCF") else self.sec, name).value

    def string(self, text):
        result = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)
        if not result:
            raise SetupError("Cannot allocate a credential attribute.")
        return result

    def dictionary(self, pairs):
        keys = (C.c_void_p * len(pairs))(*(self.constant(k) for k, _ in pairs))
        values = (C.c_void_p * len(pairs))(*(v for _, v in pairs))
        result = self.cf.CFDictionaryCreate(
            None,
            keys,
            values,
            len(pairs),
            C.addressof(C.c_byte.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")),
            C.addressof(C.c_byte.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")),
        )
        if not result:
            raise SetupError("Cannot allocate a credential query.")
        return result

    def check(self, status):
        if status:
            raise SetupError(
                "macOS Keychain denied or failed the credential operation (status "
                + str(status)
                + "). No access controls were changed."
            )

    def base_query(self, server, owned):
        url = self.string(server)
        owned.append(url)
        return [("kSecClass", self.constant("kSecClassInternetPassword")), ("kSecAttrServer", url)]

    def read(self, server):
        owned = []
        try:
            base = self.base_query(server, owned)
            query = self.dictionary(
                base
                + [
                    ("kSecReturnAttributes", self.constant("kCFBooleanTrue")),
                    ("kSecMatchLimit", self.constant("kSecMatchLimitAll")),
                    # Preview must not quietly treat a denied read as "no old key".
                    ("kSecUseAuthenticationUI", self.constant("kSecUseAuthenticationUIFail")),
                ]
            )
            owned.append(query)
            result = C.c_void_p()
            status = self.sec.SecItemCopyMatching(query, C.byref(result))
            if status == -25300:
                return None
            self.check(status)
            owned.append(result.value)
            if (
                self.cf.CFGetTypeID(result) != self.cf.CFArrayGetTypeID()
                or self.cf.CFArrayGetCount(result) != 1
            ):
                raise SetupError(
                    "Multiple or unrecognized Keychain entries exist for this API URL. No credentials were changed."
                )
            found = self.cf.CFArrayGetValueAtIndex(result, 0)
            if self.cf.CFGetTypeID(found) != self.cf.CFDictionaryGetTypeID():
                raise SetupError("Unexpected Keychain result type.")
            account = self.cf.CFDictionaryGetValue(found, self.constant("kSecAttrAccount"))
            if not account or self.cf.CFGetTypeID(account) != self.cf.CFStringGetTypeID():
                raise SetupError("Unexpected Keychain credential format.")
            secret_query = self.dictionary(
                [(k, v) for k, v in base if k != "kSecAttrAccount"]
                + [
                    ("kSecAttrAccount", account),
                    ("kSecReturnData", self.constant("kCFBooleanTrue")),
                    ("kSecMatchLimit", self.constant("kSecMatchLimitOne")),
                    ("kSecUseAuthenticationUI", self.constant("kSecUseAuthenticationUIFail")),
                ]
            )
            owned.append(secret_query)
            secret_result = C.c_void_p()
            self.check(self.sec.SecItemCopyMatching(secret_query, C.byref(secret_result)))
            data = secret_result.value
            owned.append(data)
            if not data or self.cf.CFGetTypeID(data) != self.cf.CFDataGetTypeID():
                raise SetupError("Unexpected Keychain password format.")
            size = (
                self.cf.CFStringGetMaximumSizeForEncoding(
                    self.cf.CFStringGetLength(account), 0x08000100
                )
                + 1
            )
            if size > 65536 or self.cf.CFDataGetLength(data) > 65536:
                raise SetupError("Credential exceeds the supported size.")
            buffer = C.create_string_buffer(size)
            if not self.cf.CFStringGetCString(account, buffer, size, 0x08000100):
                raise SetupError("Cannot read the Keychain account name.")
            return encode(
                buffer.value.decode("utf-8"),
                C.string_at(self.cf.CFDataGetBytePtr(data), self.cf.CFDataGetLength(data)),
            )
        finally:
            for item in reversed(owned):
                self.cf.CFRelease(item)

    def write(self, server, value):
        owned = []
        try:
            base = self.base_query(server, owned)
            query = self.dictionary(base)
            owned.append(query)
            if value is None:
                status = self.sec.SecItemDelete(query)
                if status != -25300:
                    self.check(status)
                return
            try:
                doc = json.loads(value)
                password = base64.b64decode(doc["password"], validate=True)
                if not isinstance(doc["username"], str) or len(password) > 65536:
                    raise ValueError()
            except Exception:
                raise SetupError("Invalid credential backup format.") from None
            account = self.string(doc["username"])
            owned.append(account)
            data = self.cf.CFDataCreate(None, password, len(password))
            owned.append(data)
            update = self.dictionary([("kSecAttrAccount", account), ("kSecValueData", data)])
            owned.append(update)
            status = self.sec.SecItemUpdate(query, update)
            if status == -25300:
                create = self.dictionary(
                    [(k, v) for k, v in base if k != "kSecAttrAccount"]
                    + [("kSecAttrAccount", account), ("kSecValueData", data)]
                )
                owned.append(create)
                status = self.sec.SecItemAdd(create, None)
            self.check(status)
        finally:
            for item in reversed(owned):
                if item:
                    self.cf.CFRelease(item)


class MacGenericPassword(MacInternetPassword):
    """Generic Password contract used by Jan's Rust keyring crate."""

    def __init__(self, account):
        super().__init__()
        self.account = account

    def base_query(self, service, owned):
        name = self.string(service)
        owned.append(name)
        account = self.string(self.account)
        owned.append(account)
        return [
            ("kSecClass", self.constant("kSecClassGenericPassword")),
            ("kSecAttrService", name),
            ("kSecAttrAccount", account),
        ]

    def write(self, service, value):
        if value is not None:
            try:
                if json.loads(value)["username"] != self.account:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise SetupError("Credential backup does not match the selected account.") from None
        super().write(service, value)


JAN_RESOURCE = {
    "kind": "macos-generic-password",
    "service": "jan-providers",
    "account": "CometAPI Connect",
    "app": "jan",
}
JAN_WINDOWS_RESOURCE = {
    "kind": "windows-generic-password",
    "service": "jan-providers",
    "account": "CometAPI Connect",
    "app": "jan",
}
ZED_WINDOWS_RESOURCE = {"kind": "windows-zed-password", "server": BASE_URL, "app": "zed"}


def validate(resource):
    if resource not in (
        {"kind": "macos-internet-password", "server": BASE_URL, "app": "zed"},
        JAN_RESOURCE,
        JAN_WINDOWS_RESOURCE,
        ZED_WINDOWS_RESOURCE,
    ):
        raise SetupError("Unrecognized credential transaction target.")


def read(resource):
    validate(resource)
    if resource == ZED_WINDOWS_RESOURCE:
        return WindowsZedPassword("Bearer").read("zed:url=" + resource["server"])
    if resource == JAN_WINDOWS_RESOURCE:
        return WindowsGenericPassword(resource["account"]).read(
            resource["account"] + "." + resource["service"]
        )
    if resource == JAN_RESOURCE:
        return MacGenericPassword(resource["account"]).read(resource["service"])
    return MacInternetPassword().read(resource["server"])


def write(resource, value):
    validate(resource)
    if resource == ZED_WINDOWS_RESOURCE:
        return WindowsZedPassword("Bearer").write("zed:url=" + resource["server"], value)
    if resource == JAN_WINDOWS_RESOURCE:
        return WindowsGenericPassword(resource["account"]).write(
            resource["account"] + "." + resource["service"], value
        )
    if resource == JAN_RESOURCE:
        return MacGenericPassword(resource["account"]).write(resource["service"], value)
    MacInternetPassword().write(resource["server"], value)
