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
from .categories import CategoryResult, registry
from .client import HubSpotClient
from .context import DEFAULT_SAMPLE_SIZE, AuditContext
from .error_router import handle_failure, load_router_config
from .errors import AuthError, HubSpotError
from .probes import ACCOUNT_INFO_PATH, PROBES

logger = logging.getLogger("hubspot_audit")
DEFAULT_OUTPUT_DIR = "output"


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
    return parser


def execute_run(args: argparse.Namespace, context: dict) -> Path:
    """Run the audit and return the output directory."""
    service_key = os.environ.get("HUBSPOT_SERVICE_KEY", "")
    prospect = args.prospect
    if not prospect:
        raise ValueError("Prospect name is required: set PROSPECT_NAME or pass --prospect.")
    available = registry()
    numbers = parse_categories(args.categories, set(available))

    now = datetime.now(timezone.utc)
    client = HubSpotClient(service_key)
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
        results, prospect, portal_id, now, client.granted_areas(), probe_rows, args.sample_size
    )
    tables: dict[str, list] = {}
    for result in results:
        tables.update(result.tables)
    bundle.validate(data, service_key)
    run_dir = bundle.write_outputs(data, tables, Path(args.output_dir), prospect, now)
    logger.info("Bundle written to %s (%s API calls)", run_dir, len(client.call_log))
    return run_dir


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
