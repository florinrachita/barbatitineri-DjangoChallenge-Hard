"""Small HTTP client for the challenge host."""

import logging
from urllib.parse import urljoin

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ChallengeAPIError(Exception):
    pass


def build_url(path):
    return urljoin(settings.CHALLENGE_HOST.rstrip("/") + "/", path.lstrip("/"))


def get_json(path_or_url):
    """GET a path (or absolute URL) on the challenge host and return decoded JSON."""
    url = path_or_url if path_or_url.startswith("http") else build_url(path_or_url)
    try:
        response = requests.get(url, timeout=settings.CHALLENGE_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.Timeout as exc:
        raise ChallengeAPIError(f"Timed out fetching {url}") from exc
    except requests.HTTPError as exc:
        raise ChallengeAPIError(
            f"HTTP {exc.response.status_code} from {url}: {exc.response.text[:200]}"
        ) from exc
    except requests.RequestException as exc:
        raise ChallengeAPIError(f"Request to {url} failed: {exc}") from exc
    except ValueError as exc:
        raise ChallengeAPIError(f"Invalid JSON from {url}") from exc


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
