// The experiment builder's forms (DESIGN.md 6.14, Configure 1); every field edits the spec YAML beside it.
import { Component, type ChangeEvent, type FormEvent, type ReactElement } from "react";

import { formatSweepValues, parseSweepValues, readPath, type SpecPath } from "../specs/document";
import { schemaOptions } from "../specs/schema";
import { SpecField, type FieldChangeListener, type FieldSpec } from "./SpecField";

export interface FormSection {
  title: string;
  fields: FieldSpec[];
}

function field(label: string, path: SpecPath, kind: FieldSpec["kind"], catalog?: string): FieldSpec {
  const options = kind === "select" || kind === "choices" ? schemaOptions(path) : undefined;
  return { label, path, kind, options, catalog };
}

export const FORM_SECTIONS: FormSection[] = [
  {
    title: "Experiment",
    fields: [
      field("Name", ["experiment", "name"], "text"),
      field("Hypothesis", ["experiment", "hypothesis"], "text"),
      field("Budget (USD)", ["experiment", "budget_usd"], "number"),
      field("Tracker", ["experiment", "tracker"], "select"),
    ],
  },
  {
    title: "Image",
    fields: [
      field("Image", ["image", "id"], "text", "images"),
      field("Operating system", ["image", "os"], "select"),
      field("VMM pool", ["image", "pool", "vmm"], "select"),
      field("Desktop", ["image", "desktop"], "select"),
      field("Display width", ["image", "display", "width"], "number"),
      field("Display height", ["image", "display", "height"], "number"),
    ],
  },
  {
    title: "World",
    fields: [
      field("World generator", ["world", "generator"], "text", "worlds"),
      field("Seed", ["world", "seed"], "number"),
      field("Internet", ["world", "network", "internet"], "select"),
    ],
  },
  {
    title: "Task",
    fields: [
      field("Task generator", ["task", "generator"], "text", "task_templates"),
      field("Allowed actions", ["task", "allowed_actions"], "choices"),
      field("Observations", ["task", "observation"], "choices"),
      field("Max steps", ["task", "max_steps"], "number"),
      field("Apps", ["task", "difficulty", "apps"], "number"),
      field("Oracle steps", ["task", "difficulty", "oracle_steps"], "number"),
      field("Distractors", ["task", "difficulty", "distractors"], "number"),
      field("Corrupted inputs", ["task", "difficulty", "corrupted_inputs"], "number"),
      field("Ambiguity", ["task", "difficulty", "ambiguity"], "select"),
      field("Dependency depth", ["task", "difficulty", "dependency_depth"], "number"),
      field("Split", ["task", "split"], "select"),
    ],
  },
  {
    title: "Run",
    fields: [
      field("Run type", ["run", "type"], "select"),
      field("Policy endpoint", ["run", "policy", "endpoint"], "text", "endpoints"),
      field("Policy model", ["run", "policy", "model"], "text"),
      field("Action format", ["run", "policy", "action_format"], "select"),
      field("Token ids", ["run", "policy", "token_ids"], "boolean"),
      field("Logprobs", ["run", "policy", "logprobs"], "boolean"),
      field("Judge endpoint", ["run", "judge", "endpoint"], "text", "endpoints"),
      field("Judge model", ["run", "judge", "model"], "text"),
      field("Group size", ["run", "group_size"], "number"),
      field("Tasks per batch", ["run", "tasks_per_batch"], "number"),
      field("Batches", ["run", "batches"], "number"),
      field("Max concurrent envs", ["run", "max_concurrent_envs"], "number"),
      field("Priority", ["run", "priority"], "select"),
      field("History", ["run", "history"], "select"),
    ],
  },
];

export const SWEEP_FIELDS: FieldSpec[] = [
  field("Sweep mode", ["experiment", "sweep_mode"], "select"),
  field("Random samples", ["experiment", "random_samples"], "number"),
];

function preventSubmit(event: FormEvent): void {
  event.preventDefault();
}

interface SpecFormProps {
  value: unknown;
  disabled: boolean;
  suggestions: Record<string, readonly string[]>;
  onFieldChange: FieldChangeListener;
}

