"""Client for the Oxylabs Web API: web search and page scraping.

Built from https://developers.oxylabs.io/products/web-api/for-agents.md
(see docs/oxylabs-telegram-scrape.md). Stdlib only, like local_ai/.

- Bearer auth on every call. The key comes from OXYLABS_WEB_API_KEY in the
  environment, then .env. It never appears in logs, errors or reprs.
- 150 s per call: scrapes can be slow and short timeouts fail work that
  would have succeeded.
- Any 2xx is success.
- Retries only transport timeouts and 408/429/5xx: 3 attempts in total, with
  exponential backoff plus jitter. 400 and 401 are raised straight away.
- Every response's `metadata.request_id` is logged and returned.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

API_URL = "https://webapi.oxylabs.io"
KEY_ENV = "OXYLABS_WEB_API_KEY"
TIMEOUT = 150
ATTEMPTS = 3
MAX_QUERY_CHARS = 2048
MAX_SEARCH_RESULTS = 20
SCRAPE_OUTPUTS = frozenset({"markdown", "html", "json", "screenshot"})

log = logging.getLogger(__name__)

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")


# ---- errors -----------------------------------------------------------------

class WebApiError(RuntimeError):
    """An Oxylabs call failed. Branch on `title` (the problem type), not the message."""

    def __init__(self, message: str, *, status: int | None = None,
                 title: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.title = title
        self.request_id = request_id


class MissingApiKeyError(WebApiError):
    """OXYLABS_WEB_API_KEY is in neither the environment nor .env."""


class AuthenticationError(WebApiError):
    """401: the key was rejected. Never retried."""


class ValidationError(WebApiError):
    """400: the request was invalid. Never retried unchanged."""

    def __init__(self, message: str, *, pointers: Sequence[str] = (), **kwargs: Any):
        super().__init__(message, **kwargs)
        self.pointers = list(pointers)


class RateLimitError(WebApiError):
    """429 that outlasted the retries: rate limiting or a spent quota."""


# ---- transport --------------------------------------------------------------

@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


# (method, url, headers, body, timeout) -> HttpResponse.
# Raises TimeoutError on a transport timeout and ConnectionError on any other
# network failure. HTTP error statuses are returned, not raised.
Transport = Callable[[str, str, dict, bytes, float], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict, body: bytes,
                     timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return HttpResponse(resp.status, resp.read(), dict(resp.headers))
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read()
        except OSError:
            error_body = b""
        finally:
            e.close()
        return HttpResponse(e.code, error_body, dict(e.headers or {}))
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise TimeoutError(f"timed out after {timeout:.0f}s") from None
        raise ConnectionError(f"network error: {e.reason}") from None
    except TimeoutError:
        raise TimeoutError(f"timed out after {timeout:.0f}s") from None
    except (http.client.HTTPException, OSError) as e:
        raise ConnectionError(f"network error: {e.__class__.__name__}: {e}") from None


# ---- key --------------------------------------------------------------------

def load_api_key() -> str:
    """The key from the environment, else from .env. Raises MissingApiKeyError."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        try:
            from local_ai.state import read_env
            key = read_env().get(KEY_ENV, "").strip()
        except ImportError:
            key = ""
    if not key:
        raise MissingApiKeyError(f"{KEY_ENV} is not set in the environment or in .env")
    return key


# ---- results ----------------------------------------------------------------

@dataclass
class SearchResult:
    results: list[dict]
    request_id: str | None
    data: dict


@dataclass
class ScrapeResult:
    content: dict[str, Any]          # output format -> content, e.g. {"html": "<html>..."}
    final_url: str | None            # after redirects
    request_id: str | None
    data: dict

    def text(self, fmt: str = "html") -> str:
        value = self.content.get(fmt)
        return value if isinstance(value, str) else ""


# ---- client -----------------------------------------------------------------

