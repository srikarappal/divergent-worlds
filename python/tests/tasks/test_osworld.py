import base64
import copy
import hashlib
import json
import shlex
from pathlib import Path

import httpx
import pytest
import yaml
from support.guest_fakes import ScriptedShell, WindowsAppear, build_agent

from rlworld.actions.models import TerminalOutput
from rlworld.rewards import EpisodeOutcome, evaluate_episode
from rlworld.sdk.client import SnapshotSource
from rlworld.specs import parse_spec_document, spec_digest
from rlworld.specs.models import DifficultySpec
from rlworld.tasks import osworld
from rlworld.tasks.generators import TaskGenerationError, TaskSuite
from rlworld.tasks.osworld import (
    ORACLE_NAME,
    TEMPLATE_NAME,
    VERIFIER_NAME,
    OSWorldSuite,
    PlannedAssets,
    SuiteAssets,
    TaskReads,
    UnsupportedExample,
    given,
    home_relative,
    import_osworld,
    import_report,
    load_examples,
    load_suite,
    setup_step,
    shell_command,
    window_command,
)
from rlworld.tasks.osworld_evaluation import Getter
from rlworld.tasks.registry import RegistryTaskProvider, StaticSnapshots, default_task_registry
from rlworld.worlds import generate_mid_market_company

EXAMPLE_SPEC = Path(__file__).resolve().parents[3] / "examples" / "specs" / "suite_osworld_eval.yaml"
SNAPSHOT = SnapshotSource(digest="sha256:" + "7a" * 32, state_path="", memory_path="", base_disk_path="")
NOTES_URL = "https://drive.example/notes.txt"
GOLD_URL = "https://drive.example/notes_gold.txt"
SHARED_URL = "https://drive.example/shared.txt"
TABLE_URL = "https://drive.example/table.xlsx"
BROKEN_URL = "https://drive.example/gone.bin"
NOTES = b"Meeting at 10\n\nbring budget\n"
GOLD = b"Meeting at 10\nBring the budget\n"
SHARED = b"shared\n"
SAVE_COMMAND = ["python", "-c", "import pyautogui; pyautogui.hotkey('ctrl', 's')"]
FILES = {NOTES_URL: NOTES, GOLD_URL: GOLD, SHARED_URL: SHARED, TABLE_URL: b"PK\x03\x04 workbook"}


def digest_of(content):
    return hashlib.sha256(content).hexdigest()


def download(url, path):
    return {"type": "download", "parameters": {"files": [{"url": url, "path": path}]}}


def rule(rules):
    return {"type": "rule", "rules": rules}


def command_line(command, shell=False):
    return {"type": "vm_command_line", "command": command, "shell": shell}


