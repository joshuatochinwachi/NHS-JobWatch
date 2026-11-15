"""HTTP fetcher for NHS Scotland vacancy pages adhering to strict access constraints."""

import time
import urllib.parse
import urllib.request
from typing import Optional

from jobwatch.config import (
    LISTING_ENDPOINT,
    MIN_REQUEST_INTERVAL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)


class FetcherError(Exception):
    """Base exception for fetcher errors."""
    pass


class ChallengePageError(FetcherError):
    """Raised when an anti-bot or CAPTCHA challenge page is detected."""
    pass


CHALLENGE_PATTERNS = [
    "cf-browser-verification",
    "challenge-running",
    'id="challenge-form"',
    "hcaptcha.com",
    "recaptcha",
    "just a moment...",
    "attention required! | cloudflare",
]


class Fetcher:
    """HTTP client enforcing polite access constraints (Decision D-006, D-009).

    - Honest User-Agent identifying the tool
    - Minimum 3.0s floor between requests
    - Single attempt, no retries, no evasion loops
    - Explicit timeouts
    """

    def __init__(self, user_agent: str = USER_AGENT, min_interval: float = MIN_REQUEST_INTERVAL_SECONDS):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._last_request_time: Optional[float] = None

    def _wait_for_rate_limit(self) -> None:
        """Enforces the minimum interval floor between sequential requests."""
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

    def fetch_url(self, url: str) -> str:
        """Fetches a URL via a single plain HTTP GET request."""
        self._wait_for_rate_limit()

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )

        try:
            self._last_request_time = time.time()
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                content = response.read().decode(charset, errors="replace")
                lower = content.lower()
                for pat in CHALLENGE_PATTERNS:
                    if pat in lower:
                        raise ChallengePageError(f"Anti-bot or CAPTCHA challenge detected (pattern '{pat}')")
                return content
        except Exception:
            self._last_request_time = time.time()
            raise

    def fetch_listing(self, department_id: int = 9, skip: int = 0) -> str:
        """Fetches a listing partial from /Home/_JobCard."""
        params = urllib.parse.urlencode({
            "Departments": department_id,
            "Skip": skip,
        })
        url = f"{LISTING_ENDPOINT}?{params}"
        return self.fetch_url(url)

    def fetch_scope(self, scope_key: str, skip: int = 0) -> str:
        """Fetches a page for an arbitrary scope_key (e.g. 'Departments=9' or 'what=nurse')."""
        pairs = urllib.parse.parse_qsl(scope_key)
        params = dict(pairs)
        params["Skip"] = str(skip)
        query = urllib.parse.urlencode(params)
        url = f"{LISTING_ENDPOINT}?{query}"
        return self.fetch_url(url)
