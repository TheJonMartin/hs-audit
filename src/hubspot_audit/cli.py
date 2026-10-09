"""Command line entry point: ``hubspot-audit run``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, bundle
from .auth import AuthProvider, OAuthAuth, StaticKeyAuth, run_loopback_flow
from .categories import CategoryResult, registry
from .client import HubSpotClient
from .context import DEFAULT_SAMPLE_SIZE, AuditContext
from .error_router import handle_failure, load_router_config
from .errors import AuthError, HubSpotError
from .probes import ACCOUNT_INFO_PATH, PROBES
from .scopes import OPTIONAL_SCOPES, REQUIRED_SCOPES

logger = logging.getLogger("hubspot_audit")
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
AUTH_SERVICE_KEY = "service-key"
AUTH_OAUTH = "oauth"


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


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="hubspot-audit", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Pull the portal and write the audit bundle.")
    run.add_argument("--categories", help="Comma-separated category numbers (default: all 1-10).")
    run.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    run.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    run.add_argument("--prospect", default=os.environ.get("PROSPECT_NAME"))
    run.add_argument(
        "--auth",
        choices=[AUTH_SERVICE_KEY, AUTH_OAUTH],
        default=AUTH_SERVICE_KEY,
        help="service-key (default) or oauth (public app install).",
    )
    url = sub.add_parser("install-url", help="Print the OAuth install URL for the public app.")
    url.add_argument("--state", default="manual")
    return parser


def execute_run(args: argparse.Namespace, context: dict) -> Path:
    """Run the audit and return the output directory."""
    prospect = args.prospect
    if not prospect:
        raise ValueError("Prospect name is required: set PROSPECT_NAME or pass --prospect.")
    available = registry()
    numbers = parse_categories(args.categories, set(available))

    now = datetime.now(timezone.utc)
    auth = build_auth(args.auth)
    client = HubSpotClient(auth=auth)
    ctx = AuditContext(client=client, now=now, sample_size=args.sample_size)

    context["process_name"] = "Probing scopes"
    probes = client.run_probes(PROBES)
    portal_id = _portal_id(client)
    context["record_id"] = portal_id or prospect

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
        prospect,
        portal_id,
        now,
        client.granted_areas(),
        probe_rows,
        args.sample_size,
        auth_mode=args.auth,
    )
    if isinstance(auth, OAuthAuth):
        data["meta"]["oauth_scopes_granted"] = auth.token_info()["scopes"]
    tables: dict[str, list] = {}
    for result in results:
        tables.update(result.tables)
    bundle.validate(data, client.secrets)
    run_dir = bundle.write_outputs(data, tables, Path(args.output_dir), prospect, now)
    logger.info("Bundle written to %s (%s API calls)", run_dir, len(client.call_log))
    return run_dir


def oauth_from_env() -> OAuthAuth:
    """Build the OAuth provider from the environment."""
    return OAuthAuth(
        os.environ.get("HUBSPOT_CLIENT_ID", ""),
        os.environ.get("HUBSPOT_CLIENT_SECRET", ""),
        os.environ.get("HUBSPOT_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        refresh_token=os.environ.get("HUBSPOT_REFRESH_TOKEN") or None,
    )


def build_auth(mode: str) -> AuthProvider:
    """Create the auth provider. OAuth uses HUBSPOT_REFRESH_TOKEN, else an interactive install."""
    if mode == AUTH_SERVICE_KEY:
        return StaticKeyAuth(os.environ.get("HUBSPOT_SERVICE_KEY", ""))
    oauth = oauth_from_env()
    if not os.environ.get("HUBSPOT_REFRESH_TOKEN"):
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


def main(argv: list[str] | None = None) -> int:
    """Entry point. One global handler: Catch, Log (router), Fail (re-raise)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv_if_present()
    args = build_parser().parse_args(argv)
    if args.command == "install-url":
        print(oauth_from_env().authorization_url(REQUIRED_SCOPES, OPTIONAL_SCOPES, args.state))
        return 0
    router = load_router_config()
    context = {
        "record_id": args.prospect,
        "process_name": "Initialization",
        "correlation_id": str(uuid.uuid4()),
    }
    try:
        run_dir = execute_run(args, context)
    except Exception as exc:
        handle_failure(router, exc, context)
        raise
    print(f"Audit bundle: {run_dir / 'audit_bundle.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
