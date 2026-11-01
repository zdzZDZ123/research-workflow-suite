"""Per-user Windows Credential Manager, with environment-variable override."""
import ctypes
import os
from ctypes import wintypes

TARGET = 'PaperDeepReading/Notion'
MODEL_TARGET = 'PaperRelations/ModelAPI'


class Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def save_secret(target, token):
    if os.name != 'nt': raise RuntimeError('Windows Credential Manager required')
    if not token or '\n' in token or '\r' in token or len(token) >= 4096:
        raise ValueError('Invalid secret')
    raw = token.encode('utf-16-le')
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = Credential(Type=1, TargetName=target, Comment='Local research integration',
                            CredentialBlobSize=len(raw), CredentialBlob=buffer, Persist=2, UserName='Local integration')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    if not api.CredWriteW(ctypes.byref(credential), 0): raise ctypes.WinError(ctypes.get_last_error())


def read_secret(target):
    if os.name != 'nt': return None
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168: return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        credential = pointer.contents
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode('utf-16-le')
    finally: api.CredFree(pointer)


def save_token(token):
    if not token.startswith(('ntn_', 'secret_')) or not 20 < len(token) < 1000:
        raise ValueError('Invalid Notion token format')
    save_secret(TARGET, token)


def notion_token():
    return os.environ.get('NOTION_TOKEN') or os.environ.get('NOTION_API_KEY') or read_secret(TARGET)


def model_api_key():
    return os.environ.get('PAPER_RELATIONS_API_KEY') or read_secret(MODEL_TARGET)
