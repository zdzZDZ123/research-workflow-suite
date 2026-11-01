"""Independent Windows Credential Manager namespace; never serialize keys."""
import ctypes
import os
from ctypes import wintypes

from .contracts import FormatterError


class Credential(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _target(kind):
    if kind not in ("model", "search"):
        raise FormatterError("INVALID_SECRET_KIND")
    return "PaperFormatter/" + kind


def read_secret(kind):
    target = _target(kind)
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def save_secret(kind, value):
    target = _target(kind)
    if os.name != "nt":
        raise FormatterError("WINDOWS_CREDENTIAL_MANAGER_REQUIRED")
    if not isinstance(value, str) or not value or len(value) > 2000 or any(c in value for c in "\r\n\x00"):
        raise FormatterError("INVALID_SECRET")
    encoded = value.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    record = Credential(Type=1, TargetName=target, CredentialBlobSize=len(encoded), CredentialBlob=buffer,
                        Persist=2, UserName="Paper Formatter", Comment="Independent paper formatting plugin")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    if not api.CredWriteW(ctypes.byref(record), 0):
        raise ctypes.WinError(ctypes.get_last_error())
