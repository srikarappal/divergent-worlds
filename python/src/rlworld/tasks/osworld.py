"""OSWorld task import: a public suite that runs as eval (DESIGN.md sections 6.3 and 6.10).

The importer reads OSWorld's example files (examples/<domain>/<id>.json, in the
order of an index such as test_all.json when one is given) and turns every task
it can run into a suite task: the instruction, ordered guest setup steps, the
read plan of files and command outputs its evaluator needs and the evaluator
itself as data. VMs have no internet, so files a task downloads at setup and
the expected files its evaluator compares against are fetched once on the host
and kept content addressed beside the suite; expected files never enter the VM.
A task that needs a setup step, getter or metric not supported yet is listed
with the reason instead.

The suite registers as one task template whose index-th task is the suite's
index-th task. It runs only as eval, since training must not overlap public
benchmarks. OSWorld's setup and evaluator commands name /home/user and its
tools, so the eval spec's image should run its desktop as user with OSWorld's
apps, xdg-open, wmctrl and python with pyautogui.
"""

import argparse
import base64
import hashlib
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from rlworld.actions.models import ActionModel
from rlworld.guest.protocol import READ_STEP_KINDS
from rlworld.rewards.records import CheckResult
from rlworld.rewards.verifiers import FunctionVerifier, Verifier, VerifierContext
from rlworld.specs.models import DifficultySpec
from rlworld.tasks.generators import GeneratedTask, TaskGenerationError
from rlworld.tasks.osworld_evaluation import METRICS, Evaluation, Getter, MetricCall, evaluation_checks
from rlworld.worlds.generator import WORLD_GENERATORS
from rlworld.worlds.model import World

TEMPLATE_NAME = "tasks.public.osworld"
ORACLE_NAME = "oracles.public.osworld"
VERIFIER_NAME = "verifiers.public.osworld"
SUITE_FILE = "suite.json"
ASSETS_DIRECTORY = "assets"
OSWORLD_HOME = "/home/user/"
# Setup files travel base64 encoded in one inject message, which the guest caps at 64 MiB.
MAX_SETUP_BYTES = 40 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 120.0
WINDOW_COMMANDS = {"activate_window": "a", "close_window": "c"}
CONJUNCTIONS = ("and", "or")


class UnsupportedExample(ValueError):
    """An OSWorld task needs a setup step, getter or metric that is not supported yet."""


class SuiteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskReads(SuiteModel):
    """Steps that run before the reads, such as saving the open document, then the files and commands read."""

    before: tuple[dict[str, Any], ...] = ()
    files: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()


class SuiteTask(SuiteModel):
    """File setup steps name their content by asset digest; the template inlines it when it builds the task."""

    id: str
    domain: str
    instruction: str
    related_apps: tuple[str, ...] = ()
    setup: tuple[dict[str, Any], ...] = ()
    reads: TaskReads = Field(default_factory=TaskReads)
    evaluation: Evaluation


class ImportedSuite(SuiteModel):
    source: str = "osworld"
    tasks: tuple[SuiteTask, ...]
    skipped: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class OSWorldExample:
    domain: str
    example: dict[str, Any]


class AssetSource(Protocol):
    def asset(self, url: str) -> str: ...


class Fetcher(Protocol):
    def fetch(self, url: str) -> bytes: ...


class HttpFetcher:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.http_client = http_client or httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)

    def fetch(self, url: str) -> bytes:
        response = self.http_client.get(url)
        response.raise_for_status()
        return response.content