EXAMPLES = {
    ("libreoffice_writer", "w1"): {
        "id": "w1", "snapshot": "libreoffice_writer", "instruction": "Tidy the meeting notes on the desktop.",
        "source": "https://help.example/notes", "related_apps": ["libreoffice_writer"],
        "config": [download(NOTES_URL, "/home/user/Desktop/notes.txt"),
                   {"type": "open", "parameters": {"path": "/home/user/Desktop/notes.txt"}}],
        "evaluator": {
            "postconfig": [{"type": "activate_window",
                            "parameters": {"window_name": "notes.txt - LibreOffice Writer", "strict": True}},
                           {"type": "sleep", "parameters": {"seconds": 0.5}},
                           {"type": "execute", "parameters": {"command": SAVE_COMMAND}}],
            "func": "compare_text_file",
            "result": {"type": "vm_file", "path": "/home/user/Desktop/notes.txt", "dest": "notes.txt"},
            "expected": {"type": "cloud_file", "path": GOLD_URL, "dest": "gold.txt"},
            "options": {"ignore_blanks": True}}},
    ("os", "o1"): {
        "id": "o1", "snapshot": "os", "instruction": "Install Spotify.",
        "config": [{"type": "execute", "parameters": {"command": ["python", "-c", "print('ready')"]}}],
        "evaluator": {"func": "check_include_exclude", "result": command_line("which spotify", shell=True),
                      "expected": rule({"include": ["spotify"], "exclude": ["not found"]})}},
    ("os", "o2"): {"id": "o2", "snapshot": "os", "instruction": "Turn the moon purple.", "config": [],
                   "evaluator": {"func": "infeasible"}},
    ("vs_code", "v1"): {
        "id": "v1", "snapshot": "vscode", "instruction": "Make the editor font 14.",
        "config": [{"type": "launch", "parameters": {"command": ["code"]}},
                   {"type": "sleep", "parameters": {"seconds": 2}}],
        "evaluator": {"func": "check_json_settings",
                      "result": {"type": "vm_file", "path": "/home/user/.config/Code/User/settings.json",
                                 "dest": "settings.json"},
                      "expected": rule({"expected": {"editor.fontSize": 14}})}},
    ("multi_apps", "m1"): {
        "id": "m1", "snapshot": "multi_apps", "instruction": "Write the answer or keep the report.",
        "config": [download(SHARED_URL, "/home/user/Desktop/shared.txt")],
        "evaluator": {"func": ["exact_match", "check_include_exclude"], "conj": "or",
                      "result": [command_line("cat ~/answer.txt", shell=True),
                                 command_line(["ls", "/home/user/Desktop"])],
                      "expected": [rule({"expected": "42\n"}), rule({"include": ["report.pdf"]})],
                      "options": [{}, {}]}},
    ("multi_apps", "m2"): {
        "id": "m2", "snapshot": "multi_apps", "instruction": "Keep the shared file.",
        "config": [download(SHARED_URL, "/home/user/Desktop/shared.txt")],
        "evaluator": {"func": "exact_match", "result": command_line("cat /home/user/Desktop/shared.txt"),
                      "expected": rule({"expected": "shared\n"})}},
    ("multi_apps", "m4"): {
        "id": "m4", "snapshot": "multi_apps", "instruction": "Copy the notes into b.txt.", "config": [],
        "evaluator": {"func": "compare_text_file",
                      "result": {"type": "vm_file", "multi": True, "path": ["/home/user/a.txt", "/home/user/b.txt"],
                                 "dest": ["a.txt", "b.txt"], "gives": [1]},
                      "expected": {"type": "cloud_file", "multi": True, "path": [GOLD_URL, NOTES_URL],
                                   "dest": ["gold.txt", "notes.txt"], "gives": [0]}}},
    ("libreoffice_calc", "c1"): {
        "id": "c1", "snapshot": "libreoffice_calc", "instruction": "Pad the ids.",
        "config": [download(TABLE_URL, "/home/user/table.xlsx")],
        "evaluator": {"func": "compare_table", "result": {"type": "vm_file", "path": "/home/user/table.xlsx"},
                      "expected": {"type": "cloud_file", "path": TABLE_URL}}},
    ("chrome", "ch1"): {
        "id": "ch1", "snapshot": "chrome", "instruction": "Open the docs.", "config": [],
        "evaluator": {"func": "exact_match", "result": {"type": "active_url_from_accessTree"},
                      "expected": rule({"expected": "https://docs.example"})}},
    ("gimp", "g1"): {
        "id": "g1", "snapshot": "gimp", "instruction": "Brighten the photo.",
        "config": [{"type": "googledrive", "parameters": {"settings_file": "settings.yml"}}],
        "evaluator": {"func": "exact_match", "result": command_line("ls"), "expected": rule({"expected": ""})}},
    ("multi_apps", "m3"): {
        "id": "m3", "snapshot": "multi_apps", "instruction": "Use the archive.",
        "config": [download(BROKEN_URL, "/home/user/archive.bin")],
        "evaluator": {"func": "exact_match", "result": command_line("ls"), "expected": rule({"expected": ""})}},
    ("os", "o3"): {
        "id": "o3", "snapshot": "os", "instruction": "Sum the column.",
        "config": [download(SHARED_URL, "/tmp/data.csv")],
        "evaluator": {"func": "exact_match", "result": command_line("ls"), "expected": rule({"expected": ""})}},
    ("vlc", "vl1"): {
        "id": "vl1", "snapshot": "vlc", "instruction": "Play the video.", "config": [],
        "evaluator": {"postconfig": [{"type": "launch", "parameters": {"command": ["vlc"]}}],
                      "func": "exact_match", "result": command_line("ls"), "expected": rule({"expected": ""})}},
    ("os", "o4"): {"id": "o4", "snapshot": "os", "config": [],
                   "evaluator": {"func": "exact_match", "result": command_line("ls"),
                                 "expected": rule({"expected": ""})}},
    ("os", "o5"): {
        "id": "o5", "snapshot": "os", "instruction": "Wait for the service.",
        "config": [{"type": "execute", "parameters": {"command": "systemctl is-active x",
                                                      "until": {"returncode": 0}}}],
        "evaluator": {"func": "exact_match", "result": command_line("ls"), "expected": rule({"expected": ""})}},
}
INDEX = {"libreoffice_writer": ["w1"], "os": ["o1", "o2", "o3", "o4", "o5"], "vs_code": ["v1"],
         "multi_apps": ["m1", "m2", "m3", "m4"], "libreoffice_calc": ["c1"], "chrome": ["ch1"], "gimp": ["g1"],
         "vlc": ["vl1"]}
