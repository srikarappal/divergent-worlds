import { expect, test } from "vitest";
import { parse } from "yaml";

import { changedLineCount, diffLines } from "./diff";
import { formatSweepValues, parseSpec, parseSweepValues, readPath, writePath } from "./document";
import { PRESETS } from "./presets";
import { schemaOptions, validateSpec, type SpecProblem } from "./schema";

function exampleSpec(): string {
  return PRESETS[0].yaml;
}

function problemPath(problem: SpecProblem): string {
  return problem.path;
}

function thePresetIsTheRepositoryExample(): void {
  expect(PRESETS[0].name).toBe("recon_difficulty_sweep");
  expect(validateSpec(parse(exampleSpec()))).toEqual([]);
}

test("the preset is the repository example and passes the schema", thePresetIsTheRepositoryExample);

function schemaProblemsNameTheirField(): void {
  const document = parse(exampleSpec());
  document.run.group_size = 0;
  document.run.surprise = true;
  document.task.split = "dev";
  delete document.image.display;
  const problems = validateSpec(document);
  expect(problems.map(problemPath)).toEqual(
    expect.arrayContaining(["run.group_size", "run.surprise", "task.split", "image.display"]));
  expect(problems).toContainEqual({ path: "run.surprise", message: "is not a known field" });
  expect(problems).toContainEqual({ path: "image.display", message: "is required" });
}

test("schema problems name their field", schemaProblemsNameTheirField);

function enumOptionsComeFromTheSchema(): void {
  expect(schemaOptions(["run", "type"])).toEqual(["rl", "eval", "data"]);
  expect(schemaOptions(["run", "policy", "action_format"])).toEqual(["computer_tool", "typed_actions", "text_actions"]);
  expect(schemaOptions(["task", "allowed_actions"])).toEqual(["computer", "browser", "terminal", "native", "control"]);
  expect(schemaOptions(["experiment", "tracker"])).toEqual(["wandb", "mlflow"]);
  expect(schemaOptions(["image", "desktop"])).toEqual(["openbox", "xfce"]);
}

test("enum options come from the shared schema", enumOptionsComeFromTheSchema);

function editsKeepCommentsAndFlowStyle(): void {
  const edited = writePath(exampleSpec(), ["run", "group_size"], 16);
  expect(edited).toContain("group_size: 16");
  expect(edited).toContain("# who plays, who grades, how much");
  const pooled = writePath(edited, ["image", "pool", "cpu_model"], "fleet_v2");
  expect(pooled).toContain("pool: { vmm: qemu_templated, machine: pc-q35-9.0, cpu_model: fleet_v2 }");
  expect(readPath(parseSpec(writePath(pooled, ["run", "history"], "sliding_window")).value, ["run", "history"]))
    .toBe("sliding_window");
  expect(writePath(edited, ["experiment", "tracker"], undefined)).not.toContain("tracker:");
}

test("edits keep comments and flow style", editsKeepCommentsAndFlowStyle);

function brokenYamlIsReportedNotThrown(): void {
  const parsed = parseSpec("experiment: [\n");
  expect(parsed.value).toBeNull();
  expect(parsed.errors.length).toBeGreaterThan(0);
}

test("broken YAML is reported, not thrown", brokenYamlIsReportedNotThrown);

function sweepValuesRoundTrip(): void {
  expect(parseSweepValues("0, 5, 10")).toEqual([0, 5, 10]);
  expect(parseSweepValues("low, high")).toEqual(["low", "high"]);
  expect(formatSweepValues([0.0, "low", true])).toBe("0, low, true");
}

test("sweep values round trip", sweepValuesRoundTrip);

function diffsShowAddedAndRemovedLines(): void {
  const lines = diffLines("a\nb\nc", "a\nc\nd");
  expect(lines).toEqual([
    { kind: "same", text: "a" }, { kind: "removed", text: "b" }, { kind: "same", text: "c" },
    { kind: "added", text: "d" },
  ]);
  expect(changedLineCount(lines)).toBe(2);
  expect(changedLineCount(diffLines(exampleSpec(), exampleSpec()))).toBe(0);
}

test("diffs show added and removed lines", diffsShowAddedAndRemovedLines);
