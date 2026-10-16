"""Local TLS identity and portable room invitations. Never export private keys."""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import ssl


def validate_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter an IP address or DNS name.")
    value = value.strip().lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
            raise ValueError("Enter an IP address or DNS name without https:// or a port.")
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
            raise ValueError("Enter a valid DNS name.")
        return value


def host_identity(directory: Path, hostname: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hostname = validate_host(hostname)
    directory = Path(directory) / hashlib.sha256(hostname.encode()).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / 'certificate.pem', directory / 'private-key.pem'
    now = dt.datetime.now(dt.timezone.utc)
    if cert_path.is_file() and key_path.is_file():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if cert.not_valid_after_utc > now + dt.timedelta(days=7):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            return cert_path, key_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
             x509.IPAddress(ipaddress.ip_address('::1'))]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(hostname)))
    except ValueError:
        names.append(x509.DNSName(hostname))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'SEED Destiny private room')])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def make_invitation(host: str, port: int, room: str, certificate: str = '', tls: bool = True) -> str:
    data = {'format': 'seed-room-v1', 'host': validate_host(host), 'port': port,
            'room': room, 'tls': tls, 'certificate': certificate}
    # Apply the same validation on both sides of a shared invitation.
    text = json.dumps(data, indent=2)
    parse_invitation(text)
    return text


def parse_invitation(text: str) -> dict:
    if len(text) > 16384:
        raise ValueError('This invitation is too large.')
    data = json.loads(text)
    if not isinstance(data, dict) or data.get('format') != 'seed-room-v1':
        raise ValueError('This is not a SEED Destiny room invitation.')
    host = validate_host(data.get('host', ''))
    port = data.get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid room server port.')
    if not isinstance(data.get('room'), str) or not re.fullmatch(r'[A-Z0-9]{4,12}', data['room']):
        raise ValueError('Invalid room code.')
    tls = data.get('tls')
    if type(tls) is not bool or (not tls and host not in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('Internet invitations must use encryption.')
    cert = data.get('certificate', '')
    if not isinstance(cert, str) or len(cert) > 8192 or ('PRIVATE KEY' in cert):
        raise ValueError('Invalid public server certificate.')
    if cert:
        if cert.count('-----BEGIN CERTIFICATE-----') != 1 or cert.count('-----END CERTIFICATE-----') != 1:
            raise ValueError('An invitation must contain one public certificate.')
        ssl.create_default_context(cadata=cert)
    return {'host': host, 'port': port, 'room': data['room'], 'tls': tls, 'certificate': cert}


def import_invitation(text: str, directory: Path) -> dict:
    data = parse_invitation(text)
    data['ca'] = ''
    if data['certificate']:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (hashlib.sha256(data['certificate'].encode()).hexdigest() + '.pem')
        path.write_text(data['certificate'], encoding='ascii')
        data['ca'] = str(path)
    return data
