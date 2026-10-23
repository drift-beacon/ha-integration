"""The hub's TLS key fingerprint, computed as the hub computes it."""

from __future__ import annotations

import hashlib
import ssl

from custom_components.drift_beacon.pinning import spki_sha256

# Two P-256 certificates for driftbeacon.local with one key, as the hub's renewal makes them:
# the second differs only in its serial number and validity.
CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIByDCCAW6gAwIBAgIUUpFXjoM924ejbtyuywv5EifOPyswCgYIKoZIzj0EAwIw
HDEaMBgGA1UEAwwRZHJpZnRiZWFjb24ubG9jYWwwHhcNMjYxMDIyMjEwNDQ1WhcN
MjgxMjMxMjEwNDQ1WjAcMRowGAYDVQQDDBFkcmlmdGJlYWNvbi5sb2NhbDBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABNcYODQW+aW845C1epi/bHfs1ZwTCCRt/c9S
Yd0iX5DZBT3TOrzLK95rjm+z/wXRoYb9nXh+zBkGwzdsXQ7qRw+jgY0wgYowHQYD
VR0OBBYEFDnVMDKoUK6el5yHbi6R3XfztZtlMB8GA1UdIwQYMBaAFDnVMDKoUK6e
l5yHbi6R3XfztZtlMA8GA1UdEwEB/wQFMAMBAf8wIgYDVR0RBBswGYIRZHJpZnRi
ZWFjb24ubG9jYWyHBH8AAAEwEwYDVR0lBAwwCgYIKwYBBQUHAwEwCgYIKoZIzj0E
AwIDSAAwRQIhAIRpIPC8fek5D65dJhZYWY64VjFFGO2hBcNNoOsb8OqEAiBQlNhE
/mQLIwTLogtOa7M/EnhGcDDJduedAAG1XNByOQ==
-----END CERTIFICATE-----
"""
RENEWED = """-----BEGIN CERTIFICATE-----
MIIByDCCAW6gAwIBAgIUPb7fV2gpGv21S190KK3x3d9lX44wCgYIKoZIzj0EAwIw
HDEaMBgGA1UEAwwRZHJpZnRiZWFjb24ubG9jYWwwHhcNMjYxMDIyMjEwNDQ1WhcN
MjkwMTAxMjEwNDQ1WjAcMRowGAYDVQQDDBFkcmlmdGJlYWNvbi5sb2NhbDBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABNcYODQW+aW845C1epi/bHfs1ZwTCCRt/c9S
Yd0iX5DZBT3TOrzLK95rjm+z/wXRoYb9nXh+zBkGwzdsXQ7qRw+jgY0wgYowHQYD
VR0OBBYEFDnVMDKoUK6el5yHbi6R3XfztZtlMB8GA1UdIwQYMBaAFDnVMDKoUK6e
l5yHbi6R3XfztZtlMA8GA1UdEwEB/wQFMAMBAf8wIgYDVR0RBBswGYIRZHJpZnRi
ZWFjb24ubG9jYWyHBH8AAAEwEwYDVR0lBAwwCgYIKwYBBQUHAwEwCgYIKoZIzj0E
AwIDSAAwRQIhAJ4Vc+ywNoBM40B+UqGRLrDCOoj2Az7XMx3727MXRtuvAiB0kSZK
jMo0+tan9+9HBmgovEBdLDQEJz2PKdi6VoyRdA==
-----END CERTIFICATE-----
"""
# What the hub's `spkiSha256` (Node's `publicKey.export({type: "spki", format: "der"})`, then
# SHA-256 in base64) and `openssl pkey -pubin -outform der | openssl dgst -sha256` both give.
PIN = "kUN46OEDB0UFdufCAXq7cuGSngqtorakyt+CmtJEKoY="


def test_the_pin_is_the_hubs() -> None:
    """SHA-256 of the DER SubjectPublicKeyInfo, in padded standard base64."""
    assert spki_sha256(ssl.PEM_cert_to_DER_cert(CERTIFICATE)) == PIN


def test_a_renewed_certificate_keeps_its_pin() -> None:
    """The hub renews with its key, so the pin holds where a whole-certificate hash breaks."""
    first = ssl.PEM_cert_to_DER_cert(CERTIFICATE)
    renewed = ssl.PEM_cert_to_DER_cert(RENEWED)
    assert hashlib.sha256(first).digest() != hashlib.sha256(renewed).digest()
    assert spki_sha256(renewed) == PIN
