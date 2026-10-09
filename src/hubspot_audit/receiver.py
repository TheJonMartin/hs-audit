"""Client side of the hosted OAuth receiver: signed install state, token fetch and decrypt."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .errors import AuthError

KEY_BITS = 3072
STATE_SIGNATURE_HEX_LENGTH = 32
FETCH_TIMEOUT_SECONDS = 15
DEFAULT_KEY_PATH = Path.home() / ".hubspot-audit" / "token_private.pem"
PRIVATE_KEY_MODE = 0o600


def sign_nonce(nonce: str, signing_secret: str) -> str:
    """Signature half of a state value; must match ``signNonce`` in the Netlify function."""
    digest = hmac.new(signing_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return digest[:STATE_SIGNATURE_HEX_LENGTH]


def new_signed_state(signing_secret: str) -> str:
    """Create ``<nonce>.<signature>``. The nonce is also the key the token is stored under."""
    if not signing_secret:
        raise AuthError("STATE_SIGNING_SECRET is required for the hosted install flow.")
    nonce = secrets.token_urlsafe(24)
    return f"{nonce}.{sign_nonce(nonce, signing_secret)}"


def generate_keypair(private_key_path: Path = DEFAULT_KEY_PATH) -> str:
    """Create the RSA keypair. Writes the private key (mode 600) and returns the public PEM.

    Raises:
        FileExistsError: a key already exists at the path (never overwritten).
    """
    if private_key_path.exists():
        raise FileExistsError(f"{private_key_path} already exists; refusing to overwrite.")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(private_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_KEY_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def decrypt_ciphertext(ciphertext_b64: str, private_key_path: Path) -> dict:
    """Decrypt an RSA-OAEP (SHA-256) payload produced by the Netlify function."""
    try:
        key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
        plain = key.decrypt(
            base64.b64decode(ciphertext_b64),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return json.loads(plain)
    except (OSError, ValueError) as exc:
        raise AuthError(f"Could not decrypt the stored token: {type(exc).__name__}") from exc


def fetch_refresh_token(
    receiver_url: str,
    fetch_secret: str,
    state: str,
    private_key_path: Path = DEFAULT_KEY_PATH,
    session: requests.Session | None = None,
) -> dict:
    """Fetch (one time) and decrypt the refresh token stored for ``state``.

    Returns ``{"refresh_token", "hub_id", "scopes"}``. The receiver deletes the token on read,
    so a failed run after this point needs a fresh install.

    Raises:
        AuthError: missing config, HTTP failure (404 not found, 410 expired), or bad decrypt.
    """
    if not receiver_url or not fetch_secret:
        raise AuthError("TOKEN_RECEIVER_URL and TOKEN_FETCH_SECRET are required.")
    http = session or requests.Session()
    try:
        response = http.get(
            receiver_url.rstrip("/") + "/api/token",
            params={"state": state},
            headers={"Authorization": f"Bearer {fetch_secret}"},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise AuthError(f"Token fetch failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        reason = {404: "not found (not installed yet, or already fetched)", 410: "expired"}.get(
            response.status_code, f"HTTP {response.status_code}"
        )
        raise AuthError(f"Token fetch failed: {reason}")
    body = response.json()
    payload = decrypt_ciphertext(body["ciphertext"], private_key_path)
    return {
        "refresh_token": payload["refresh_token"],
        "hub_id": body.get("hub_id"),
        "scopes": body.get("scopes", []),
    }