class PlannedAssets:
    """Stands in for fetching while checking that a task is supported, so unsupported tasks download nothing."""

    def asset(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()


class SuiteAssets:
    """Content addressed files beside a suite, each URL fetched once per import."""

    def __init__(self, root: Path, fetcher: Fetcher | None = None) -> None:
        self.directory = root / ASSETS_DIRECTORY
        self.fetcher = fetcher
        self.fetched: dict[str, str] = {}

    def asset(self, url: str) -> str:
        if url not in self.fetched:
            if self.fetcher is None:
                raise UnsupportedExample(f"cannot fetch {url} without a fetcher")
            try:
                content = self.fetcher.fetch(url)
            except (httpx.HTTPError, OSError) as error:
                raise UnsupportedExample(f"cannot fetch {url}: {error}") from error
            self.fetched[url] = self.store(content)
        return self.fetched[url]

    def store(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.directory / digest
        if not target.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(dir=self.directory, prefix=".incoming_")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary_path, target)
        return digest

    def read(self, digest: str) -> bytes:
        return (self.directory / digest).read_bytes()

    def size(self, digest: str) -> int:
        return (self.directory / digest).stat().st_size


def load_examples(examples_root: Path, index_path: Path | None = None) -> list[OSWorldExample]:
    """Every example by domain and file name, or only an index's examples in its order."""
    if index_path is None:
        return [OSWorldExample(path.parent.name, json.loads(path.read_text()))
                for path in sorted(examples_root.glob("*/*.json"))]
    index = json.loads(index_path.read_text())
    return [OSWorldExample(domain, json.loads((examples_root / domain / f"{example_id}.json").read_text()))
            for domain, example_ids in index.items() for example_id in example_ids]


def home_relative(path: str) -> str:
    """OSWorld names the user's home /home/user or ~; the guest resolves the rest inside its own user's home."""
    if path.startswith(OSWORLD_HOME):
        relative = path.removeprefix(OSWORLD_HOME)
    elif path.startswith("~/"):
        relative = path.removeprefix("~/")
    else:
        raise UnsupportedExample(f"{path} is outside the user's home")
    parts = PurePosixPath(relative).parts
    if not parts or ".." in parts:
        raise UnsupportedExample(f"{path} does not name a file inside the user's home")
    return PurePosixPath(*parts).as_posix()


def shell_argument(argument: str) -> str:
    """Quoted for bash; OSWorld expands a leading ~/ in an argument, as bash does with it left unquoted."""
    if argument.startswith("~/"):
        return f"~/{shlex.quote(argument.removeprefix('~/'))}"
    return shlex.quote(argument)


def shell_command(command: str | Sequence[str], shell: bool = False) -> str:
    """OSWorld runs a command without a shell unless asked, splitting a string into arguments; quoting each
    argument keeps that meaning in bash."""
    if shell and isinstance(command, str):
        return command
    arguments = shlex.split(command) if isinstance(command, str) else list(command)
    return " ".join(shell_argument(argument) for argument in arguments)


def window_command(kind: str, parameters: Mapping[str, Any]) -> str:
    """What OSWorld's VM server runs on Linux to activate or close a window."""
    flags = ("x" if parameters.get("by_class") else "") + ("F" if parameters.get("strict") else "")
    return shlex.join(["wmctrl", f"-{flags}{WINDOW_COMMANDS[kind]}", parameters["window_name"]])


def setup_step(kind: str, parameters: Mapping[str, Any], assets: AssetSource) -> list[dict[str, Any]]:
    if kind == "download":
        return [{"kind": "file", "path": home_relative(file["path"]), "asset": assets.asset(file["url"])}
                for file in parameters["files"]]
    if kind == "open":
        return [{"kind": "open", "path": home_relative(parameters["path"])}]
    if kind == "launch":
        return [{"kind": "launch", "command": shell_command(parameters["command"], parameters.get("shell", False))}]
    if kind in ("execute", "command"):
        if parameters.get("until"):
            raise UnsupportedExample(f"{kind} steps that retry until their output matches are not supported")
        return [{"kind": "execute", "command": shell_command(parameters["command"], parameters.get("shell", False))}]
    if kind == "sleep":
        return [{"kind": "sleep", "seconds": float(parameters["seconds"])}]
    if kind in WINDOW_COMMANDS:
        return [{"kind": "execute", "command": window_command(kind, parameters)}]
    raise UnsupportedExample(f"setup step {kind} is not supported")


def setup_steps(config: Iterable[Mapping[str, Any]], assets: AssetSource) -> list[dict[str, Any]]:
    steps = []
    for entry in config:
        steps.extend(setup_step(entry.get("type", ""), entry.get("parameters") or {}, assets))
    return steps


def given(config: Mapping[str, Any]) -> list[str]:
    """The paths a file getter hands its metric: one, or with multi the ones its gives indexes pick."""
    if not config.get("multi"):
        return [config["path"]]
    return [config["path"][index] for index in config.get("gives", [0])]


def getter(config: Mapping[str, Any], assets: AssetSource) -> Getter:
    kind = config.get("type")
    if kind == "vm_file":
        return Getter(kind="vm_file", paths=tuple(home_relative(path) for path in given(config)))
    if kind in ("vm_command_line", "vm_command_error"):
        return Getter(kind=kind, command=shell_command(config["command"], config.get("shell", False)))
    if kind == "rule":
        return Getter(kind="rule", rules=config["rules"])
    if kind == "cloud_file":
        return Getter(kind="cloud_file", assets=tuple(assets.asset(url) for url in given(config)))
    raise UnsupportedExample(f"getter {kind} is not supported")


def metric_lists(evaluator: Mapping[str, Any]) -> list[tuple[str, Any, Any, Any]]:
    """(func, result, expected, options) per metric, whether the evaluator names one metric or a list."""
    funcs = evaluator.get("func")
    if isinstance(funcs, str):
        return [(funcs, evaluator.get("result"), evaluator.get("expected"), evaluator.get("options") or {})]
    if not funcs:
        raise UnsupportedExample("the evaluator names no metric")
    count = len(funcs)
    results = evaluator.get("result") or [None] * count
    expecteds = evaluator.get("expected") or [None] * count
    options = evaluator.get("options") or [{}] * count
    if not len(results) == len(expecteds) == len(options) == count:
        raise UnsupportedExample("the evaluator's metric, result, expected and options lists differ in length")
    return list(zip(funcs, results, expecteds, options))


def unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def evaluation(evaluator: Mapping[str, Any], assets: AssetSource) -> tuple[Evaluation, TaskReads]:
    """The evaluator as data and the read plan that feeds it; postconfig runs before the reads."""
    if evaluator.get("func") == "infeasible":
        return Evaluation(infeasible=True), TaskReads()
    conjunction = evaluator.get("conj", "and")
    if conjunction not in CONJUNCTIONS:
        raise UnsupportedExample(f"metrics cannot combine with {conjunction!r}")
    calls = []
    for func, result, expected, options in metric_lists(evaluator):
        if func not in METRICS:
            raise UnsupportedExample(f"metric {func} is not supported")
        if not result or not expected:
            raise UnsupportedExample(f"metric {func} needs a result and an expected value")
        calls.append(MetricCall(func=func, result=getter(result, assets), expected=getter(expected, assets),
                                options=options or {}))
    before = setup_steps(evaluator.get("postconfig") or [], assets)
    unsupported_kinds = sorted({step["kind"] for step in before} - READ_STEP_KINDS)
    if unsupported_kinds:
        raise UnsupportedExample(f"evaluator postconfig cannot run {', '.join(unsupported_kinds)} steps")
    sources = [source for call in calls for source in (call.result, call.expected)]
    reads = TaskReads(before=tuple(before), files=unique(path for source in sources for path in source.paths),
                      commands=unique(source.command for source in sources if source.command))
    return Evaluation(conj=conjunction, metrics=tuple(calls)), reads


def suite_task(example: OSWorldExample, assets: AssetSource) -> SuiteTask:
    raw = example.example
    task_evaluation, reads = evaluation(raw.get("evaluator") or {}, assets)
    return SuiteTask(id=raw["id"], domain=example.domain, instruction=raw["instruction"],
                     related_apps=tuple(raw.get("related_apps") or ()),
                     setup=tuple(setup_steps(raw.get("config") or [], assets)), reads=reads,
                     evaluation=task_evaluation)


def check_setup_size(task: SuiteTask, assets: SuiteAssets) -> None:
    total_bytes = sum(assets.size(step["asset"]) for step in task.setup if step["kind"] == "file")
    if total_bytes > MAX_SETUP_BYTES:
        raise UnsupportedExample(
            f"setup files total {total_bytes} bytes, over the {MAX_SETUP_BYTES} one inject carries")


def skip_reason(error: Exception) -> str:
    if isinstance(error, KeyError):
        return f"malformed example: missing {error.args[0]!r}"
    return str(error)


def save_suite(root: Path, suite: ImportedSuite) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / SUITE_FILE).write_text(suite.model_dump_json(indent=2))


