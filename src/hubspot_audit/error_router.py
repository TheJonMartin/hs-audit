"""Catch, Log, Fail: post the Unified Error Payload to the Central Error Router."""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import requests

logger = logging.getLogger(__name__)

ERROR_ROUTER_TIMEOUT_SECONDS = 5
ERROR_ROUTER_MAX_ATTEMPTS = 2
ERROR_ROUTER_BACKOFF_SECONDS = 0.5
HTTP_SERVER_ERROR_MIN = 500
PLATFORM_NAMES = frozenset(
    {
        "AWS_LAMBDA",
        "GCP_CLOUD_FUNCTION",
        "AZURE_FUNCTION",
        "HEROKU",
        "DIGITALOCEAN",
        "INTERNAL_SERVER",
        "LOCAL_SCRIPT",
        "OTHER",
    }
)


def load_router_config() -> dict[str, str]:
    """Read the router settings from the environment and fail fast if unusable.

    Raises:
        RuntimeError: ``ERROR_ROUTER_URL`` is unset, or ``PLATFORM_NAME`` is not canonical.
    """
    url = os.environ.get("ERROR_ROUTER_URL", "").strip()
    if not url:
        raise RuntimeError("ERROR_ROUTER_URL environment variable is required.")
    platform = os.environ.get("PLATFORM_NAME", "LOCAL_SCRIPT")
    if platform not in PLATFORM_NAMES:
        raise RuntimeError(
            f"PLATFORM_NAME must be one of {sorted(PLATFORM_NAMES)}, got {platform!r}."
        )
    return {
        "url": url,
        "platform": platform,
        "flow_id": os.environ.get("FLOW_ID", "unknown"),
        "flow_name": os.environ.get("FLOW_NAME", "Unnamed_Flow"),
    }


def post_to_error_router(config: dict[str, str], payload: dict) -> None:
    """POST the payload with one bounded retry on network error, timeout or 5xx (never 4xx).

    Falls back to stderr if both attempts fail. Never raises.
    """
    status: int | None = None
    last_error = ""
    for attempt in range(1, ERROR_ROUTER_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                config["url"], json=payload, timeout=ERROR_ROUTER_TIMEOUT_SECONDS
            )
            if response.status_code < HTTP_SERVER_ERROR_MIN:
                return
            status = response.status_code
            last_error = f"HTTP {status}"
        except requests.RequestException as exc:
            status = None
            last_error = type(exc).__name__
        if attempt == ERROR_ROUTER_MAX_ATTEMPTS:
            print(
                f"CRITICAL: Central Error Router post failed (attempt {attempt}/"
                f"{ERROR_ROUTER_MAX_ATTEMPTS}, status={status or 'network'}): {last_error}. "
                f"Payload: {json.dumps(payload)}",
                file=sys.stderr,
            )
            return
        time.sleep(ERROR_ROUTER_BACKOFF_SECONDS)


def handle_failure(config: dict[str, str], error: BaseException, context: dict) -> None:
    """Build the Unified Error Payload from ``error`` and post it. Never raises."""
    payload = {
        "record_id": context.get("record_id") or "N/A",
        "flow_id": config["flow_id"],
        "flow_name": config["flow_name"],
        "process_name": context.get("process_name", "Unknown step"),
        "error_details": f"{type(error).__name__}: {error}",
        "source": config["platform"],
    }
    if context.get("correlation_id"):
        payload["correlation_id"] = context["correlation_id"]
    post_to_error_router(config, payload)
