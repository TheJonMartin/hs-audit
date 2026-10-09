"""Client configuration: which portals to audit, how to authenticate, what to run.

The file never holds credentials. A Service Key client names the environment variable that
holds its key; an OAuth client is identified by portal id and its token lives in the receiver.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path("clients.toml")
AUTH_SERVICE_KEY = "service-key"
AUTH_OAUTH = "oauth"
AUTH_MODES = (AUTH_SERVICE_KEY, AUTH_OAUTH)
CATEGORY_NUMBERS = range(1, 11)
DEFAULT_SAMPLE_SIZE = 1000
DEFAULT_OUTPUT_DIR = "output"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
PORTAL_PATTERN = re.compile(r"^\d{1,20}$")
# A HubSpot token pasted into the file; refuse to read it so it cannot be committed by habit.
TOKEN_LIKE_PATTERN = re.compile(r"\bpat-[a-z0-9]{2,5}-[A-Za-z0-9-]{8,}")
CLIENT_KEYS = {
    "name",
    "slug",
    "portal_id",
    "auth",
    "service_key_env",
    "categories",
    "sample_size",
    "enabled",
}
DEFAULT_KEYS = {"categories", "sample_size", "output_dir"}


@dataclass(frozen=True)
class ClientConfig:
    """One audited portal, with defaults already applied."""

    name: str
    slug: str
    auth: str
    categories: tuple[int, ...]
    sample_size: int
    enabled: bool = True
    portal_id: str | None = None
    service_key_env: str | None = None


@dataclass(frozen=True)
class AuditConfig:
    """The whole config file."""

    output_dir: str = DEFAULT_OUTPUT_DIR
    clients: tuple[ClientConfig, ...] = field(default_factory=tuple)

    def find(self, ident: str) -> ClientConfig:
        """Find a client by slug, name (case-insensitive) or portal id.

        Raises:
            ConfigError: no match.
        """
        wanted = ident.strip().lower()
        for client in self.clients:
            if wanted in (client.slug, client.name.lower(), client.portal_id):
                return client
        known = ", ".join(c.slug for c in self.clients) or "none"
        raise ConfigError(f"No client matches {ident!r}. Configured: {known}.")

    def enabled_clients(self) -> list[ClientConfig]:
        """Clients with ``enabled = true``, in file order."""
        return [c for c in self.clients if c.enabled]


def slugify(name: str) -> str:
    """Lowercase kebab-case slug for a client name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AuditConfig:
    """Read and validate the client config.

    Raises:
        ConfigError: the file is missing, unreadable, malformed, contains a token, or fails
            validation. The message names the client and field.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {type(exc).__name__}") from exc
    if TOKEN_LIKE_PATTERN.search(text):
        raise ConfigError(
            f"{path} contains something that looks like a HubSpot token. Remove it and set "
            "service_key_env to the NAME of an environment variable instead."
        )
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return _validate(raw)


def _validate(raw: dict[str, Any]) -> AuditConfig:
    unknown_top = set(raw) - {"defaults", "clients"}
    if unknown_top:
        raise ConfigError(f"Unknown top-level keys: {sorted(unknown_top)}.")
    defaults = raw.get("defaults", {})
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise ConfigError(f"Unknown keys in [defaults]: {sorted(unknown)}.")
    default_categories = _categories(defaults.get("categories"), "[defaults]") or tuple(
        CATEGORY_NUMBERS
    )
    default_sample = _sample_size(defaults.get("sample_size", DEFAULT_SAMPLE_SIZE), "[defaults]")
    output_dir = str(defaults.get("output_dir", DEFAULT_OUTPUT_DIR))

    clients = [
        _client(entry, index, default_categories, default_sample)
        for index, entry in enumerate(raw.get("clients", []), start=1)
    ]
    for label, values in (
        ("name", [c.name.lower() for c in clients]),
        ("slug", [c.slug for c in clients]),
        ("portal_id", [c.portal_id for c in clients if c.portal_id]),
    ):
        duplicates = sorted({v for v in values if values.count(v) > 1})
        if duplicates:
            raise ConfigError(f"Duplicate client {label}: {duplicates}.")
    return AuditConfig(output_dir=output_dir, clients=tuple(clients))


def _client(
    entry: dict[str, Any], index: int, default_categories: tuple[int, ...], default_sample: int
) -> ClientConfig:
    name = str(entry.get("name", "")).strip()
    where = f"client #{index}" + (f" ({name})" if name else "")
    if not name:
        raise ConfigError(f"{where}: name is required.")
    unknown = set(entry) - CLIENT_KEYS
    if unknown:
        raise ConfigError(
            f"{where}: unknown keys {sorted(unknown)}. Credentials do not belong in this file."
        )
    slug = str(entry.get("slug") or slugify(name))
    if not SLUG_PATTERN.match(slug):
        raise ConfigError(f"{where}: slug {slug!r} must be lowercase letters, digits and hyphens.")
    auth = entry.get("auth", AUTH_OAUTH)
    if auth not in AUTH_MODES:
        raise ConfigError(f"{where}: auth must be one of {list(AUTH_MODES)}, got {auth!r}.")
    portal_id = entry.get("portal_id")
    if portal_id is not None:
        portal_id = str(portal_id)
        if not PORTAL_PATTERN.match(portal_id):
            raise ConfigError(f"{where}: portal_id must be digits, got {portal_id!r}.")
    key_env = entry.get("service_key_env")
    if auth == AUTH_SERVICE_KEY:
        if not key_env or not ENV_NAME_PATTERN.match(str(key_env)):
            raise ConfigError(
                f"{where}: service-key clients need service_key_env set to an environment "
                "variable NAME such as ACME_HUBSPOT_KEY."
            )
    elif key_env:
        raise ConfigError(f"{where}: service_key_env only applies to auth = 'service-key'.")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: enabled must be true or false.")
    return ClientConfig(
        name=name,
        slug=slug,
        auth=auth,
        categories=_categories(entry.get("categories"), where) or default_categories,
        sample_size=_sample_size(entry.get("sample_size", default_sample), where),
        enabled=enabled,
        portal_id=portal_id,
        service_key_env=str(key_env) if key_env else None,
    )


def _categories(value: Any, where: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
    ):
        raise ConfigError(f"{where}: categories must be a non-empty list of integers.")
    bad = sorted(set(value) - set(CATEGORY_NUMBERS))
    if bad:
        raise ConfigError(f"{where}: unknown categories {bad}; choose from 1-10.")
    return tuple(sorted(set(value)))


def _sample_size(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{where}: sample_size must be a positive integer.")
    return value