IMPORTED = ["w1", "o1", "o2", "v1", "m1", "m2", "m4"]


class FakeFetcher:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.files:
            raise httpx.ConnectError(f"cannot reach {url}")
        return self.files[url]


def write_examples(root, examples=None, index=None):
    examples_root = root / "examples"
    for (domain, example_id), example in (examples or EXAMPLES).items():
        (examples_root / domain).mkdir(parents=True, exist_ok=True)
        (examples_root / domain / f"{example_id}.json").write_text(json.dumps(example))
    index_path = root / "test_all.json"
    index_path.write_text(json.dumps(index or INDEX))
    return examples_root, index_path


@pytest.fixture
def imported(tmp_path):
    examples_root, index_path = write_examples(tmp_path)
    fetcher = FakeFetcher(FILES)
    suite = import_osworld(load_examples(examples_root, index_path), tmp_path / "suite", fetcher)
    return suite, fetcher, tmp_path / "suite"


def task_by_id(suite, task_id):
    return next(task for task in suite.tasks if task.id == task_id)


def test_examples_load_by_domain_or_in_index_order(tmp_path):
    examples_root, index_path = write_examples(tmp_path)
    by_domain = load_examples(examples_root)
    assert [example.domain for example in by_domain][:3] == ["chrome", "gimp", "libreoffice_calc"]
    assert len(by_domain) == len(EXAMPLES)
    indexed = load_examples(examples_root, index_path)
    assert [example.example["id"] for example in indexed][:3] == ["w1", "o1", "o2"]
    subset = tmp_path / "test_small.json"
    subset.write_text(json.dumps({"os": ["o2"], "vs_code": ["v1"]}))
    assert [(example.domain, example.example["id"]) for example in load_examples(examples_root, subset)] == [
        ("os", "o2"), ("vs_code", "v1")]


def test_import_keeps_supported_tasks_and_lists_the_rest_with_reasons(imported):
    suite, fetcher, root = imported
    assert [task.id for task in suite.tasks] == IMPORTED
    assert set(suite.skipped) == {"c1", "ch1", "g1", "m3", "o3", "vl1", "o4", "o5"}
    assert suite.skipped["c1"] == "metric compare_table is not supported"
    assert suite.skipped["ch1"] == "getter active_url_from_accessTree is not supported"
    assert suite.skipped["g1"] == "setup step googledrive is not supported"
    assert suite.skipped["m3"].startswith(f"cannot fetch {BROKEN_URL}: cannot reach")
    assert suite.skipped["o3"] == "/tmp/data.csv is outside the user's home"
    assert suite.skipped["vl1"] == "evaluator postconfig cannot run launch steps"
    assert suite.skipped["o4"] == "malformed example: missing 'instruction'"
    assert "retry until their output matches" in suite.skipped["o5"]
    assert TABLE_URL not in fetcher.calls
    assert sorted(fetcher.calls) == sorted([NOTES_URL, GOLD_URL, SHARED_URL, BROKEN_URL])
    assert load_suite(root) == suite