class WebApiClient:
    def __init__(self, api_key: str | None = None, *,
                 transport: Transport = urllib_transport,
                 attempts: int = ATTEMPTS,
                 timeout: float = TIMEOUT,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = random.random,
                 base_url: str = API_URL):
        self._api_key = api_key or load_api_key()
        self._transport = transport
        self._attempts = max(1, attempts)
        self._timeout = timeout
        self._sleep = sleep
        self._jitter = jitter
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:  # never show the key
        return f"WebApiClient(base_url={self._base_url!r})"

    def search(self, query: str, max_results: int = 10,
               location: str | None = None) -> SearchResult:
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query is {len(query)} characters; the limit is {MAX_QUERY_CHARS}")
        payload: dict[str, Any] = {
            "query": query,
            "max_results": min(max(int(max_results), 1), MAX_SEARCH_RESULTS),
        }
        if location is not None:
            payload["location"] = _country_code(location)
        data, request_id = self._post("/v1/search", payload)
        return SearchResult(list(data.get("results") or []), request_id, data)

    def scrape(self, url: str, output: Sequence[str] = ("html",), *,
               location: str | None = None, run_js: bool | None = None,
               device: str | None = None) -> ScrapeResult:
        if not re.match(r"^https?://", url or ""):
            raise ValueError(f"url must be an absolute http(s) URL, got {url!r}")
        formats = list(output)
        unknown = set(formats) - SCRAPE_OUTPUTS
        if not formats or unknown:
            raise ValueError(f"output must be a non-empty subset of {sorted(SCRAPE_OUTPUTS)}")
        if "screenshot" in formats and not run_js:
            raise ValueError("output 'screenshot' requires run_js=True")
        payload: dict[str, Any] = {"url": url, "output": formats}
        if location is not None:
            payload["location"] = _country_code(location)
        if run_js is not None:
            payload["run_js"] = bool(run_js)
        if device is not None:
            if device not in ("desktop", "mobile"):
                raise ValueError("device must be 'desktop' or 'mobile'")
            payload["device"] = device
        data, request_id = self._post("/v1/scrape", payload)
        first = (data.get("results") or [{}])[0] or {}
        content = {fmt: first.get(fmt) for fmt in formats if fmt in first}
        return ScrapeResult(content, first.get("url"), request_id, data)

    def _post(self, path: str, payload: dict) -> tuple[dict, str | None]:
        url = self._base_url + path
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(payload).encode("utf-8")
        last_error: WebApiError | None = None
        for attempt in range(self._attempts):
            if attempt:
                delay = 2 ** (attempt - 1) + self._jitter()
                log.info("oxylabs %s: retrying in %.1fs (attempt %d/%d)",
                         path, delay, attempt + 1, self._attempts)
                self._sleep(delay)
            try:
                resp = self._transport("POST", url, headers, body, self._timeout)
            except TimeoutError as e:
                last_error = WebApiError(f"{path}: {e}", title="TIMEOUT")
                log.warning("oxylabs %s: %s", path, e)
                continue
            except ConnectionError as e:
                raise WebApiError(f"{path}: {e}", title="NETWORK_ERROR") from None

            data = _json_body(resp.body)
            request_id = (data.get("metadata") or {}).get("request_id")
            if 200 <= resp.status < 300:
                log.info("oxylabs %s: %d request_id=%s", path, resp.status, request_id)
                return data, request_id

            error = _error_from_problem(path, resp.status, data, request_id)
            log.warning("oxylabs %s: %d %s request_id=%s",
                        path, resp.status, error.title or "", request_id)
            if not _retryable(resp.status):
                raise error
            last_error = error

        assert last_error is not None
        if last_error.status == 429:
            raise RateLimitError(
                f"{path}: still 429 after {self._attempts} attempts. This is a rate limit "
                "or a spent quota; if it keeps happening, check the Oxylabs account.",
                status=429, title=last_error.title, request_id=last_error.request_id)
        raise last_error


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _country_code(value: str) -> str:
    code = value.strip().upper()
    if not _COUNTRY_CODE.match(code):
        raise ValueError(f"location must be an ISO 3166-1 alpha-2 code such as 'RU', got {value!r}")
    return code


def _json_body(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _error_from_problem(path: str, status: int, data: dict,
                        request_id: str | None) -> WebApiError:
    """Map an RFC 9457 problem body to an exception."""
    title = data.get("title") if isinstance(data.get("title"), str) else None
    detail = data.get("detail") if isinstance(data.get("detail"), str) else ""
    pointers = [e.get("pointer") for e in data.get("errors") or []
                if isinstance(e, dict) and e.get("pointer")]
    summary = f"{path} returned {status}" + (f" {title}" if title else "")
    if detail:
        summary += f": {detail}"
    kwargs = {"status": status, "title": title, "request_id": request_id}
    if status == 401:
        return AuthenticationError(
            f"{summary}. Oxylabs rejected the API key: check {KEY_ENV} "
            "(a key for a different Oxylabs product is also rejected).", **kwargs)
    if status == 400:
        if pointers:
            summary += f" (fields: {', '.join(pointers)})"
        return ValidationError(summary, pointers=pointers, **kwargs)
    return WebApiError(summary, **kwargs)
