"""AES-CBC/PKCS7 for existing application credential formats.

macOS uses Apple's system CommonCrypto, including Intel Macs. Other platforms
use cryptography. Neither backend changes the apps' ciphertext formats.
"""

import sys


def aes_cbc(data, key, iv, decrypt=False):
    if sys.platform == "darwin":
        import ctypes

        # CommonCryptor.h: AES=0, decrypt=1, PKCS7 padding=1.
        library = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
        operation = library.CCCrypt
        operation.restype = ctypes.c_int
        operation.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        output = ctypes.create_string_buffer(len(data) + 16)
        written = ctypes.c_size_t()
        status = operation(
            int(decrypt),
            0,
            1,
            ctypes.create_string_buffer(key),
            len(key),
            ctypes.create_string_buffer(iv),
            ctypes.create_string_buffer(data),
            len(data),
            output,
            len(output),
            ctypes.byref(written),
        )
        if status != 0:
            raise ValueError("AES-CBC operation failed.")
        return output.raw[: written.value]

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    if decrypt:
        worker = cipher.decryptor()
        padded = worker.update(data) + worker.finalize()
        unpad = padding.PKCS7(128).unpadder()
        return unpad.update(padded) + unpad.finalize()
    pad = padding.PKCS7(128).padder()
    padded = pad.update(data) + pad.finalize()
    worker = cipher.encryptor()
    return worker.update(padded) + worker.finalize()