def test_imported_tasks_keep_setup_reads_and_evaluator(imported):
    suite, _, _ = imported
    notes = task_by_id(suite, "w1")
    assert (notes.domain, notes.related_apps) == ("libreoffice_writer", ("libreoffice_writer",))
    assert notes.setup == ({"kind": "file", "path": "Desktop/notes.txt", "asset": digest_of(NOTES)},
                           {"kind": "open", "path": "Desktop/notes.txt"})
    assert notes.reads == TaskReads(
        before=({"kind": "execute", "command": "wmctrl -Fa 'notes.txt - LibreOffice Writer'"},
                {"kind": "sleep", "seconds": 0.5},
                {"kind": "execute", "command": shlex.join(SAVE_COMMAND)}),
        files=("Desktop/notes.txt",))
    metric = notes.evaluation.metrics[0]
    assert (metric.func, metric.options) == ("compare_text_file", {"ignore_blanks": True})
    assert metric.result == Getter(kind="vm_file", paths=("Desktop/notes.txt",))
    assert metric.expected == Getter(kind="cloud_file", assets=(digest_of(GOLD),))
    spotify = task_by_id(suite, "o1")
    assert spotify.setup == ({"kind": "execute", "command": "python -c 'print('\"'\"'ready'\"'\"')'"},)
    assert spotify.reads == TaskReads(commands=("which spotify",))
    either = task_by_id(suite, "m1")
    assert either.evaluation.conj == "or" and either.reads.commands == ("cat ~/answer.txt", "ls /home/user/Desktop")
    chosen = task_by_id(suite, "m4")
    assert chosen.reads.files == ("b.txt",)
    assert chosen.evaluation.metrics[0].expected.assets == (digest_of(GOLD),)
    infeasible = task_by_id(suite, "o2")
    assert infeasible.evaluation.infeasible and infeasible.reads == TaskReads()


def test_fetched_files_are_stored_once_by_content(imported):
    suite, _, root = imported
    stored = {path.name: path.read_bytes() for path in (root / "assets").iterdir()}
    assert stored == {digest_of(content): content for content in (NOTES, GOLD, SHARED)}
    assets = SuiteAssets(root)
    assert assets.store(SHARED) == digest_of(SHARED) and len(list((root / "assets").iterdir())) == 3
    assert assets.read(digest_of(GOLD)) == GOLD and assets.size(digest_of(NOTES)) == len(NOTES)
    with pytest.raises(UnsupportedExample, match="without a fetcher"):
        assets.asset(NOTES_URL)


def test_setup_files_over_what_one_inject_carries_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(osworld, "MAX_SETUP_BYTES", len(NOTES) - 1)
    examples_root, index_path = write_examples(tmp_path, index={"libreoffice_writer": ["w1"], "os": ["o2"]})
    suite = import_osworld(load_examples(examples_root, index_path), tmp_path / "suite", FakeFetcher(FILES))
    assert [task.id for task in suite.tasks] == ["o2"]
    assert suite.skipped["w1"].startswith(f"setup files total {len(NOTES)} bytes")


@pytest.mark.parametrize("path, relative", [
    ("/home/user/Desktop/a.xlsx", "Desktop/a.xlsx"),
    ("~/Documents/b.docx", "Documents/b.docx"),
    ("/home/user/.config/Code/User/settings.json", ".config/Code/User/settings.json"),
    ("/home/user/Desktop//c.txt", "Desktop/c.txt"),
])
def test_home_paths_become_relative_to_the_guest_users_home(path, relative):
    assert home_relative(path) == relative


@pytest.mark.parametrize("path", ["/tmp/a.txt", "/home/user/", "/home/user/../root/x", "Desktop/a.txt", "~/"])
def test_paths_outside_the_home_are_unsupported(path):
    with pytest.raises(UnsupportedExample):
        home_relative(path)


def test_commands_and_window_steps_translate_like_osworlds_server():
    assert shell_command("ls -la ~ | wc -l", shell=True) == "ls -la ~ | wc -l"
    assert shell_command("ls -la ~ | wc -l") == "ls -la '~' '|' wc -l"
    assert shell_command(["echo", "two words", "$HOME"]) == "echo 'two words' '$HOME'"
    assert shell_command("cat ~/Desktop/'my notes.txt'") == "cat ~/'Desktop/my notes.txt'"
    assert shell_command(["ls", "~/"]) == "ls ~/''"
    with pytest.raises(ValueError, match="No closing quotation"):
        shell_command("echo 'unbalanced")
    assert window_command("activate_window", {"window_name": "Untitled 1 - Writer"}) == (
        "wmctrl -a 'Untitled 1 - Writer'")
    assert window_command("activate_window", {"window_name": "gimp", "by_class": True, "strict": True}) == (
        "wmctrl -xFa gimp")
    assert window_command("close_window", {"window_name": "Calc", "strict": True}) == "wmctrl -Fc Calc"
    planned = PlannedAssets()
    assert setup_step("sleep", {"seconds": 3}, planned) == [{"kind": "sleep", "seconds": 3.0}]
    assert setup_step("command", {"command": ["pkill", "chrome"]}, planned) == [
        {"kind": "execute", "command": "pkill chrome"}]
    assert setup_step("close_window", {"window_name": "Calc"}, planned) == [
        {"kind": "execute", "command": "wmctrl -c Calc"}]
    files = setup_step("download", {"files": [{"url": "https://a.example/x", "path": "~/x"},
                                              {"url": "https://a.example/y", "path": "~/y"}]}, planned)
    assert [step["path"] for step in files] == ["x", "y"] and files[0]["asset"] != files[1]["asset"]
    with pytest.raises(UnsupportedExample, match="setup step chrome_open_tabs"):
        setup_step("chrome_open_tabs", {"urls_to_open": ["https://a.example"]}, planned)


