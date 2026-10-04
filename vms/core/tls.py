"""Certificado autofirmado para servir la web del VMS por HTTPS en la LAN de la tienda.

    python -m vms tls-cert --host pc-control --ip 192.168.1.20

Crea `<datos>/secrets/tls/vms.crt` y `vms.key` (clave EC P-256, válido 825 días, con el nombre
y las IP indicadas más localhost/127.0.0.1) y dice qué líneas poner en el .env. Para que los
navegadores no avisen, importa `vms.crt` como raíz de confianza en los equipos que abran la web
(docs/RED.md). Si el cliente tiene su propia CA, usa su certificado en lugar de este.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .atomic import atomic_write_bytes
from .paths import restrict_permissions

DEFAULT_DAYS = 825   # máximo que aceptan los navegadores para certificados de servidor


@dataclass(frozen=True)
class TlsFiles:
    cert: Path
    key: Path


def create_self_signed(out_dir: Path, hosts: list[str], ips: list[str], *, days: int = DEFAULT_DAYS) -> TlsFiles:
    """Genera el par certificado/clave. Lanza ValueError si una IP no es válida."""
    names = list(dict.fromkeys([h.strip() for h in hosts if h.strip()] + [socket.gethostname(), "localhost"]))
    addresses = list(dict.fromkeys([ipaddress.ip_address(i.strip()) for i in ips if i.strip()]
                                   + [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VMS Multimarca")])
    now = datetime.now(timezone.utc)
    san: list[x509.GeneralName] = [x509.DNSName(n) for n in names]
    san += [x509.IPAddress(a) for a in addresses]
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    out_dir.mkdir(parents=True, exist_ok=True)
    restrict_permissions(out_dir)
    files = TlsFiles(out_dir / "vms.crt", out_dir / "vms.key")
    atomic_write_bytes(files.key, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    restrict_permissions(files.key)
    atomic_write_bytes(files.cert, cert.public_bytes(serialization.Encoding.PEM))
    return files
