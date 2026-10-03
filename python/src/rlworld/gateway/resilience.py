"""Token buckets, circuit breakers and jittered retry delays, with injectable clocks for tests."""

import random
import threading
from collections.abc import Callable
from typing import Literal

from rlworld.gateway.errors import CircuitOpenError, RateLimitTimeoutError

CircuitState = Literal["closed", "open", "half_open"]


class TokenBucket:
    """Holds one minute of capacity and refills continuously; can run into debt after a large real call."""

    def __init__(self, per_minute: int, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self.tokens = float(per_minute)
        self.clock = clock
        self.sleep = sleep
        self.last_refill = clock()
        self.lock = threading.Lock()

    def refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.refill_per_second)
        self.last_refill = now

    def available(self) -> float:
        with self.lock:
            self.refill()
            return self.tokens

    def acquire(self, amount: float, timeout_seconds: float) -> float:
        """Take amount, waiting if needed; returns the seconds waited."""
        wanted = min(amount, self.capacity)
        waited_seconds = 0.0
        while True:
            with self.lock:
                self.refill()
                if self.tokens >= wanted:
                    self.tokens -= wanted
                    return waited_seconds
                wait_seconds = (wanted - self.tokens) / self.refill_per_second
            if waited_seconds + wait_seconds > timeout_seconds:
                raise RateLimitTimeoutError(f"rate limit would need {wait_seconds:.1f}s more than the timeout allows")
            self.sleep(wait_seconds)
            waited_seconds += wait_seconds

    def adjust(self, extra_amount: float) -> None:
        """Charge (positive) or refund (negative) the gap between estimated and real usage."""
        with self.lock:
            self.tokens = min(self.capacity, self.tokens - extra_amount)


class CircuitBreaker:
    def __init__(self, failure_threshold: int, cooldown_seconds: float, clock: Callable[[], float]) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.state: CircuitState = "closed"
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.trial_in_flight = False
        self.lock = threading.Lock()

    def before_call(self) -> None:
        with self.lock:
            if self.state == "open" and self.clock() - self.opened_at >= self.cooldown_seconds:
                self.state = "half_open"
                self.trial_in_flight = False
            if self.state == "open" or (self.state == "half_open" and self.trial_in_flight):
                raise CircuitOpenError("the endpoint is cooling down after repeated failures")
            if self.state == "half_open":
                self.trial_in_flight = True

    def record_success(self) -> None:
        with self.lock:
            self.state = "closed"
            self.consecutive_failures = 0
            self.trial_in_flight = False

    def record_failure(self) -> None:
        with self.lock:
            self.consecutive_failures += 1
            if self.state == "half_open" or self.consecutive_failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = self.clock()
                self.trial_in_flight = False

    def release_trial(self) -> None:
        """A half open trial that ended without an endpoint verdict frees the slot for the next one."""
        with self.lock:
            self.trial_in_flight = False


def retry_delay(
    attempt_index: int,
    retry_after_seconds: float | None,
    base_seconds: float,
    max_seconds: float,
    random_generator: random.Random,
) -> float:
    """Full jitter exponential backoff, never shorter than a server's Retry-After."""
    jittered_seconds = random_generator.uniform(0, min(max_seconds, base_seconds * 2**attempt_index))
    return max(retry_after_seconds or 0.0, jittered_seconds)
