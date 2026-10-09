"""One-time setup for the hosted OAuth receiver, plus a post-deploy smoke test."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization

from .receiver import DEFAULT_KEY_PATH, generate_keypair
from .scopes import OPTIONAL_SCOPES, REQUIRED_SCOPES

RECEIVER_ENV_FILE = Path(".env.receiver")
LOCAL_ENV_FILE = Path(".env")
APP_CONFIG_PATH = Path("hubspot-app/src/app/app-hsmeta.json")
SECRET_BYTES = 32
FILE_MODE = 0o600
CALLBACK_PATH = "/oauth/callback"
SMOKE_TIMEOUT_SECONDS = 15
# Names written to .env.receiver in this order. Blank values must be filled in by hand.
RECEIVER_VARS = [
    "OAUTH_REDIRECT_URI",
    "STATE_SIGNING_SECRET",
    "TOKEN_FETCH_SECRET",
    "TOKEN_PUBLIC_KEY_PEM",
    "TOKEN_STORE_NAME",
    "HUBSPOT_CLIENT_ID",
    "HUBSPOT_CLIENT_SECRET",
    "ERROR_ROUTER_URL",
    "FLOW_ID",
    "FLOW_NAME",
    "PLATFORM_NAME",
]
# Variables the receiver cannot run without; scripts/netlify_env.sh refuses to deploy if blank.
REQUIRED_BY_RECEIVER = [
    "OAUTH_REDIRECT_URI",
    "STATE_SIGNING_SECRET",
    "TOKEN_FETCH_SECRET",
    "TOKEN_PUBLIC_KEY_PEM",
    "TOKEN_STORE_NAME",
    "HUBSPOT_CLIENT_ID",
    "HUBSPOT_CLIENT_SECRET",
    "ERROR_ROUTER_URL",
]
SHARED_WITH_LOCAL = ["STATE_SIGNING_SECRET", "TOKEN_FETCH_SECRET"]
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(?:'([^']*)'|([^\n]*))$", re.MULTILINE)


class SetupError(Exception):
    """Setup cannot continue without the user fixing something."""


@dataclass
class SetupReport:
    """What setup did. Names only, never values."""

    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    blank_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value and KEY='multi\nline' entries. Returns {} for a missing file."""
    if not path.exists():
        return {}
    values = {}
    for match in ENV_LINE.finditer(path.read_text(encoding="utf-8")):
        values[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write values single-quoted (safe for multi-line PEM) with owner-only permissions."""
    lines = []
    for name, value in values.items():
        if "'" in value:
            raise SetupError(f"{name} contains a single quote, which this file cannot store.")
        lines.append(f"{name}='{value}'")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    path.chmod(FILE_MODE)


def public_pem_for(private_key_path: Path) -> str:
    """Public key PEM for an existing private key file."""
    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def normalize_site_url(site_url: str) -> str:
    """Require an https origin such as https://audit.example.netlify.app.

    Raises:
        SetupError: not https, has a path, or has no host.
    """
    parsed = urlparse(site_url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise SetupError("--site-url must be an https origin like https://my-site.netlify.app")
    return f"https://{parsed.netloc}"


def render_app_config(path: Path, site_url: str, support_email: str | None) -> None:
    """Point the HubSpot app definition at the deployed callback; sync scopes from scopes.py."""
    config = json.loads(path.read_text(encoding="utf-8"))
    auth = config["config"]["auth"]
    auth["redirectUrls"] = [site_url + CALLBACK_PATH]
    auth["requiredScopes"] = list(REQUIRED_SCOPES)
    auth["optionalScopes"] = list(OPTIONAL_SCOPES)
    if support_email:
        config["config"]["support"]["supportEmail"] = support_email
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def run_setup(
    site_url: str,
    support_email: str | None = None,
    root: Path = Path("."),
    private_key_path: Path = DEFAULT_KEY_PATH,
) -> SetupReport:
    """Create keys and secrets, write .env.receiver and the local .env, render the app config.

    Idempotent: existing keys, secrets and values are kept, never rotated or overwritten.

    Raises:
        SetupError: bad site URL, or a secret in .env disagrees with .env.receiver.
    """
    site = normalize_site_url(site_url)
    report = SetupReport()

    if private_key_path.exists():
        public_pem = public_pem_for(private_key_path)
        report.kept.append("private key")
    else:
        public_pem = generate_keypair(private_key_path)
        report.created.append("private key")

    receiver_path = root / RECEIVER_ENV_FILE
    existing = read_env_file(receiver_path)
    generated = {
        "OAUTH_REDIRECT_URI": site + CALLBACK_PATH,
        "STATE_SIGNING_SECRET": secrets.token_hex(SECRET_BYTES),
        "TOKEN_FETCH_SECRET": secrets.token_hex(SECRET_BYTES),
        "TOKEN_PUBLIC_KEY_PEM": public_pem,
        "TOKEN_STORE_NAME": "oauth-tokens",
        "PLATFORM_NAME": "OTHER",
        "FLOW_NAME": "HubSpot Audit OAuth Receiver",
    }
    values: dict[str, str] = {}
    for name in RECEIVER_VARS:
        if existing.get(name):
            values[name] = existing[name]
            report.kept.append(name)
        else:
            values[name] = generated.get(name, "")
            if name in generated:
                report.created.append(name)
    # The redirect URI and public key always follow the arguments, so a new site URL takes effect.
    values["OAUTH_REDIRECT_URI"] = generated["OAUTH_REDIRECT_URI"]
    values["TOKEN_PUBLIC_KEY_PEM"] = public_pem
    write_env_file(receiver_path, values)
    report.blank_required = [n for n in REQUIRED_BY_RECEIVER if not values[n]]

    _sync_local_env(root / LOCAL_ENV_FILE, values, site, report)

    app_path = root / APP_CONFIG_PATH
    if app_path.exists():
        render_app_config(app_path, site, support_email)
        report.notes.append(f"{APP_CONFIG_PATH} now points at {site + CALLBACK_PATH}")
        if not support_email and "REPLACE-WITH" in app_path.read_text(encoding="utf-8"):
            report.notes.append("Fill in the REPLACE-WITH-* support fields in the app definition.")
    return report


def _sync_local_env(path: Path, receiver: dict[str, str], site: str, report: SetupReport) -> None:
    """Add the values the CLI needs to the local .env; refuse silent disagreement."""
    local = read_env_file(path)
    wanted = {name: receiver[name] for name in SHARED_WITH_LOCAL}
    wanted["TOKEN_RECEIVER_URL"] = site
    wanted["HUBSPOT_REDIRECT_URI"] = receiver["OAUTH_REDIRECT_URI"]
    for name in ("HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET"):
        if receiver[name]:
            wanted[name] = receiver[name]
    additions = {}
    for name, value in wanted.items():
        current = local.get(name)
        if not current:
            additions[name] = value
        elif current != value:
            if name in SHARED_WITH_LOCAL:
                raise SetupError(
                    f"{name} in {path} differs from .env.receiver. The CLI and the receiver "
                    "must share it; fix one of the files."
                )
            report.notes.append(f"{path}: {name} differs from the deployed value; left unchanged.")
    if additions:
        _append_env(path, additions)
        report.created.extend(f"{path.name}:{name}" for name in additions)


def _append_env(path: Path, additions: dict[str, str]) -> None:
    """Append simple KEY=value lines to .env, keeping the file private."""
    needs_newline = path.exists() and not path.read_text(encoding="utf-8").endswith("\n")
    needs_newline = needs_newline and path.stat().st_size > 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if needs_newline:
            handle.write("\n")
        for name, value in additions.items():
            handle.write(f"{name}={value}\n")


def receiver_check(
    receiver_url: str, fetch_secret: str, session: requests.Session | None = None
) -> list[tuple[str, bool, str]]:
    """Smoke-test a deployed (or local) receiver. Returns (check, passed, detail) rows."""
    http = session or requests.Session()
    base = receiver_url.rstrip("/")

    def get(path: str, headers: dict | None = None, params: dict | None = None):
        return http.get(
            base + path, headers=headers or {}, params=params, timeout=SMOKE_TIMEOUT_SECONDS
        )

    checks: list[tuple[str, bool, str]] = []

    def record(name: str, fn, expect_status: int, extra=None) -> None:
        try:
            response = fn()
            passed = response.status_code == expect_status and (extra(response) if extra else True)
            checks.append((name, passed, f"HTTP {response.status_code}, expected {expect_status}"))
        except requests.RequestException as exc:
            checks.append((name, False, type(exc).__name__))

    record("clients rejects a missing secret", lambda: get("/api/clients"), 401)
    record(
        "clients rejects a wrong secret",
        lambda: get("/api/clients", {"Authorization": "Bearer wrong"}),
        401,
    )
    record(
        "clients accepts the fetch secret",
        lambda: get("/api/clients", {"Authorization": f"Bearer {fetch_secret}"}),
        200,
        lambda r: isinstance(r.json().get("clients"), list),
    )
    record(
        "callback rejects a forged state",
        lambda: get(CALLBACK_PATH, params={"code": "x", "state": "forged"}),
        400,
    )
    record(
        "callback handles a declined install",
        lambda: get(CALLBACK_PATH, params={"error": "access_denied"}),
        200,
    )
    return checks