def test_multi_getters_hand_over_the_files_their_gives_pick():
    assert given({"path": "/home/user/a.txt"}) == ["/home/user/a.txt"]
    assert given({"multi": True, "path": ["a", "b", "c"], "gives": [2, 0]}) == ["c", "a"]
    assert given({"multi": True, "path": ["a", "b"]}) == ["a"]


@pytest.mark.parametrize("evaluator, reason", [
    ({"func": []}, "names no metric"),
    ({"func": ["exact_match", "exact_match"], "result": [command_line("ls")], "expected": [rule({}), rule({})],
      "options": [{}, {}]}, "differ in length"),
    ({"func": "exact_match", "result": command_line("ls")}, "needs a result and an expected value"),
    ({"func": "exact_match", "conj": "xor", "result": command_line("ls"), "expected": rule({})}, "'xor'"),
    ({"func": "exact_match", "result": {"type": "vm_window_size"}, "expected": rule({})},
     "getter vm_window_size"),
])
def test_evaluators_the_platform_cannot_score_are_refused(tmp_path, evaluator, reason):
    example = {"id": "x1", "instruction": "Do it.", "config": [], "evaluator": evaluator}
    examples_root, index_path = write_examples(tmp_path, {("os", "x1"): example}, {"os": ["x1"]})
    suite = import_osworld(load_examples(examples_root, index_path), tmp_path / "suite", FakeFetcher({}))
    assert suite.tasks == () and reason in suite.skipped["x1"]


def test_the_report_counts_domains_and_reasons(imported):
    suite, _, _ = imported
    report = import_report(suite)
    assert (report["imported"], report["skipped"]) == (7, 8)
    assert report["imported_by_domain"] == {"libreoffice_writer": 1, "os": 2, "vs_code": 1, "multi_apps": 3}
    assert report["skipped_reasons"] == suite.skipped


