"""SMART Backend Services client (client_credentials with a signed JWT assertion).

The client proves who it is with a private key that never leaves this service. It asks only for the
scopes the field map needs, and it reuses a token until shortly before it expires.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
PRIVATE_KEY_FILE = "client_private.pem"
PUBLIC_KEY_FILE = "client_public.pem"
KEY_ID = "recall-pipeline-1"


class TokenError(RuntimeError):
    pass


def ensure_keypair(directory: Path) -> Path:
    """Create the client's RSA key pair if it doesn't exist. Returns the private key path.

    In production the public half is registered with the EHR's app registration (as a JWKS); here the
    demo gateway reads it from a shared volume.
    """
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / PRIVATE_KEY_FILE
    if private_path.exists():
        return private_path
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    (directory / PUBLIC_KEY_FILE).write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    try:
        private_path.chmod(0o600)
    except OSError:
        pass
    return private_path


@dataclass
class TokenInfo:
    access_token: str
    scope: str
    expires_at: float
    issued_at: float

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "issued_at": int(self.issued_at),
            "expires_at": int(self.expires_at),
            "lifetime_seconds": int(self.expires_at - self.issued_at),
        }


class SmartBackendClient:
    def __init__(self, token_url: str, client_id: str, private_key_pem: bytes, scopes: str,
                 http: httpx.Client, refresh_margin: float = 30.0, clock=time.time):
        self.token_url = token_url
        self.client_id = client_id
        self.private_key_pem = private_key_pem
        self.scopes = scopes
        self.http = http
        self.refresh_margin = refresh_margin
        self.clock = clock
        self.current: TokenInfo | None = None
        self.tokens_issued = 0

    def assertion(self) -> str:
        now = int(self.clock())
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "exp": now + 300,
            "iat": now,
            "jti": str(uuid.uuid4()),
        }
        return jwt.encode(claims, self.private_key_pem, algorithm="RS384", headers={"kid": KEY_ID})

    def token(self) -> str:
        now = self.clock()
        if self.current and now < self.current.expires_at - self.refresh_margin:
            return self.current.access_token
        response = self.http.post(self.token_url, data={
            "grant_type": "client_credentials",
            "scope": self.scopes,
            "client_assertion_type": ASSERTION_TYPE,
            "client_assertion": self.assertion(),
        })
        if response.status_code != 200:
            raise TokenError(f"token endpoint returned {response.status_code}: {response.text[:200]}")
        body = response.json()
        self.current = TokenInfo(access_token=body["access_token"], scope=body.get("scope", ""),
                                 expires_at=now + int(body.get("expires_in", 300)), issued_at=now)
        self.tokens_issued += 1
        return self.current.access_token
