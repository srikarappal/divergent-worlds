// Configure: the experiment builder, YAML view, presets, cloning, import, export and diff (DESIGN.md 6.14).
// Saving creates an immutable digest; nothing configured here lives outside the spec.
import { Component, type ChangeEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { ConsoleApi } from "../api/client";
import type { CatalogEntry } from "../api/types";
import { DiffView, failureProblems, ParityPanel, ProblemList, YamlPane } from "../components/common";
import { SpecForm, SweepEditor } from "../components/SpecForm";
import { diffLines } from "../specs/diff";
import { parseSpec, readPath, writePath, type SpecPath } from "../specs/document";
import { PRESETS } from "../specs/presets";
import { validateSpec, type SpecProblem } from "../specs/schema";

const SUGGESTION_CATALOGS = ["images", "worlds", "task_templates", "endpoints"];

interface SpecEditorProps {
  api: ConsoleApi;
}

interface SpecEditorState {
  yamlText: string;
  baseYaml: string;
  baseLabel: string;
  showDiff: boolean;
  savedYaml: string | null;
  savedDigest: string | null;
  saving: boolean;
  serverProblems: string[];
  message: string | null;
  cloneRunId: string;
  suggestions: Record<string, string[]>;
}

function entryNames(entries: readonly CatalogEntry[]): string[] {
  const names: string[] = [];
  for (const entry of entries) {
    names.push(entry.name);
  }
  return names;
}

function problemText(problem: SpecProblem): string {
  return `${problem.path}: ${problem.message}`;
}

function presetYaml(name: string): string | undefined {
  for (const preset of PRESETS) {
    if (preset.name === name) {
      return preset.yaml;
    }
  }
  return undefined;
}

export class SpecEditorPage extends Component<SpecEditorProps, SpecEditorState> {
  constructor(props: SpecEditorProps) {
    super(props);
    const firstPreset = PRESETS[0];
    this.state = {
      yamlText: firstPreset?.yaml ?? "",
      baseYaml: firstPreset?.yaml ?? "",
      baseLabel: firstPreset ? `preset ${firstPreset.name}` : "an empty spec",
      showDiff: false,
      savedYaml: null,
      savedDigest: null,
      saving: false,
      serverProblems: [],
      message: null,
      cloneRunId: "",
      suggestions: {},
    };
    this.handleYamlChange = this.handleYamlChange.bind(this);
    this.handleFieldChange = this.handleFieldChange.bind(this);
    this.handleSweepChange = this.handleSweepChange.bind(this);
    this.handlePresetSelect = this.handlePresetSelect.bind(this);
    this.handleCloneRunIdChange = this.handleCloneRunIdChange.bind(this);
    this.handleClone = this.handleClone.bind(this);
    this.handleImport = this.handleImport.bind(this);
    this.handleToggleDiff = this.handleToggleDiff.bind(this);
    this.handleSave = this.handleSave.bind(this);
  }

  async componentDidMount(): Promise<void> {
    const suggestions: Record<string, string[]> = {};
    for (const kind of SUGGESTION_CATALOGS) {
      try {
        suggestions[kind] = entryNames(await this.props.api.listCatalog(kind));
      } catch {
        suggestions[kind] = [];
      }
    }
    this.setState({ suggestions });
  }

  loadSpec(yamlText: string, baseLabel: string): void {
    this.setState({ yamlText, baseYaml: yamlText, baseLabel, serverProblems: [], message: null });
  }

  handleYamlChange(event: ChangeEvent<HTMLTextAreaElement>): void {
    this.setState({ yamlText: event.target.value, serverProblems: [] });
  }

  handleFieldChange(path: SpecPath, value: unknown): void {
    if (parseSpec(this.state.yamlText).errors.length === 0) {
      this.setState({ yamlText: writePath(this.state.yamlText, path, value), serverProblems: [] });
    }
  }

  handleSweepChange(sweepPath: string, values: unknown[] | undefined): void {
    this.handleFieldChange(["experiment", "sweep", sweepPath], values);
  }

  handlePresetSelect(event: ChangeEvent<HTMLSelectElement>): void {
    const yaml = presetYaml(event.target.value);
    if (yaml !== undefined) {
      this.loadSpec(yaml, `preset ${event.target.value}`);
    }
  }

  handleCloneRunIdChange(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ cloneRunId: event.target.value });
  }

  async handleClone(): Promise<void> {
    const runId = this.state.cloneRunId.trim();
    if (runId === "") {
      return;
    }
    try {
      this.loadSpec(await this.props.api.getRunSpec(runId), `run ${runId}`);
    } catch (error) {
      this.setState({ message: `could not clone ${runId}: ${failureProblems(error).join("; ")}` });
    }
  }

  async handleImport(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.target.files?.[0];
    if (file !== undefined) {
      this.loadSpec(await file.text(), `file ${file.name}`);
    }
  }

  handleToggleDiff(): void {
    this.setState({ showDiff: !this.state.showDiff });
  }

  async handleSave(): Promise<void> {
    const yamlText = this.state.yamlText;
    this.setState({ saving: true, serverProblems: [], message: null });
    try {
      const digest = await this.props.api.saveSpec(yamlText);
      this.setState({ saving: false, savedYaml: yamlText, savedDigest: digest, message: `saved as ${digest}` });
    } catch (error) {
      this.setState({ saving: false, serverProblems: failureProblems(error) });
    }
  }

  renderToolbar(blocked: boolean): ReactElement {
    const presetOptions: ReactElement[] = [<option key="" value="">Start from a preset</option>];
    for (const preset of PRESETS) {
      presetOptions.push(<option key={preset.name} value={preset.name}>{preset.name}</option>);
    }
    const exportHref = `data:application/yaml;charset=utf-8,${encodeURIComponent(this.state.yamlText)}`;
    return (
      <div className="toolbar">
        <select aria-label="preset" value="" onChange={this.handlePresetSelect}>{presetOptions}</select>
        <input aria-label="run to clone" placeholder="run id to clone" value={this.state.cloneRunId}
               onChange={this.handleCloneRunIdChange} />
        <button type="button" onClick={this.handleClone}>Clone run</button>
        <label className="file-button">
          Import
          <input type="file" accept=".yaml,.yml" onChange={this.handleImport} />
        </label>
        <a className="button" download="spec.yaml" href={exportHref}>Export</a>
        <button type="button" onClick={this.handleToggleDiff}>{this.state.showDiff ? "Hide diff" : "Diff"}</button>
        <button type="button" className="primary" disabled={blocked || this.state.saving} onClick={this.handleSave}>
          Save spec
        </button>
      </div>
    );
  }

  render(): ReactElement {
    const parsed = parseSpec(this.state.yamlText);
    const schemaProblems = parsed.errors.length === 0 ? validateSpec(parsed.value) : [];
    const digest = this.state.savedYaml === this.state.yamlText ? this.state.savedDigest : null;
    const budget = readPath(parsed.value, ["experiment", "budget_usd"]);
    return (
      <div className="spec-editor">
        {this.renderToolbar(parsed.errors.length > 0 || schemaProblems.length > 0)}
        {this.state.message ? <p className="status" role="status">{this.state.message}</p> : null}
        {digest !== null ? <Link className="button primary" to={`/launch/${digest}`}>Preflight and launch</Link> : null}
        <div className="columns">
          <section className="forms">
            <SpecForm value={parsed.value} disabled={parsed.errors.length > 0} suggestions={this.state.suggestions}
                      onFieldChange={this.handleFieldChange} />
            <SweepEditor value={parsed.value} disabled={parsed.errors.length > 0}
                         onSweepChange={this.handleSweepChange} onFieldChange={this.handleFieldChange} />
          </section>
          <section className="yaml">
            <YamlPane text={this.state.yamlText} onYamlChange={this.handleYamlChange} />
            <ProblemList title="YAML" problems={parsed.errors} />
            <ProblemList title="Schema" problems={schemaProblems.map(problemText)} />
            <ProblemList title="Server" problems={this.state.serverProblems} />
            {this.state.showDiff
              ? <DiffView label={this.state.baseLabel} lines={diffLines(this.state.baseYaml, this.state.yamlText)} />
              : null}
          </section>
        </div>
        <ParityPanel items={[
          { name: "save_spec", values: { spec_file: "spec.yaml" } },
          { name: "preflight", values: { digest } },
          { name: "launch", values: { digest, budget_usd: typeof budget === "number" ? budget : undefined } },
        ]} />
      </div>
    );
  }
}
