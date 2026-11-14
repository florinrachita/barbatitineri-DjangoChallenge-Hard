"""Small HTTP client for the challenge host."""

import logging
import time
from urllib.parse import urljoin

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10  # seconds


class ChallengeAPIError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


# Azure's front end answers 504 or 499 when the app takes longer than ~240s.
RETRYABLE_STATUSES = {408, 425, 429, 499}


def is_retryable_status(status_code):
    return status_code >= 500 or status_code in RETRYABLE_STATUSES


def build_url(path):
    return urljoin(settings.CHALLENGE_HOST.rstrip("/") + "/", path.lstrip("/"))


def get_json(path_or_url, timeout=None):
    """GET a path (or absolute URL) on the challenge host and return decoded JSON.

    Raises ChallengeAPIError; `retryable` is True for timeouts, connection errors, 5xx
    and gateway/rate-limit statuses.
    """
    url = path_or_url if path_or_url.startswith("http") else build_url(path_or_url)
    read_timeout = timeout or settings.CHALLENGE_TIMEOUT
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=(CONNECT_TIMEOUT, read_timeout))
    except requests.Timeout as exc:
        raise ChallengeAPIError(f"Timed out after {read_timeout}s: {url}", retryable=True) from exc
    except requests.ConnectionError as exc:
        raise ChallengeAPIError(f"Connection error for {url}: {exc}", retryable=True) from exc
    except requests.RequestException as exc:
        raise ChallengeAPIError(f"Request to {url} failed: {exc}") from exc

    logger.info("GET %s -> HTTP %s in %.1fs", url, response.status_code, time.monotonic() - started)
    if response.status_code >= 400:
        raise ChallengeAPIError(
            f"HTTP {response.status_code} from {url}: {response.text[:200]}",
            retryable=is_retryable_status(response.status_code),
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ChallengeAPIError(f"Invalid JSON from {url}", retryable=True) from exc

    # The API also reports errors in the body, e.g. {"data": {...}, "status": 400} with HTTP 200.
    status = payload.get("status") if isinstance(payload, dict) else None
    if isinstance(status, int) and status >= 400:
        raise ChallengeAPIError(
            f"API status {status} from {url}: {payload.get('data')}", retryable=is_retryable_status(status)
        )
    return payload


def fetch_people():
    """Return every person from /people, following a 'next' link if the API ever adds one."""
    people, url = [], "people"
    while url:
        payload = get_json(url)
        if isinstance(payload, list):
            return people + payload
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ChallengeAPIError(f"Unexpected /people payload shape: {type(payload).__name__}")
        people.extend(payload["data"])
        url = payload.get("next")
    logger.info("Fetched %d people", len(people))
    return people


def fetch_batch(path, number):
    """Return a <host>/<path>/<number> payload: {"data": [...], "next_batch": int | None}."""
    payload = get_json(f"{path}/{number}", timeout=settings.CHALLENGE_BATCH_TIMEOUT)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ChallengeAPIError(f"Unexpected /{path}/{number} payload: {str(payload)[:200]}")
    return payload
