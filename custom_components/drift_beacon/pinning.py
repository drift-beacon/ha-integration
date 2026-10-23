"""The hub's TLS key fingerprint, for pinning it later.

Not used yet: the integration talks to the hub over plain HTTP for v1. After v1, D8 of the
host-network plan pins the hub's HTTPS certificate by this fingerprint: trust on first use,
bound when the user pastes an API key, then compared on every connection over an
``SSLContext``. It hashes the public key, not the whole certificate as ``aiohttp.Fingerprint``
does, because the hub keeps its key when it renews the certificate.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def spki_sha256(certificate_der: bytes) -> str:
    """The certificate's pin, as the hub publishes it (``fp`` in TXT, ``tlsSpkiSha256``).

    SHA-256 over the DER SubjectPublicKeyInfo, in padded standard base64: HPKP's
    ``pin-sha256``. ``certificate_der`` is what ``SSLSocket.getpeercert(binary_form=True)``
    returns.
    """
    public_key = x509.load_der_x509_certificate(certificate_der).public_key()
    spki = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")
