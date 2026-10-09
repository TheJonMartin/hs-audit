"""Exception types for HubSpot API failures.

Per-endpoint failures (403, unsupported endpoint) are recorded in the bundle and do not fail
the run. Only ``AuthError`` and bundle validation failures reach the global handler.
"""

from __future__ import annotations


class HubSpotError(Exception):
    """Base class for HubSpot API failures."""

    def __init__(self, message: str, endpoint: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status


class AuthError(HubSpotError):
    """The Service Key was rejected (HTTP 401). The whole run is untrustworthy."""


class ForbiddenError(HubSpotError):
    """The key lacks the scope for this endpoint (HTTP 403)."""


class UnsupportedError(HubSpotError):
    """The endpoint does not exist or is not available for this portal (404/405/501)."""


class ApiError(HubSpotError):
    """Any other non-retryable API failure, or retries exhausted."""


class BundleValidationError(Exception):
    """The assembled bundle failed schema validation or the data-safety checks."""


class ConfigError(Exception):
    """The client config file is missing, malformed or unsafe."""


class BatchError(Exception):
    """One or more clients failed in a ``run --all`` batch. Each was already reported."""
