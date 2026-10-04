"""Split integrity for eval (DESIGN.md section 6.10).

Whole apps, sites, templates and world seeds are held out for eval, so a
training spec may touch none of them; a held out site covers its subdomains.
Training must not overlap public benchmarks either: the instruction of every
task generated for training is compared with the instructions of OSWorld,
Windows Agent Arena, WebArena and WorkArena by word shingles; a near
duplicate is never registered for training.
"""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rlworld.specs.document import SpecDocument
from rlworld.tasks.generators import GeneratedTask

WORD_PATTERN = re.compile(r"[a-z0-9]+")
DEFAULT_SHINGLE_SIZE = 3
DEFAULT_OVERLAP_THRESHOLD = 0.5

Shingle = tuple[str, ...]


class HoldoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    apps: frozenset[str] = frozenset()
    sites: frozenset[str] = frozenset()
    templates: frozenset[str] = frozenset()
    world_seeds: frozenset[int] = frozenset()


def held_out_site(hostname: str, sites: Iterable[str]) -> bool:
    return any(hostname == site or hostname.endswith(f".{site}") for site in sites)


def holdout_problems(document: SpecDocument, policy: HoldoutPolicy) -> list[str]:
    """What a training spec takes from the eval holdout; eval and holdout specs may use anything."""
    if document.task.split != "train":
        return []
    problems = []
    apps = sorted((set(document.world.materialize) | set(document.image.apps)) & policy.apps)
    if apps:
        problems.append(f"apps {', '.join(apps)} are held out for eval")
    network = document.world.network
    sites = sorted(host for host in {*network.dns_overrides, *network.allowlist} if held_out_site(host, policy.sites))
    if sites:
        problems.append(f"sites {', '.join(sites)} are held out for eval")
    if document.task.generator in policy.templates:
        problems.append(f"template {document.task.generator} is held out for eval")
    if document.world.seed in policy.world_seeds:
        problems.append(f"world seed {document.world.seed} is held out for eval")
    return problems


@dataclass(frozen=True)
class BenchmarkTask:
    benchmark: str
    task_id: str
    instruction: str


@dataclass(frozen=True)
class BenchmarkMatch:
    task: BenchmarkTask
    similarity: float


def load_benchmark(name: str, path: Path) -> list[BenchmarkTask]:
    """A folder of OSWorld style examples, as OSWorld and Windows Agent Arena ship them, or a JSON list of tasks
    with an instruction or a WebArena intent, as in WebArena's test.raw.json or an export of WorkArena's goals."""
    if path.is_dir():
        examples = [json.loads(example_path.read_text()) for example_path in sorted(path.glob("*/*.json"))]
        return [BenchmarkTask(name, str(example["id"]), example["instruction"]) for example in examples]
    records = json.loads(path.read_text())
    return [BenchmarkTask(name, str(record.get("task_id", record.get("id", index))),
                          record["instruction"] if "instruction" in record else record["intent"])
            for index, record in enumerate(records)]


def shingles(text: str, size: int = DEFAULT_SHINGLE_SIZE) -> frozenset[Shingle]:
    """Runs of size words, lowercased without punctuation; a shorter text is one shingle of all its words."""
    words = WORD_PATTERN.findall(text.lower())
    if len(words) <= size:
        return frozenset({tuple(words)}) if words else frozenset()
    return frozenset(tuple(words[start:start + size]) for start in range(len(words) - size + 1))


class OverlapIndex:
    """Benchmark instructions by shingle, so a text is compared only with tasks it shares a shingle with."""

    def __init__(self, tasks: Sequence[BenchmarkTask], threshold: float = DEFAULT_OVERLAP_THRESHOLD,
                 shingle_size: int = DEFAULT_SHINGLE_SIZE) -> None:
        self.tasks = tuple(tasks)
        self.threshold = threshold
        self.shingle_size = shingle_size
        self.task_shingles = [shingles(task.instruction, shingle_size) for task in self.tasks]
        self.postings: dict[Shingle, list[int]] = defaultdict(list)
        for index, task_shingles in enumerate(self.task_shingles):
            for shingle in task_shingles:
                self.postings[shingle].append(index)

    def matches(self, text: str) -> list[BenchmarkMatch]:
        """Benchmark tasks whose shingle Jaccard similarity with text reaches the threshold, closest first."""
        query = shingles(text, self.shingle_size)
        shared = Counter(index for shingle in query for index in self.postings.get(shingle, ()))
        found = []
        for index, count in shared.items():
            similarity = count / (len(query) + len(self.task_shingles[index]) - count)
            if similarity >= self.threshold:
                found.append(BenchmarkMatch(self.tasks[index], similarity))
        return sorted(found, key=attrgetter("similarity"), reverse=True)


class BenchmarkScreen:
    """Keeps near duplicates of public benchmark tasks out of training."""

    def __init__(self, index: OverlapIndex) -> None:
        self.index = index

    def blocked(self, task: GeneratedTask) -> str | None:
        found = self.index.matches(task.instruction)
        if not found:
            return None
        closest = found[0]
        return f"overlaps {closest.task.benchmark} task {closest.task.task_id} at {closest.similarity:.2f}"
