"""Circuit breaker protecting the NHS site and bot from degraded or blocked states."""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional
import urllib.error

from jobwatch.fetcher import ChallengePageError
from jobwatch.notifier import sanitize_error
from jobwatch.storage import Storage

logger = logging.getLogger("jobwatch.circuit_breaker")


class CircuitBreaker:
    """Monitors sweep errors and enforces hard trips or failure counters with rate-limited alerts."""

    def __init__(self, storage: Storage, alert_callback: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None):
        self.storage = storage
        self.alert_callback = alert_callback

    def is_tripped(self) -> bool:
        """Returns True if the circuit breaker is currently in tripped state."""
        state = self.storage.get_circuit_breaker_state()
        return bool(state.get("is_tripped", 0))

    def get_trip_reason(self) -> Optional[str]:
        """Returns the reason string if tripped, else None."""
        state = self.storage.get_circuit_breaker_state()
        return state.get("tripped_reason") if state.get("is_tripped") else None

    async def handle_error(self, exc: Exception, context_info: str = "") -> bool:
        """Processes an exception from a sweep. Returns True if circuit breaker is now tripped."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        clean_msg = sanitize_error(str(exc))

        # 1. Hard Trip Check: 403, 429, or anti-bot challenge
        is_hard_trip = False
        trip_reason = ""

        if isinstance(exc, ChallengePageError):
            is_hard_trip = True
            trip_reason = f"Anti-bot or CAPTCHA challenge detected ({clean_msg})"
        elif isinstance(exc, urllib.error.HTTPError):
            if exc.code == 403:
                is_hard_trip = True
                trip_reason = "HTTP 403 Forbidden received from NHS Scotland"
            elif exc.code == 429:
                is_hard_trip = True
                trip_reason = "HTTP 429 Too Many Requests received from NHS Scotland"

        if is_hard_trip:
            logger.critical(f"Circuit Breaker HARD TRIP: {trip_reason} [{context_info}]")
            self.storage.update_circuit_breaker(
                is_tripped=1,
                tripped_reason=trip_reason,
                tripped_at=now_iso,
            )
            if self.alert_callback:
                alert_text = (
                    f"🚨 <b>CIRCUIT BREAKER HARD TRIP</b>\n\n"
                    f"• <b>Reason:</b> {trip_reason}\n"
                    f"• <b>Context:</b> {context_info}\n\n"
                    f"Automated sweeps have been <b>suspended immediately</b> without retrying. "
                    f"Resolve the issue and use <code>/resume_sweeps</code> to clear."
                )
                try:
                    await self.alert_callback(alert_text)
                except Exception as alert_err:
                    logger.error(f"Failed to send circuit breaker alert: {alert_err}")
            return True

        # 2. Transient Failure Check: 5xx, timeouts, connection errors
        state = self.storage.get_circuit_breaker_state()
        consecutive = state.get("consecutive_failures", 0) + 1
        last_alert_iso = state.get("last_failure_alert_at")

        # Rate-limit transient failure alert: at most once per hour
        send_alert = False
        if not last_alert_iso:
            send_alert = True
        else:
            try:
                last_alert_dt = datetime.fromisoformat(last_alert_iso)
                if (now - last_alert_dt).total_seconds() >= 3600:
                    send_alert = True
            except Exception:
                send_alert = True

        new_last_alert = now_iso if send_alert else last_alert_iso

        if consecutive >= 3:
            # 3 consecutive failures: trip the circuit breaker!
            tripped_reason = f"3 consecutive sweep failures: {clean_msg}"
            logger.critical(f"Circuit Breaker TRIPPED after 3 failures: {tripped_reason}")
            self.storage.update_circuit_breaker(
                is_tripped=1,
                tripped_reason=tripped_reason,
                tripped_at=now_iso,
                consecutive_failures=consecutive,
                last_failure_alert_at=now_iso,
            )
            if self.alert_callback:
                alert_text = (
                    f"🚨 <b>CIRCUIT BREAKER TRIPPED (3 Consecutive Failures)</b>\n\n"
                    f"• <b>Last Error:</b> <code>{clean_msg}</code>\n"
                    f"• <b>Context:</b> {context_info}\n\n"
                    f"Automated sweeps are suspended. Use <code>/resume_sweeps</code> to clear."
                )
                try:
                    await self.alert_callback(alert_text)
                except Exception as alert_err:
                    logger.error(f"Failed to send circuit breaker alert: {alert_err}")
            return True
        else:
            # Under 3 failures: record failure and optionally send hourly alert
            logger.warning(f"Transient sweep failure ({consecutive}/3): {clean_msg}")
            self.storage.update_circuit_breaker(
                consecutive_failures=consecutive,
                last_failure_alert_at=new_last_alert,
            )
            if send_alert and self.alert_callback:
                alert_text = (
                    f"⚠️ <b>Sweep Failure ({consecutive}/3)</b>\n\n"
                    f"• <b>Error:</b> <code>{clean_msg}</code>\n"
                    f"• <b>Context:</b> {context_info}\n\n"
                    f"Run will retry at next scheduled time."
                )
                try:
                    await self.alert_callback(alert_text)
                except Exception as alert_err:
                    logger.error(f"Failed to send failure alert: {alert_err}")
            return False

    def record_success(self) -> None:
        """Records a successful run, resetting consecutive failure counter."""
        self.storage.update_circuit_breaker(consecutive_failures=0)

    def resume(self) -> None:
        """Manually resets the circuit breaker, clearing tripped state and failure count."""
        self.storage.reset_circuit_breaker()
        logger.info("Circuit breaker manually reset.")