export function SpecForm(props: SpecFormProps): ReactElement {
  const sections: ReactElement[] = [];
  for (const section of FORM_SECTIONS) {
    const fields: ReactElement[] = [];
    for (const sectionField of section.fields) {
      fields.push(
        <SpecField
          key={sectionField.path.join(".")}
          field={sectionField}
          value={readPath(props.value, sectionField.path)}
          suggestions={sectionField.catalog ? props.suggestions[sectionField.catalog] : undefined}
          onFieldChange={props.onFieldChange}
        />,
      );
    }
    sections.push(
      <fieldset key={section.title} className="form-section" disabled={props.disabled}>
        <legend>{section.title}</legend>
        {fields}
      </fieldset>,
    );
  }
  return <form className="spec-form" onSubmit={preventSubmit}>{sections}</form>;
}

export interface SweepChangeListener {
  (sweepPath: string, values: unknown[] | undefined): void;
}

interface SweepRowProps {
  sweepPath: string;
  values: unknown;
  onSweepChange: SweepChangeListener;
}

class SweepRow extends Component<SweepRowProps> {
  constructor(props: SweepRowProps) {
    super(props);
    this.handleValues = this.handleValues.bind(this);
    this.handleRemove = this.handleRemove.bind(this);
  }

  handleValues(event: ChangeEvent<HTMLInputElement>): void {
    this.props.onSweepChange(this.props.sweepPath, parseSweepValues(event.target.value));
  }

  handleRemove(): void {
    this.props.onSweepChange(this.props.sweepPath, undefined);
  }

  render(): ReactElement {
    return (
      <div className="sweep-row">
        <code>{this.props.sweepPath}</code>
        <input aria-label={`${this.props.sweepPath} values`} defaultValue={formatSweepValues(this.props.values)}
               onBlur={this.handleValues} />
        <button type="button" onClick={this.handleRemove}>Remove</button>
      </div>
    );
  }
}

interface SweepEditorProps {
  value: unknown;
  disabled: boolean;
  onSweepChange: SweepChangeListener;
  onFieldChange: FieldChangeListener;
}

interface SweepEditorState {
  draftPath: string;
  draftValues: string;
}

// Grid or random sweeps over any spec field (DESIGN.md 6.14, Configure 3); each point becomes a run.
export class SweepEditor extends Component<SweepEditorProps, SweepEditorState> {
  constructor(props: SweepEditorProps) {
    super(props);
    this.state = { draftPath: "", draftValues: "" };
    this.handleDraftPath = this.handleDraftPath.bind(this);
    this.handleDraftValues = this.handleDraftValues.bind(this);
    this.handleAdd = this.handleAdd.bind(this);
  }

  handleDraftPath(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ draftPath: event.target.value });
  }

  handleDraftValues(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ draftValues: event.target.value });
  }

  handleAdd(): void {
    if (this.state.draftPath.trim() === "") {
      return;
    }
    this.props.onSweepChange(this.state.draftPath.trim(), parseSweepValues(this.state.draftValues));
    this.setState({ draftPath: "", draftValues: "" });
  }

  render(): ReactElement {
    const sweep = readPath(this.props.value, ["experiment", "sweep"]);
    const rows: ReactElement[] = [];
    if (sweep !== null && typeof sweep === "object") {
      for (const [sweepPath, values] of Object.entries(sweep as Record<string, unknown>)) {
        rows.push(<SweepRow key={`${sweepPath}=${formatSweepValues(values)}`} sweepPath={sweepPath} values={values}
                            onSweepChange={this.props.onSweepChange} />);
      }
    }
    const modeFields: ReactElement[] = [];
    for (const sweepField of SWEEP_FIELDS) {
      modeFields.push(<SpecField key={sweepField.path.join(".")} field={sweepField}
                                 value={readPath(this.props.value, sweepField.path)}
                                 onFieldChange={this.props.onFieldChange} />);
    }
    return (
      <fieldset className="form-section sweep-editor" disabled={this.props.disabled}>
        <legend>Sweep</legend>
        {modeFields}
        {rows}
        <div className="sweep-row">
          <input aria-label="sweep path" placeholder="task.difficulty.distractors" value={this.state.draftPath}
                 onChange={this.handleDraftPath} />
          <input aria-label="sweep values" placeholder="0, 5, 10" value={this.state.draftValues}
                 onChange={this.handleDraftValues} />
          <button type="button" onClick={this.handleAdd}>Add sweep</button>
        </div>
      </fieldset>
    );
  }
}
