"""Segunda capa: firma Authenticode de todo PE del payload (PLAN-V2 §1.6).

La protección principal es TUF (hash y longitud firmados). Esto comprueba además que cada `.exe`, `.dll` y
`.pyd` nuevo lleva una firma válida de **nuestra** empresa:
- `WinVerifyTrust` (cadena hasta una raíz de confianza de Windows, uso «firma de código» y sello de tiempo
  si el certificado ya caducó);
- **sujeto** `O=<razón social>` y `C=ES` y **emisor** en la lista corta del descriptor (firmado por TUF);
- **no** se fija la huella: un certificado renovado (misma empresa, otra huella) sigue valiendo, y una vuelta
  atrás a una versión firmada con el certificado anterior también (sello de tiempo RFC 3161);
- se exige sello de tiempo.

Archivos de terceros que ya vienen firmados por su autor (p. ej. `python.exe` de la PSF o `vcruntime140.dll`
de Microsoft en `runtime\\`) se aceptan si su sujeto está en `third_party` del descriptor.

Sin certificado todavía (decisión N1): un descriptor sin `authenticode` es una versión sin firmar y solo se
acepta con un `root` de **desarrollo** (`"x-vms-env": "dev"`).
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("vms_updater.authenticode")

PE_SUFFIXES = {".exe", ".dll", ".pyd", ".sys"}
DEFAULT_THIRD_PARTY = ("Python Software Foundation", "Microsoft Corporation")


@dataclass(frozen=True)
class SignatureInfo:
    trusted: bool              # WinVerifyTrust = 0 (cadena, uso y, si caducó, sello de tiempo)
    subject_o: str
    subject_c: str
    issuer_cn: str
    thumbprint_sha256: str
    timestamped: bool
    error: str = ""


class AuthenticodeError(Exception):
    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


def is_pe(path: Path) -> bool:
    if path.suffix.lower() not in PE_SUFFIXES:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def check_policy(info: SignatureInfo | None, *, subject_o: str, subject_c: str, issuers: Iterable[str],
                 third_party: Iterable[str] = (), rel: str = "") -> None:
    """Lanza `AuthenticodeError` si la firma no cumple la política. Nunca mira la huella."""
    if info is None:
        raise AuthenticodeError(f"{rel}: sin firma Authenticode")
    if not info.trusted:
        raise AuthenticodeError(f"{rel}: firma no válida ({info.error or 'WinVerifyTrust la rechaza'})")
    if not info.timestamped:
        raise AuthenticodeError(f"{rel}: la firma no lleva sello de tiempo")
    if info.subject_o == subject_o and info.subject_c.upper() == subject_c.upper():
        if info.issuer_cn not in set(issuers):
            raise AuthenticodeError(f"{rel}: emisor «{info.issuer_cn}» no admitido")
        return
    if info.subject_o and info.subject_o in set(third_party):
        return
    raise AuthenticodeError(f"{rel}: firmado por «{info.subject_o or '?'}», no por «{subject_o}»")


Verifier = Callable[[Path], "SignatureInfo | None"]


def verify_tree(root: Path, files: Iterable[str], verifier: Verifier, *, subject_o: str, subject_c: str,
                issuers: Iterable[str], third_party: Iterable[str] = DEFAULT_THIRD_PARTY) -> int:
    """Comprueba los PE de `files` (rutas relativas a `root`). Devuelve cuántos comprobó."""
    n = 0
    issuers = list(issuers)
    third = list(third_party)
    for rel in files:
        p = root / rel
        if not is_pe(p):
            continue
        check_policy(verifier(p), subject_o=subject_o, subject_c=subject_c, issuers=issuers, third_party=third,
                     rel=rel)
        n += 1
    return n


def system_verifier() -> Verifier | None:
    """El verificador real (solo Windows). En macOS/Linux de desarrollo no hay: devuelve None."""
    if sys.platform != "win32":
        return None
    return _win_verify


# --------------------------------------------------------------------------- Windows (ctypes)
def _win_verify(path: Path) -> SignatureInfo | None:  # pragma: no cover - solo Windows (job B4 de CI)
    import ctypes
    import hashlib
    from ctypes import wintypes

    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(GUID))]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    class ALGID(ctypes.Structure):
        _fields_ = [("pszObjId", ctypes.c_char_p), ("Parameters", BLOB)]

    class CRYPT_ATTRIBUTE(ctypes.Structure):
        _fields_ = [("pszObjId", ctypes.c_char_p), ("cValue", wintypes.DWORD), ("rgValue", ctypes.POINTER(BLOB))]

    class CRYPT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("cAttr", wintypes.DWORD), ("rgAttr", ctypes.POINTER(CRYPT_ATTRIBUTE))]

    class CMSG_SIGNER_INFO(ctypes.Structure):
        _fields_ = [("dwVersion", wintypes.DWORD), ("Issuer", BLOB), ("SerialNumber", BLOB),
                    ("HashAlgorithm", ALGID), ("HashEncryptionAlgorithm", ALGID), ("EncryptedHash", BLOB),
                    ("AuthAttrs", CRYPT_ATTRIBUTES), ("UnauthAttrs", CRYPT_ATTRIBUTES)]

    class BITBLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
                    ("cUnusedBits", wintypes.DWORD)]

    class PUBKEYINFO(ctypes.Structure):
        _fields_ = [("Algorithm", ALGID), ("PublicKey", BITBLOB)]

    class CERT_INFO(ctypes.Structure):
        _fields_ = [("dwVersion", wintypes.DWORD), ("SerialNumber", BLOB), ("SignatureAlgorithm", ALGID),
                    ("Issuer", BLOB), ("NotBefore", wintypes.FILETIME), ("NotAfter", wintypes.FILETIME),
                    ("Subject", BLOB), ("SubjectPublicKeyInfo", PUBKEYINFO), ("IssuerUniqueId", BITBLOB),
                    ("SubjectUniqueId", BITBLOB), ("cExtension", wintypes.DWORD), ("rgExtension", ctypes.c_void_p)]

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [("dwCertEncodingType", wintypes.DWORD), ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
                    ("cbCertEncoded", wintypes.DWORD), ("pCertInfo", ctypes.POINTER(CERT_INFO)),
                    ("hCertStore", wintypes.HANDLE)]

    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    finfo = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = 2            # WTD_UI_NONE
    data.fdwRevocationChecks = 0   # WTD_REVOKE_NONE (las tiendas pueden no tener Internet; TUF manda)
    data.dwUnionChoice = 1         # WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(finfo)
    data.dwStateAction = 1         # WTD_STATEACTION_VERIFY
    data.dwProvFlags = 0x1000 | 0x10   # WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE
    wvt = wintrust.WinVerifyTrust
    wvt.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    wvt.restype = ctypes.c_long
    status = wvt(None, ctypes.byref(action), ctypes.byref(data))
    data.dwStateAction = 2         # WTD_STATEACTION_CLOSE
    wvt(None, ctypes.byref(action), ctypes.byref(data))
    trusted = status == 0
    error = "" if trusted else f"WinVerifyTrust 0x{status & 0xFFFFFFFF:08X}"

    enc = wintypes.DWORD()
    ctype = wintypes.DWORD()
    fmt = wintypes.DWORD()
    store = wintypes.HANDLE()
    msg = wintypes.HANDLE()
    cqo = crypt32.CryptQueryObject
    cqo.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    cqo.restype = wintypes.BOOL
    ok = cqo(1, ctypes.c_wchar_p(str(path)), 1 << 10, 2, 0, ctypes.byref(enc), ctypes.byref(ctype),
             ctypes.byref(fmt), ctypes.byref(store), ctypes.byref(msg), None)
    if not ok:      # sin firma incrustada (TRUST_E_NOSIGNATURE) o PE que no se puede leer
        return None
    try:
        get = crypt32.CryptMsgGetParam
        get.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                        ctypes.POINTER(wintypes.DWORD)]
        get.restype = wintypes.BOOL
        size = wintypes.DWORD()
        get(msg, 6, 0, None, ctypes.byref(size))          # CMSG_SIGNER_INFO_PARAM
        buf = ctypes.create_string_buffer(size.value)
        if not get(msg, 6, 0, buf, ctypes.byref(size)):
            return SignatureInfo(False, "", "", "", "", False, "no se pudo leer el firmante")
        si = ctypes.cast(buf, ctypes.POINTER(CMSG_SIGNER_INFO)).contents
        timestamped = False
        for i in range(si.UnauthAttrs.cAttr):
            oid = (si.UnauthAttrs.rgAttr[i].pszObjId or b"").decode()
            if oid in ("1.2.840.113549.1.9.6", "1.3.6.1.4.1.311.3.3.1"):
                timestamped = True
        ci = CERT_INFO()
        ci.Issuer = si.Issuer
        ci.SerialNumber = si.SerialNumber
        find = crypt32.CertFindCertificateInStore
        find.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                         ctypes.c_void_p]
        find.restype = ctypes.POINTER(CERT_CONTEXT)
        ctx = find(store, 0x00010001, 0, 0x000B0000, ctypes.byref(ci), None)  # CERT_FIND_SUBJECT_CERT
        if not ctx:
            return SignatureInfo(False, "", "", "", "", timestamped, "certificado del firmante no encontrado")
        try:
            name = crypt32.CertGetNameStringW
            name.argtypes = [ctypes.POINTER(CERT_CONTEXT), wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.LPWSTR, wintypes.DWORD]
            name.restype = wintypes.DWORD

            def attr(oid: bytes, issuer: bool = False, kind: int = 3) -> str:
                out = ctypes.create_unicode_buffer(512)
                param = ctypes.c_char_p(oid) if kind == 3 else None
                name(ctx, kind, 1 if issuer else 0, param, out, 512)
                return out.value

            subject_o = attr(b"2.5.4.10")
            subject_c = attr(b"2.5.4.6")
            issuer_cn = attr(b"", issuer=True, kind=4)          # CERT_NAME_SIMPLE_DISPLAY_TYPE
            raw = ctypes.string_at(ctx.contents.pbCertEncoded, ctx.contents.cbCertEncoded)
            thumb = hashlib.sha256(raw).hexdigest()
        finally:
            crypt32.CertFreeCertificateContext(ctx)
        return SignatureInfo(trusted, subject_o, subject_c, issuer_cn, thumb, timestamped, error)
    finally:
        crypt32.CertCloseStore(store, 0)
        crypt32.CryptMsgClose(msg)
