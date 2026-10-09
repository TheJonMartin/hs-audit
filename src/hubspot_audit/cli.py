"""Command line entry point: ``hubspot-audit run``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, bundle
from .auth import AuthProvider, OAuthAuth, StaticKeyAuth, run_loopback_flow
from .categories import CategoryResult, registry
from .client import HubSpotClient
from .config import (
    AUTH_OAUTH,
    AUTH_SERVICE_KEY,
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SAMPLE_SIZE,
    AuditConfig,
    ClientConfig,
    load_config,
)
from .context import AuditContext
from .error_router import handle_failure, load_router_config
from .errors import AuthError, BatchError, ConfigError, HubSpotError
from .probes import ACCOUNT_INFO_PATH, PROBES
from .receiver import (
    DEFAULT_KEY_PATH,
    delete_client,
    fetch_refresh_token,
    generate_keypair,
    list_clients,
    new_signed_state,
    store_refresh_token,
)
from .scopes import OPTIONAL_SCOPES, REQUIRED_SCOPES

logger = logging.getLogger("hubspot_audit")
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
SERVICE_KEY_ENV_DEFAULT = "HUBSPOT_SERVICE_KEY"


def load_dotenv_if_present(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from ``.env`` without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def parse_categories(text: str | None, available: set[int]) -> list[int]:
    """Parse ``--categories 1,2,3``; default is all categories.

    Raises:
        ValueError: a token is not an available category number.
    """
    if not text:
        return sorted(available)
    numbers = []
    for token in text.split(","):
        number = int(token.strip())
        if number not in available:
            raise ValueError(f"Unknown category {number}; choose from {sorted(available)}.")
        numbers.append(number)
    return sorted(set(numbers))


@dataclass
class RunSpec:
    """Everything one audit run needs, resolved from flags, config and environment."""

    prospect: str
    auth_mode: str
    categories: list[int] | None
    sample_size: int
    output_dir: str
    service_key_env: str = SERVICE_KEY_ENV_DEFAULT
    portal: str | None = None
    state: str | None = None
    slug: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="hubspot-audit", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", help="Client config file (default: HUBSPOT_AUDIT_CONFIG or clients.toml)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Pull a portal and write the audit bundle.")
    target = run.add_mutually_exclusive_group()
    target.add_argument("--client", help="Client from the config file (slug, name or portal id).")
    target.add_argument(
        "--all", action="store_true", help="Run every enabled client in the config."
    )
    run.add_argument(
        "--categories", help="Comma-separated category numbers (default: config or all)."
    )
    run.add_argument(
        "--sample-size", type=int, help="Contacts to sample (default: config or 1000)."
    )
    run.add_argument("--output-dir", help="Output directory (default: config or output).")
    run.add_argument(
        "--prospect", default=os.environ.get("PROSPECT_NAME"), help="Name for ad hoc runs."
    )
    run.add_argument(
        "--auth",
        choices=[AUTH_SERVICE_KEY, AUTH_OAUTH],
        help="service-key (ad hoc default) or oauth (public app install).",
    )
    run.add_argument("--portal", help="HubSpot portal id of an installed client (stored token).")
    run.add_argument(
        "--state",
        help="Install state from `install-url`; resolves to the portal it installed (24h).",
    )
    sub.add_parser("check-config", help="Validate the client config file.")
    url = sub.add_parser(
        "install-url", help="Create a signed install link for one client (hosted receiver)."
    )
    url.add_argument("--state", help="Reuse an existing state instead of creating one.")
    sub.add_parser("clients", help="List configured clients with install status.")
    revoke = sub.add_parser("revoke", help="Offboard a client: revoke at HubSpot, delete our copy.")
    revoke.add_argument("--portal", required=True)
    sub.add_parser("keygen", help="Create the keypair that protects tokens stored by the receiver.")
    return parser


def config_path(args: argparse.Namespace) -> Path:
    """Config location: --config, then HUBSPOT_AUDIT_CONFIG, then ./clients.toml."""
    return Path(args.config or os.environ.get("HUBSPOT_AUDIT_CONFIG") or DEFAULT_CONFIG_PATH)


def spec_for_client(client: ClientConfig, config: AuditConfig, args: argparse.Namespace) -> RunSpec:
    """Build a spec from a config entry; explicit flags override the config."""
    portal = args.portal or client.portal_id
    if client.auth == AUTH_OAUTH and not (portal or args.state):
        raise ConfigError(
            f"{client.name}: no portal_id yet. After the client installs, run with --state "
            "<state> once, then add the portal id (see `hubspot-audit clients`) to the config."
        )
    return RunSpec(
        prospect=client.name,
        auth_mode=args.auth or client.auth,
        categories=(
            parse_categories(args.categories, set(registry()))
            if args.categories
            else list(client.categories)
        ),
        sample_size=args.sample_size or client.sample_size,
        output_dir=args.output_dir or config.output_dir,
        service_key_env=client.service_key_env or SERVICE_KEY_ENV_DEFAULT,
        portal=portal,
        state=args.state,
        slug=client.slug,
    )


def spec_from_flags(args: argparse.Namespace) -> RunSpec:
    """Build a spec for an ad hoc run without a config file."""
    if not args.prospect:
        raise ValueError("Prospect name is required: use --client, --prospect or PROSPECT_NAME.")
    return RunSpec(
        prospect=args.prospect,
        auth_mode=args.auth or AUTH_SERVICE_KEY,
        categories=parse_categories(args.categories, set(registry())) if args.categories else None,
        sample_size=args.sample_size or DEFAULT_SAMPLE_SIZE,
        output_dir=args.output_dir or DEFAULT_OUTPUT_DIR,
        portal=args.portal,
        state=args.state,
    )


def execute_run(spec: RunSpec, context: dict) -> Path:
    """Run one audit and return the output directory."""
    available = registry()
    numbers = spec.categories or sorted(available)

    now = datetime.now(UTC)
    auth = build_auth(spec.auth_mode, spec.state, spec.portal, spec.service_key_env)
    client = HubSpotClient(auth=auth)
    ctx = AuditContext(client=client, now=now, sample_size=spec.sample_size)

    context["process_name"] = "Probing scopes"
    probes = client.run_probes(PROBES)
    portal_id = _portal_id(client)
    context["record_id"] = portal_id or spec.prospect
    if spec.portal and portal_id and portal_id != spec.portal:
        raise AuthError(
            f"Portal mismatch for {spec.prospect}: expected {spec.portal}, token belongs to "
            f"{portal_id}. Fix portal_id in the config or reinstall."
        )

    results: list[CategoryResult] = []
    for number in numbers:
        name, module = available[number]
        context["process_name"] = f"Category {number}: {name}"
        logger.info("Running category %s (%s)", number, name)
        results.append(module.run(ctx))

    context["process_name"] = "Assembling bundle"
    probe_rows = [{"area": p.area, "endpoint": p.endpoint, "status": p.status} for p in probes]
    data = bundle.assemble(
        results,
        spec.prospect,
        portal_id,
        now,
        client.granted_areas(),
        probe_rows,
        spec.sample_size,
        auth_mode=spec.auth_mode,
    )
    if isinstance(auth, OAuthAuth):
        data["meta"]["oauth_scopes_granted"] = auth.token_info()["scopes"]
    tables: dict[str, list] = {}
    for result in results:
        tables.update(result.tables)
    bundle.validate(data, client.secrets)
    run_dir = bundle.write_outputs(data, tables, Path(spec.output_dir), spec.prospect, now)
    logger.info("Bundle written to %s (%s API calls)", run_dir, len(client.call_log))
    if spec.state and portal_id and not spec.portal:
        print(f'Add to the config for {spec.prospect}: portal_id = "{portal_id}"')
    return run_dir


def oauth_from_env() -> OAuthAuth:
    """Build the OAuth provider from the environment."""
    return OAuthAuth(
        os.environ.get("HUBSPOT_CLIENT_ID", ""),
        os.environ.get("HUBSPOT_CLIENT_SECRET", ""),
        os.environ.get("HUBSPOT_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        refresh_token=os.environ.get("HUBSPOT_REFRESH_TOKEN") or None,
    )


def _receiver_settings() -> tuple[str, str, Path]:
    return (
        os.environ.get("TOKEN_RECEIVER_URL", ""),
        os.environ.get("TOKEN_FETCH_SECRET", ""),
        Path(os.environ.get("TOKEN_PRIVATE_KEY_PATH") or DEFAULT_KEY_PATH),
    )


def build_auth(
    mode: str,
    state: str | None = None,
    portal: str | None = None,
    service_key_env: str = SERVICE_KEY_ENV_DEFAULT,
) -> AuthProvider:
    """Create the auth provider.

    OAuth order: ``--portal`` or ``--state`` (token held by the Netlify receiver), then
    HUBSPOT_REFRESH_TOKEN, then an interactive localhost install.
    """
    if mode == AUTH_SERVICE_KEY:
        return StaticKeyAuth(os.environ.get(service_key_env, ""), service_key_env)
    oauth = oauth_from_env()
    if portal or state:
        url, fetch_secret, key_path = _receiver_settings()
        fetched = fetch_refresh_token(
            url, fetch_secret, portal=portal, state=state, private_key_path=key_path
        )
        hub_id = str(fetched["hub_id"])
        oauth.set_refresh_token(
            fetched["refresh_token"],
            on_change=lambda token: store_refresh_token(url, fetch_secret, hub_id, token, key_path),
        )
    elif not os.environ.get("HUBSPOT_REFRESH_TOKEN"):
        run_loopback_flow(
            oauth,
            REQUIRED_SCOPES,
            OPTIONAL_SCOPES,
            os.environ.get("HUBSPOT_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        )
    return oauth


def _portal_id(client: HubSpotClient) -> str | None:
    """Portal id from account info; None if the endpoint is not available to this key."""
    try:
        return str(client.get(ACCOUNT_INFO_PATH).get("portalId") or "") or None
    except AuthError:
        raise
    except HubSpotError:
        return None


def _show_clients(args: argparse.Namespace) -> int:
    """Table of configured clients with install status from the receiver, if reachable."""
    path = config_path(args)
    config = load_config(path) if path.exists() else AuditConfig()
    url, fetch_secret, _ = _receiver_settings()
    installed = {c["hub_id"]: c for c in list_clients(url, fetch_secret)} if url else {}
    print("name\tslug\tauth\tportal\tenabled\tinstalled\tlast_used")
    seen = set()
    for c in config.clients:
        entry = installed.get(c.portal_id or "")
        seen.add(c.portal_id)
        status = "-" if c.auth != AUTH_OAUTH else ("yes" if entry else "NO")
        last = (entry or {}).get("last_used") or "-"
        print(f"{c.name}\t{c.slug}\t{c.auth}\t{c.portal_id or '-'}\t{c.enabled}\t{status}\t{last}")
    for hub_id, entry in installed.items():
        if hub_id not in seen:
            print(f"(not in config)\t-\toauth\t{hub_id}\t-\tyes\t{entry.get('last_used') or '-'}")
    return 0


def _revoke(portal: str) -> int:
    """Revoke the refresh token at HubSpot (best effort), then delete our stored copy."""
    url, fetch_secret, key_path = _receiver_settings()
    revoked = False
    try:
        fetched = fetch_refresh_token(url, fetch_secret, portal=portal, private_key_path=key_path)
        oauth = oauth_from_env()
        oauth.set_refresh_token(fetched["refresh_token"])
        oauth.revoke_refresh_token()
        revoked = True
    except AuthError as exc:
        print(f"WARNING: could not revoke at HubSpot ({exc}). Continuing to delete our copy.")
    delete_client(url, fetch_secret, portal)
    print(
        f"Stored token for portal {portal} deleted"
        + (" and revoked at HubSpot." if revoked else ".")
        + " Ask the client to uninstall the app (Settings, Integrations, Connected Apps) "
        "to end access completely."
    )
    return 0


def _run_all(config: AuditConfig, args: argparse.Namespace, router: dict) -> int:
    """Run every enabled client. A failing client is reported and the batch continues."""
    failures = []
    for client in config.enabled_clients():
        context = {
            "record_id": client.portal_id or client.name,
            "process_name": "Initialization",
            "correlation_id": str(uuid.uuid4()),
        }
        try:
            run_dir = execute_run(spec_for_client(client, config, args), context)
            print(f"{client.name}: {run_dir / 'audit_bundle.json'}")
        except Exception as exc:  # noqa: BLE001 - reported per client, re-raised as a batch below
            handle_failure(router, exc, context)
            failures.append(f"{client.name}: {type(exc).__name__}")
            logger.error("Client %s failed at '%s': %s", client.name, context["process_name"], exc)
    if failures:
        raise BatchError(f"{len(failures)} client(s) failed: " + "; ".join(failures))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point. One global handler: Catch, Log (router), Fail (re-raise)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv_if_present()
    args = build_parser().parse_args(argv)
    if args.command == "check-config":
        config = load_config(config_path(args))
        print(f"OK: {len(config.clients)} client(s), {len(config.enabled_clients())} enabled.")
        return 0
    if args.command == "clients":
        return _show_clients(args)
    if args.command == "revoke":
        return _revoke(args.portal)
    if args.command == "keygen":
        public_pem = generate_keypair(
            Path(os.environ.get("TOKEN_PRIVATE_KEY_PATH") or DEFAULT_KEY_PATH)
        )
        print("Private key written (mode 600). Set this as TOKEN_PUBLIC_KEY_PEM in Netlify:\n")
        print(public_pem)
        return 0
    if args.command == "install-url":
        state = args.state or new_signed_state(os.environ.get("STATE_SIGNING_SECRET", ""))
        url = oauth_from_env().authorization_url(REQUIRED_SCOPES, OPTIONAL_SCOPES, state)
        print(
            f"Install URL (send to the client admin):\n{url}\n\nState (use with run --state):\n{state}"
        )
        return 0

    router = load_router_config()
    config = load_config(config_path(args)) if (args.client or args.all) else None
    if args.all:
        try:
            return _run_all(config, args, router)
        except BatchError:
            raise  # every client was already reported; do not post the batch a second time
    spec = (
        spec_for_client(config.find(args.client), config, args)
        if args.client
        else spec_from_flags(args)
    )
    context = {
        "record_id": spec.portal or spec.prospect,
        "process_name": "Initialization",
        "correlation_id": str(uuid.uuid4()),
    }
    try:
        run_dir = execute_run(spec, context)
    except Exception as exc:
        handle_failure(router, exc, context)
        raise
    print(f"Audit bundle: {run_dir / 'audit_bundle.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
