"""Aiper cloud API request/response envelope.

The cloud uses a dual-layer envelope:

  1. AES-128-CBC with a session key + IV (zero-byte padded), wrapped in
     `{"data": "<b64>"}`.
  2. The AES key + IV are sent in an `encryptKey` HTTP header, RSA-PKCS1
     encrypted with one of two public keys (international vs Chinese region).

The Aiper Android app generates the session key + IV at process startup
(`kotlin.random.Random.Default` over byte values 0x28-0x7e — see
`com.aiper.base.data.http.EncryptInterceptor.<clinit>`). We mirror that:
each `AiperCrypto` instance gets its own fresh pair, valid for the
lifetime of the integration's coordinator. The server doesn't pin specific
keys — it just decrypts whatever we send in `encryptKey`.

The two RSA public keys are byte-identical between v2.3.7 and v3.3.0 of
the app — if Aiper ever rotates them this module is the only place to
update.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

# DER-encoded SubjectPublicKeyInfo for the two regional servers.
RSA_PUBKEY_INTERNATIONAL_B64: Final[str] = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCIKoKPqwq1f60hm/2lpHDF/DT4J9YaptuTq78"
    "nsxdgnSBAvkIZ3E8dqbEBT/VETjJ9Yr28QtHX13E8QGByYxLzYPldHNXChgOWfSemTEC3TxPvla"
    "SuM9eFUuhqSeGbgoKG7JJNlgjvsPO2cHEhPXJE4qWtKEZVOZBxEeCgAaLZxwIDAQAB"
)
RSA_PUBKEY_CHINESE_B64: Final[str] = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAt589p8rBP5tTCv2/mKF36yvIp2adSch"
    "prw+0FQOjF6SNwCNyQsqMXiJ1dmAQlIPX8k34OEPNek5jUk99+SrX77JWYCpj4b//TGZE0eeVQG"
    "YFZdCX44Un/xKJeEfJV7cZdGnlFN/1up/ujE8Pz8DDc45SnINHs0LmiAHnnZGKzg78FSFIQktiV"
    "GHFopQox4w+eSAVnoZVYxTsM0IqSUfkObRGvjf+8AvE8ylx3+t4GmmwvFzh0iCyV+wJuPGkyEyr"
    "9AndSm+2pqtga7lq2a/MZWJhAtyqkZSSCOHOzCSuqeFaR7hikwswRza1UVQih6m6rzsbcyhXgyr"
    "1sY2ICabE1wIDAQAB"
)

# The app uses `kotlin.random.Random.Default` over IntRange(40, 126) — i.e. printable
# ASCII bytes 0x28-0x7e — to fill 16-byte AES key + IV.
_KEY_BYTE_LOW: Final[int] = 0x28
_KEY_BYTE_HIGH: Final[int] = 0x7E

_NONCE_ALPHABET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-_=+[]{}"
)


def _random_key_bytes(length: int = 16) -> bytes:
    """`length` random bytes, each in the inclusive range [0x28, 0x7e]."""
    return bytes(secrets.choice(range(_KEY_BYTE_LOW, _KEY_BYTE_HIGH + 1)) for _ in range(length))


def _generate_nonce(length: int = 4) -> str:
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(length))


def _generate_request_id_key(length: int = 16) -> str:
    alphabet = _NONCE_ALPHABET + "/"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _zero_pad(data: bytes, block_size: int = 16) -> bytes:
    """ZeroBytePadding (Aiper's chosen scheme — NOT PKCS7)."""
    remainder = len(data) % block_size
    if remainder == 0:
        return data
    return data + b"\x00" * (block_size - remainder)


def _zero_unpad(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


class AiperCrypto:
    """Holds one session's AES key + IV.

    A coordinator instantiates this once and reuses it for the entire HA
    session. Calling code does not need to know the key — request/response
    helpers handle that internally.
    """

    def __init__(
        self,
        *,
        region: str = "international",
        aes_key: bytes | None = None,
        aes_iv: bytes | None = None,
    ) -> None:
        if region not in ("international", "chinese"):
            raise ValueError(f"unknown region: {region}")
        self.region = region
        self.aes_key = aes_key or _random_key_bytes(16)
        self.aes_iv = aes_iv or _random_key_bytes(16)
        if len(self.aes_key) != 16 or len(self.aes_iv) != 16:
            raise ValueError("AES key and IV must each be 16 bytes")

        der = base64.b64decode(
            RSA_PUBKEY_CHINESE_B64 if region == "chinese" else RSA_PUBKEY_INTERNATIONAL_B64
        )
        self._rsa_pub = load_der_public_key(der)

    def _aes_encrypt(self, plaintext: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_iv))
        enc = cipher.encryptor()
        return enc.update(_zero_pad(plaintext)) + enc.finalize()

    def _aes_decrypt(self, ciphertext: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_iv))
        dec = cipher.decryptor()
        return _zero_unpad(dec.update(ciphertext) + dec.finalize())

    def _build_encrypt_key_header(self) -> str:
        payload = json.dumps(
            {"key": self.aes_key.decode("ascii"), "iv": self.aes_iv.decode("ascii")},
            separators=(",", ":"),
        ).encode("utf-8")
        encrypted = self._rsa_pub.encrypt(payload, padding.PKCS1v15())  # type: ignore[union-attr]
        return base64.b64encode(encrypted).decode("ascii")

    def encrypt_request(self, payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Wrap `payload` in Aiper's envelope.

        Returns (body, headers):
          * body — JSON string `{"data": "<base64-ciphertext>"}`
          * headers — `encryptKey` + `requestIdKey` to merge into request headers
            (caller adds `Content-Type`, `token`, `User-Agent`, …).
        """
        enriched = {
            **payload,
            "nonce": _generate_nonce(),
            "timestamp": int(time.time() * 1000),
        }
        plaintext = json.dumps(enriched, separators=(",", ":")).encode("utf-8")
        ciphertext = self._aes_encrypt(plaintext)
        body = json.dumps(
            {"data": base64.b64encode(ciphertext).decode("ascii")}, separators=(",", ":")
        )
        return body, {
            "encryptKey": self._build_encrypt_key_header(),
            "requestIdKey": _generate_request_id_key(),
        }

    def decrypt_response(self, body: str | bytes) -> Any:
        """Decrypt a server response. Falls through to plain JSON for error envelopes."""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        body = body.strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
        plaintext = self._aes_decrypt(base64.b64decode(body))
        return json.loads(plaintext.decode("utf-8"))
