"""
Генератор независимых сертификатов для локального шлюза Ксении.
Создаёт:
- certs/ksenia-ca.crt (Корневой сертификат CA)
- certs/ksenia-ca.key (Приватный ключ CA)
- certs/ksenia-lan.crt (Сертификат сервера для 192.168.0.14, 127.0.0.1, localhost)
- certs/ksenia-lan.key (Приватный ключ сервера)
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

def generate_certs(certs_dir: Path) -> None:
    certs_dir.mkdir(parents=True, exist_ok=True)
    ca_crt_path = certs_dir / "ksenia-ca.crt"
    ca_key_path = certs_dir / "ksenia-ca.key"
    lan_crt_path = certs_dir / "ksenia-lan.crt"
    lan_key_path = certs_dir / "ksenia-lan.key"

    now = datetime.datetime.now(datetime.timezone.utc)
    one_day = datetime.timedelta(days=1)
    ten_years = datetime.timedelta(days=3650)

    # 1. Генерация CA ключа
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ассистент Ксения (Локальная сеть)"),
        x509.NameAttribute(NameOID.COMMON_NAME, "Ksenia Local Root CA"),
    ])

    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - one_day)
        .not_valid_after(now + ten_years)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_key_path.write_bytes(
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    ca_crt_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    print(f"Создан CA: {ca_crt_path}")

    # 2. Генерация серверного ключа и сертификата
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname()
    server_subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ассистент Ксения"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    # Subject Alternative Names (SAN)
    san_names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv4Address("192.168.0.14")),
    ]

    # Добавляем все локальные IPv4 адреса хоста
    try:
        host_ips = socket.gethostbyname_ex(hostname)[2]
        for ip_str in host_ips:
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
                if ip_obj not in [x.value for x in san_names if isinstance(x, x509.IPAddress)]:
                    san_names.append(x509.IPAddress(ip_obj))
            except ValueError:
                pass
    except Exception:
        pass

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - one_day)
        .not_valid_after(now + ten_years)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=False,
                crl_sign=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(san_names),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    lan_key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    lan_crt_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    print(f"Создан серверный сертификат: {lan_crt_path} с SAN: {[str(n.value) for n in san_names]}")

if __name__ == "__main__":
    generate_certs(Path("certs"))