def test_the_import_command_writes_the_suite_and_prints_the_report(tmp_path, capsys):
    examples_root, index_path = write_examples(tmp_path)
    output = tmp_path / "osworld_suite"
    assert osworld.main(["--examples", str(examples_root), "--index", str(index_path), "--output", str(output)],
                        fetcher=FakeFetcher(FILES)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["imported"] == 7 and (output / "suite.json").is_file()


def test_the_suite_is_a_template_whose_tasks_inline_their_setup_files(imported):
    suite, _, root = imported
    template = OSWorldSuite(root)
    assert isinstance(template, TaskSuite) and template.size == len(IMPORTED)
    world = generate_mid_market_company(1, {"employees": 2})
    notes = template.generate(world, DifficultySpec(), IMPORTED.index("w1"))
    assert notes.instruction == "Tidy the meeting notes on the desktop." and notes.world == world
    assert notes.setup[0] == {"kind": "file", "path": "Desktop/notes.txt",
                              "base64": base64.b64encode(NOTES).decode("ascii")}
    assert notes.setup[1] == {"kind": "open", "path": "Desktop/notes.txt"}
    assert notes.reads["files"] == ["Desktop/notes.txt"] and len(notes.reads["before"]) == 3
    assert (notes.ground_truth["osworld_id"], notes.ground_truth["domain"]) == ("w1", "libreoffice_writer")
    assert notes.ground_truth["evaluation"]["metrics"][0]["func"] == "compare_text_file"
    infeasible = template.generate(world, DifficultySpec(), IMPORTED.index("o2"))
    assert infeasible.reads is None and infeasible.setup == ()
    with pytest.raises(TaskGenerationError, match="no task 7"):
        template.generate(world, DifficultySpec(), len(IMPORTED))
    with pytest.raises(TaskGenerationError, match="no scripted oracle"):
        template.oracle(notes)
    assert [(verifier.name, verifier.tier) for verifier in template.verifiers()] == [(VERIFIER_NAME, "exact")]
    assert (template.name, template.oracle_name) == (TEMPLATE_NAME, ORACLE_NAME)


def osworld_document(run_type="eval"):
    payload = copy.deepcopy(yaml.safe_load(EXAMPLE_SPEC.read_text()))
    if run_type == "rl":
        payload["task"]["split"] = "train"
        payload["run"].update({"type": "rl", "trainer": {"template": "verl_grpo", "launcher": "kubernetes"},
                               "policy": {**payload["run"]["policy"], "token_ids": True, "logprobs": True}})
    if run_type == "data":
        payload["run"]["type"] = "data"
    return parse_spec_document(payload)


def suite_registry(root):
    registry = default_task_registry()
    registry.register(OSWorldSuite(root))
    return registry


def test_the_example_spec_runs_the_whole_suite_as_eval(imported):
    _, _, root = imported
    registry = suite_registry(root)
    document = osworld_document()
    assert registry.problems(document) == []
    provider = RegistryTaskProvider(registry, document, StaticSnapshots({spec_digest(document.image): SNAPSHOT}))
    candidates = provider.candidates()
    assert len(candidates) == len(IMPORTED)
    first = provider.instance(candidates[0])
    assert first.instruction == "Tidy the meeting notes on the desktop." and first.snapshot == SNAPSHOT
    assert [step["kind"] for step in first.setup_payload["setup"]] == ["file", "open"]
    assert first.setup_payload["reads"]["files"] == ["Desktop/notes.txt"] and first.initial_state == {}
    for run_type in ("rl", "data"):
        assert any("runs only as eval" in problem for problem in registry.problems(osworld_document(run_type)))


def play(root, edit=None, final_action="done"):
    """Task setup and extraction through a guest agent, then the reward pipeline, as an eval episode runs them."""
    registry = suite_registry(root)
    document = osworld_document()
    provider = RegistryTaskProvider(registry, document, StaticSnapshots({spec_digest(document.image): SNAPSHOT}))
    instance = provider.instance(provider.candidates()[0])
    shell = ScriptedShell()
    agent = build_agent(root.parent / f"guest_{edit is not None}_{final_action}", shell=shell,
                        accessibility=WindowsAppear("notes.txt - LibreOffice Writer"))
    agent.handle("inject", instance.setup_payload)
    if edit is not None:
        (agent.home / "Desktop" / "notes.txt").write_bytes(edit)
    final_state = agent.handle("extract", {"apps": list(document.world.materialize)})
    record = evaluate_episode(document.task, registry.verifier_registry, instance.initial_state, final_state,
                              instance.ground_truth, EpisodeOutcome(steps_taken=4, final_action=final_action))
    return record, shell, agent


def test_an_imported_task_plays_through_the_guest_and_the_reward_pipeline(imported):
    _, _, root = imported
    record, shell, agent = play(root, edit=b"Meeting at 10\n  Bring the budget")
    assert (record.total, record.termination) == (1.0, "success")
    assert [check.name for check in record.checks] == ["osworld_compare_text_file"]
    assert record.side_effect_violations == ()
    assert [call[0] for call in shell.calls] == ["open", "probe", "probe"]
    assert shell.calls[0][1] == (agent.home / "Desktop" / "notes.txt").resolve()
    assert shell.calls[2][1] == shlex.join(SAVE_COMMAND)
    untouched, _, _ = play(root)
    assert (untouched.total, untouched.termination) == (0.0, "failure")
    gave_up, _, _ = play(root, edit=GOLD, final_action="give_up")
    assert gave_up.total == 0.0 and gave_up.checks[0].name == "osworld_feasible"


def test_command_reads_reach_the_metrics(imported):
    _, _, root = imported
    template = OSWorldSuite(root)
    spotify = template.generate(generate_mid_market_company(1, {"employees": 2}), DifficultySpec(),
                                IMPORTED.index("o1"))
    shell = ScriptedShell({"which spotify": TerminalOutput(stdout="/snap/bin/spotify\n", stderr="", exit_code=0)})
    agent = build_agent(root.parent / "spotify_guest", shell=shell)
    agent.handle("inject", {"setup": list(spotify.setup), "reads": spotify.reads})
    final_state = agent.handle("extract", {"apps": []})
    document = osworld_document()
    record = evaluate_episode(document.task, suite_registry(root).verifier_registry, {}, final_state,
                              spotify.ground_truth, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert record.total == 1.0
    assert shell.calls[0] == ("execute", "python -c 'print('\"'\"'ready'\"'\"')'", 120)