def load_suite(root: Path) -> ImportedSuite:
    return ImportedSuite.model_validate_json((root / SUITE_FILE).read_text())


def import_osworld(examples: Sequence[OSWorldExample], root: Path, fetcher: Fetcher) -> ImportedSuite:
    """Converts every example it can, fetching only for supported tasks, then saves the suite under root."""
    assets = SuiteAssets(root, fetcher)
    tasks, skipped = [], {}
    for example in examples:
        task_id = str(example.example.get("id", ""))
        try:
            suite_task(example, PlannedAssets())
            task = suite_task(example, assets)
            check_setup_size(task, assets)
        except (LookupError, TypeError, ValueError) as error:
            skipped[task_id] = skip_reason(error)
            continue
        tasks.append(task)
    suite = ImportedSuite(tasks=tuple(tasks), skipped=skipped)
    save_suite(root, suite)
    return suite


def import_report(suite: ImportedSuite) -> dict[str, Any]:
    imported: dict[str, int] = {}
    for task in suite.tasks:
        imported[task.domain] = imported.get(task.domain, 0) + 1
    return {"imported": len(suite.tasks), "imported_by_domain": imported, "skipped": len(suite.skipped),
            "skipped_reasons": suite.skipped}


class OSWorldSuite:
    """The imported suite as a task template; task setup inlines each setup file's content."""

    name = TEMPLATE_NAME
    version = "1"
    oracle_name = ORACLE_NAME
    description = "OSWorld tasks imported as a public suite; runs only as eval."
    world_generators = frozenset(WORLD_GENERATORS)

    def __init__(self, root: Path) -> None:
        self.suite = load_suite(root)
        self.assets = SuiteAssets(root)
        self.size = len(self.suite.tasks)

    def injected_step(self, step: dict[str, Any]) -> dict[str, Any]:
        if step["kind"] != "file":
            return step
        content = base64.b64encode(self.assets.read(step["asset"])).decode("ascii")
        return {"kind": "file", "path": step["path"], "base64": content}

    def generate(self, world: World, difficulty: DifficultySpec, seed: int) -> GeneratedTask:
        """The world is the run's own; OSWorld tasks bring their state through setup steps."""
        if not 0 <= seed < self.size:
            raise TaskGenerationError(f"the suite has {self.size} tasks, so there is no task {seed}")
        task = self.suite.tasks[seed]
        reads = task.reads.model_dump(mode="json") if task.reads != TaskReads() else None
        return GeneratedTask(instruction=task.instruction, world=world,
                             ground_truth={"osworld_id": task.id, "domain": task.domain,
                                           "evaluation": task.evaluation.model_dump(mode="json")},
                             setup=tuple(self.injected_step(step) for step in task.setup), reads=reads)

    def oracle(self, task: GeneratedTask) -> list[ActionModel]:
        raise TaskGenerationError("public suite tasks ship no scripted oracle")

    def verify(self, context: VerifierContext) -> list[CheckResult]:
        return evaluation_checks(Evaluation.model_validate(context.ground_truth["evaluation"]), context, self.assets)

    def verifiers(self) -> Sequence[Verifier]:
        return (FunctionVerifier(VERIFIER_NAME, self.version, "exact", self.verify),)


def main(argv: Sequence[str] | None = None, fetcher: Fetcher | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rlworld-import-osworld",
                                     description="Import OSWorld tasks as a public suite for eval runs.")
    parser.add_argument("--examples", type=Path, required=True, help="OSWorld's evaluation_examples/examples")
    parser.add_argument("--index", type=Path, default=None, help="an index such as test_all.json")
    parser.add_argument("--output", type=Path, required=True, help="the suite directory to write")
    arguments = parser.parse_args(argv)
    suite = import_osworld(load_examples(arguments.examples, arguments.index), arguments.output,
                           fetcher or HttpFetcher())
    print(json.dumps(import_report(suite), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
