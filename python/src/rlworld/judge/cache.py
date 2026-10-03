"""Judge verdict cache (DESIGN.md section 6.9).

Keyed by final state hash, rubric version and judge model, plus the
instruction and reference digests so two tasks never share a verdict. GRPO
groups that converge on the same final state pay for one judge call.
"""

import threading
from dataclasses import dataclass
from typing import Protocol

from rlworld.rewards.records import CheckResult


@dataclass(frozen=True)
class JudgeCacheKey:
    state_hash: str
    rubric_id: str
    rubric_version: str
    judge_model: str
    instruction_digest: str
    reference_digest: str


class JudgeCache(Protocol):
    def get(self, key: JudgeCacheKey) -> tuple[CheckResult, ...] | None: ...

    def put(self, key: JudgeCacheKey, checks: tuple[CheckResult, ...]) -> None: ...


class InMemoryJudgeCache:
    def __init__(self) -> None:
        self.entries: dict[JudgeCacheKey, tuple[CheckResult, ...]] = {}
        self.hits = 0
        self.misses = 0
        self.lock = threading.Lock()

    def get(self, key: JudgeCacheKey) -> tuple[CheckResult, ...] | None:
        with self.lock:
            checks = self.entries.get(key)
            if checks is None:
                self.misses += 1
            else:
                self.hits += 1
            return checks

    def put(self, key: JudgeCacheKey, checks: tuple[CheckResult, ...]) -> None:
        with self.lock:
            self.entries[key] = checks
