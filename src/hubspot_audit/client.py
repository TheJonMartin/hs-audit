"""HubSpot API client: Bearer auth, cursor pagination, retries, rate limiting, call log."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

from .errors import ApiError, AuthError, ForbiddenError, HubSpotError, UnsupportedError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"
REQUEST_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
# Object reads allow ~190 calls / 10s; stay well under with a single-threaded client.
MIN_INTERVAL_SECONDS = 0.1
# The Search API is limited to ~5 requests per second per account.
MIN_SEARCH_INTERVAL_SECONDS = 0.25
SEARCH_PAGE_SIZE = 100
LIST_PAGE_SIZE = 100
HTTP_OK_MAX = 299
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_MIN = 500
UNSUPPORTED_STATUSES = frozenset({404, 405, 501})


@dataclass
class CallRecord:
    """One entry in the call log. Holds endpoint, status and count only, never record data."""

    method: str
    endpoint: str
    status: int | None
    count: int | None
    attempts: int


@dataclass
class ProbeResult:
    """Outcome of the startup probe for one scope area."""

    area: str
    endpoint: str
    status: str  # "ok" | "forbidden" | "unsupported" | "error"


@dataclass
class Probe:
    """A cheap request used to learn whether a scope area works."""

    area: str
    method: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None


class HubSpotClient:
    """Thin, retrying HubSpot REST client. Never logs the key or response bodies."""

    def __init__(
        self,
        service_key: str,
        session: requests.Session | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        if not service_key:
            raise AuthError("HUBSPOT_SERVICE_KEY is not set.")
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {service_key}"})
        self._sleep = sleep
        self._clock = clock
        self._last_call = 0.0
        self.call_log: list[CallRecord] = []
        self.probe_results: list[ProbeResult] = []

    # ------------------------------------------------------------------ core request

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request, retrying on 429, 5xx and network errors.

        Raises:
            AuthError: HTTP 401.
            ForbiddenError: HTTP 403 (missing scope).
            UnsupportedError: HTTP 404, 405 or 501.
            ApiError: any other failure, or retries exhausted.
        """
        is_search = path.endswith("/search")
        min_interval = MIN_SEARCH_INTERVAL_SECONDS if is_search else MIN_INTERVAL_SECONDS
        status: int | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle(min_interval)
            try:
                response = self._session.request(
                    method,
                    BASE_URL + path,
                    params=params,
                    json=json_body,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                status = None
                if attempt == MAX_ATTEMPTS:
                    self._log(method, path, None, None, attempt)
                    raise ApiError(
                        f"Network error after {attempt} attempts: {type(exc).__name__}", path
                    ) from exc
                self._sleep(self._backoff(attempt))
                continue

            status = response.status_code
            retryable = status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR_MIN
            if retryable and attempt < MAX_ATTEMPTS and status not in UNSUPPORTED_STATUSES:
                self._sleep(self._retry_delay(response, attempt))
                continue
            return self._finish(method, path, response, attempt)
        raise ApiError("Retries exhausted.", path, status)  # pragma: no cover

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``path`` and return the parsed JSON body."""
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` to ``path`` and return the parsed JSON body."""
        return self.request("POST", path, json_body=body)

    # ------------------------------------------------------------------ pagination

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        results_key: str = "results",
        max_records: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield records from a GET endpoint that pages with ``paging.next.after``."""
        query = {"limit": LIST_PAGE_SIZE, **(params or {})}
        yielded = 0
        while True:
            page = self.get(path, query)
            for record in page.get(results_key, []):
                yield record
                yielded += 1
                if max_records is not None and yielded >= max_records:
                    return
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return
            query["after"] = after

    def search(
        self,
        object_type: str,
        filters: list[dict[str, Any]] | None = None,
        properties: list[str] | None = None,
        sorts: list[dict[str, str]] | None = None,
        max_records: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield records from the CRM search API, following ``paging.next.after``."""
        body: dict[str, Any] = {"limit": SEARCH_PAGE_SIZE}
        if filters:
            body["filterGroups"] = [{"filters": filters}]
        if properties:
            body["properties"] = properties
        if sorts:
            body["sorts"] = sorts
        yielded = 0
        path = f"/crm/v3/objects/{object_type}/search"
        while True:
            page = self.post(path, body)
            for record in page.get("results", []):
                yield record
                yielded += 1
                if max_records is not None and yielded >= max_records:
                    return
            after = (page.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return
            body["after"] = after

    def count(self, object_type: str, filters: list[dict[str, Any]] | None = None) -> int:
        """Return the search API ``total`` for ``filters`` without paging records."""
        body: dict[str, Any] = {"limit": 1}
        if filters:
            body["filterGroups"] = [{"filters": filters}]
        page = self.post(f"/crm/v3/objects/{object_type}/search", body)
        return int(page.get("total", 0))

    # ------------------------------------------------------------------ probe

    def run_probes(self, probes: list[Probe]) -> list[ProbeResult]:
        """Probe each endpoint once and record which scope areas work.

        Raises:
            AuthError: the key is invalid (propagated so the run stops).
        """
        results: list[ProbeResult] = []
        for probe in probes:
            try:
                self.request(probe.method, probe.path, params=probe.params, json_body=probe.body)
                outcome = "ok"
            except AuthError:
                raise
            except ForbiddenError:
                outcome = "forbidden"
            except UnsupportedError:
                outcome = "unsupported"
            except HubSpotError:
                outcome = "error"
            results.append(ProbeResult(probe.area, probe.path, outcome))
        self.probe_results = results
        return results

    def granted_areas(self) -> list[str]:
        """Scope areas whose probe returned success."""
        return sorted({r.area for r in self.probe_results if r.status == "ok"})

    # ------------------------------------------------------------------ internals

    def _throttle(self, min_interval: float) -> None:
        wait = self._last_call + min_interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last_call = self._clock()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_MAX_SECONDS)

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), BACKOFF_MAX_SECONDS)
            except ValueError:
                pass
        return self._backoff(attempt)

    def _log(self, method: str, path: str, status: int | None, count: int | None, attempts: int):
        self.call_log.append(CallRecord(method, path, status, count, attempts))
        logger.info("%s %s -> %s (count=%s, attempts=%s)", method, path, status, count, attempts)

    def _finish(
        self, method: str, path: str, response: requests.Response, attempts: int
    ) -> dict[str, Any]:
        status = response.status_code
        if status <= HTTP_OK_MAX:
            try:
                data = response.json() if response.content else {}
            except ValueError as exc:
                self._log(method, path, status, None, attempts)
                raise ApiError("Response was not valid JSON.", path, status) from exc
            results = data.get("results") if isinstance(data, dict) else None
            self._log(
                method, path, status, len(results) if isinstance(results, list) else None, attempts
            )
            return data if isinstance(data, dict) else {"results": data}
        self._log(method, path, status, None, attempts)
        message = self._error_message(response)
        if status == 401:
            raise AuthError(f"HTTP 401 on {path}: {message}", path, status)
        if status == 403:
            raise ForbiddenError(f"HTTP 403 on {path}: {message}", path, status)
        if status in UNSUPPORTED_STATUSES:
            raise UnsupportedError(f"HTTP {status} on {path}: {message}", path, status)
        raise ApiError(f"HTTP {status} on {path}: {message}", path, status)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        """Return HubSpot's generic error category/message; never echo the full body."""
        try:
            body = response.json()
        except ValueError:
            return "non-JSON error response"
        if isinstance(body, dict):
            return str(body.get("category") or body.get("status") or "error")
        return "error"
