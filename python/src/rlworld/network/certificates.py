"""Private CA for the fake internet (DESIGN.md section 6.6).

The image trusts one CA per world, so HTTPS to real looking hostnames reaches
the clones unchanged. Validity windows are anchored to the world's fixed
clock, not today's date, because a world frozen at 2026-03-14 must see valid
certificates at that date.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLOCK_SKEW_ALLOWANCE = timedelta(days=1)


@dataclass(frozen=True)
class CertificateAuthority:
    certificate_pem: bytes
    private_key_pem: bytes


def private_key_pem(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def create_certificate_authority(common_name: str, world_time: datetime, valid_days: int = 3650) -> CertificateAuthority:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(world_time - CLOCK_SKEW_ALLOWANCE)
        .not_valid_after(world_time + timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )
    return CertificateAuthority(certificate.public_bytes(serialization.Encoding.PEM), private_key_pem(private_key))


def issue_server_certificate(authority: CertificateAuthority, hostnames: Sequence[str], world_time: datetime,
                             valid_days: int = 397) -> tuple[bytes, bytes]:
    if not hostnames:
        raise ValueError("a server certificate needs at least one hostname")
    authority_certificate = x509.load_pem_x509_certificate(authority.certificate_pem)
    authority_key = serialization.load_pem_private_key(authority.private_key_pem, password=None)
    private_key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .issuer_name(authority_certificate.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(world_time - CLOCK_SKEW_ALLOWANCE)
        .not_valid_after(world_time + timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname) for hostname in hostnames]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), private_key_pem(private_key)
