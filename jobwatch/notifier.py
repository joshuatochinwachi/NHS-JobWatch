"""Notifier interface, Telegram implementation, FakeNotifier, and DryRunNotifier."""

import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# Sentinel for sanitizing log and error messages
_TOKEN_PATTERN = re.compile(r"https?://api\.telegram\.org/bot[^/\s]+/", re.IGNORECASE)
_REDACTED = "https://api.telegram.org/bot[REDACTED]/"

# Permanent Telegram error codes — stop sending, do not retry
PERMANENT_CODES = {400, 401, 403, 404}


def sanitize_error(text: str) -> str:
    """Strips bot tokens and Telegram API URLs from error text and log strings."""
    return _TOKEN_PATTERN.sub(_REDACTED, text)


class NotifierError(Exception):
    """Base error for notifier failures."""
    pass


class PermanentNotifierError(NotifierError):
    """Raised for permanent Telegram errors (bad token, chat not found, bot blocked)."""
    pass


class TransientNotifierError(NotifierError):
    """Raised for transient Telegram errors (timeout, rate-limit, server error)."""
    pass


class Notifier(ABC):
    """Abstract notifier interface."""

    @abstractmethod
    def send(self, text: str) -> bool:
        """Sends a single message. Returns True on confirmed delivery."""
        raise NotImplementedError

    @abstractmethod
    def send_batch(self, texts: List[str]) -> Tuple[int, int]:
        """Sends multiple messages. Returns (sent_count, failed_count)."""
        raise NotImplementedError


@dataclass
class FakeNotifier(Notifier):
    """In-memory notifier for unit tests. Never makes network calls."""
    sent: List[str] = field(default_factory=list)
    fail_on_send: bool = False
    permanent_fail: bool = False

    def send(self, text: str) -> bool:
        if self.permanent_fail:
            raise PermanentNotifierError("FakeNotifier: simulated permanent failure")
        if self.fail_on_send:
            raise TransientNotifierError("FakeNotifier: simulated transient failure")
        self.sent.append(text)
        return True

    def send_batch(self, texts: List[str]) -> Tuple[int, int]:
        sent, failed = 0, 0
        for text in texts:
            try:
                self.send(text)
                sent += 1
            except NotifierError:
                failed += 1
        return sent, failed


class DryRunNotifier(Notifier):
    """Prints messages to stdout without making any network calls."""

    def send(self, text: str) -> bool:
        print("=" * 60)
        print("[DRY-RUN] Would send Telegram message:")
        print("=" * 60)
        print(text)
        print("=" * 60)
        return True

    def send_batch(self, texts: List[str]) -> Tuple[int, int]:
        for text in texts:
            self.send(text)
        return len(texts), 0


class TelegramNotifier(Notifier):
    """Sends messages to a Telegram chat using urllib (no external dependencies)."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        min_send_interval: float = 1.5,
        timeout: float = 15.0,
        max_permanent_failures: int = 3,
    ):
        if not token:
            raise PermanentNotifierError("TELEGRAM_BOT_TOKEN is missing or empty")
        if not chat_id:
            raise PermanentNotifierError("TELEGRAM_CHAT_ID is missing or empty")
        self._token = token
        self._chat_id = chat_id
        self._min_interval = min_send_interval
        self._timeout = timeout
        self._max_permanent_failures = max_permanent_failures
        self._last_sent_at: float = 0.0
        self._consecutive_permanent_failures: int = 0

    def _api_url(self, method: str) -> str:
        # Token never appears in sanitized output
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _post(self, method: str, payload: dict) -> dict:
        """Makes a single POST to the Telegram Bot API. Raises NotifierError on failure."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._api_url(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            safe_url = sanitize_error(str(e.url) if hasattr(e, "url") else "")
            msg = f"Telegram HTTP {e.code}: {e.reason} [{safe_url}]"
            if e.code in PERMANENT_CODES:
                raise PermanentNotifierError(msg) from None
            elif e.code == 429:
                retry_after = 5
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    retry_after = err_body.get("parameters", {}).get("retry_after", 5)
                except Exception:
                    pass
                raise TransientNotifierError(f"{msg} retry_after={retry_after}") from None
            else:
                raise TransientNotifierError(msg) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise TransientNotifierError(f"Telegram network error: {sanitize_error(str(e))}") from None

    def _rate_limit_wait(self) -> None:
        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

    def send(self, text: str) -> bool:
        """Sends a message, handling rate limits and classifying errors."""
        if self._consecutive_permanent_failures >= self._max_permanent_failures:
            raise PermanentNotifierError(
                f"Halted: {self._consecutive_permanent_failures} consecutive permanent Telegram failures"
            )

        self._rate_limit_wait()
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}

        try:
            self._post("sendMessage", payload)
            self._consecutive_permanent_failures = 0
            self._last_sent_at = time.monotonic()
            return True
        except PermanentNotifierError:
            self._consecutive_permanent_failures += 1
            if self._consecutive_permanent_failures >= self._max_permanent_failures:
                raise PermanentNotifierError(
                    f"ADMIN ALERT: {self._consecutive_permanent_failures} consecutive permanent Telegram delivery failures"
                )
            raise
        except TransientNotifierError as e:
            # Check if 429 with retry_after <= 30
            m = re.search(r"retry_after=(\d+)", str(e))
            if m:
                retry_after = int(m.group(1))
                if retry_after <= 30:
                    time.sleep(retry_after + 1)
                    # Retry once
                    try:
                        self._post("sendMessage", payload)
                        self._last_sent_at = time.monotonic()
                        return True
                    except (TransientNotifierError, PermanentNotifierError):
                        pass
            raise

    def send_batch(self, texts: List[str]) -> Tuple[int, int]:
        sent, failed = 0, 0
        for text in texts:
            try:
                self.send(text)
                sent += 1
            except PermanentNotifierError:
                failed += 1
                break  # Stop sending on permanent error
            except TransientNotifierError:
                failed += 1
        return sent, failed

    def find_chat_ids(self) -> List[dict]:
        """Calls getUpdates once and returns list of dicts with chat_id and chat_type."""
        result = self._post("getUpdates", {})
        chats = []
        seen = set()
        for update in result.get("result", []):
            for key in ("message", "channel_post", "my_chat_member"):
                msg = update.get(key)
                if msg and "chat" in msg:
                    chat = msg["chat"]
                    cid = chat.get("id")
                    ctype = chat.get("type", "unknown")
                    if cid and cid not in seen:
                        seen.add(cid)
                        chats.append({"chat_id": cid, "chat_type": ctype})
        return chats

