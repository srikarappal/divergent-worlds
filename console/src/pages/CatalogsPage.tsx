// Catalogs: images, worlds, task templates, verifiers, rubrics and endpoints with version, gate status,
// clone fidelity, pass rates per policy and usage (DESIGN.md 6.14, Configure 2 and 5).
import { Component, type ChangeEvent, type MouseEvent, type ReactElement } from "react";

import type { ConsoleApi } from "../api/client";
import type { CatalogEntry, CatalogKind, EndpointView, ProbeResult } from "../api/types";
import { failureProblems, formatPercent, ParityPanel, ProblemList } from "../components/common";

export const CATALOG_TABS: ReadonlyArray<{ kind: CatalogKind; label: string }> = [
  { kind: "images", label: "Images" },
  { kind: "worlds", label: "Worlds" },
  { kind: "task_templates", label: "Task templates" },
  { kind: "verifiers", label: "Verifiers" },
  { kind: "rubrics", label: "Rubrics" },
  { kind: "endpoints", label: "Endpoints" },
];

function passRatesText(passRates: Record<string, number>): string {
  const parts: string[] = [];
  for (const [policy, rate] of Object.entries(passRates)) {
    parts.push(`${policy} ${formatPercent(rate)}`);
  }
  return parts.join(", ");
}

function catalogRow(entry: CatalogEntry): ReactElement {
  return (
    <tr key={entry.name}>
      <td><code>{entry.name}</code></td>
      <td>{entry.version}</td>
      <td>{entry.gate_status ?? ""}</td>
      <td>{formatPercent(entry.clone_fidelity)}</td>
      <td>{passRatesText(entry.pass_rates)}</td>
      <td>{entry.usage_runs}</td>
      <td>{entry.description}</td>
    </tr>
  );
}

interface CatalogsState {
  kind: CatalogKind;
  entries: CatalogEntry[];
  loading: boolean;
  error: string | null;
}

export class CatalogsPage extends Component<{ api: ConsoleApi }, CatalogsState> {
  constructor(props: { api: ConsoleApi }) {
    super(props);
    this.state = { kind: "images", entries: [], loading: true, error: null };
    this.handleTab = this.handleTab.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.load("images");
  }

  async load(kind: CatalogKind): Promise<void> {
    this.setState({ kind, loading: true, error: null });
    try {
      this.setState({ entries: await this.props.api.listCatalog(kind), loading: false });
    } catch (error) {
      this.setState({ entries: [], loading: false, error: failureProblems(error).join("; ") });
    }
  }

  async handleTab(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.load(event.currentTarget.dataset.kind as CatalogKind);
  }

  render(): ReactElement {
    const tabs: ReactElement[] = [];
    for (const tab of CATALOG_TABS) {
      tabs.push(<button key={tab.kind} type="button" data-kind={tab.kind} aria-pressed={tab.kind === this.state.kind}
                        onClick={this.handleTab}>{tab.label}</button>);
    }
    const rows: ReactElement[] = [];
    for (const entry of this.state.entries) {
      rows.push(catalogRow(entry));
    }
    return (
      <div className="catalogs">
        <nav className="tabs" aria-label="catalogs">{tabs}</nav>
        {this.state.error ? <ProblemList title="Catalog" problems={[this.state.error]} /> : null}
        <table aria-label={`${this.state.kind} catalog`}>
          <thead>
            <tr><th>Name</th><th>Version</th><th>Gates</th><th>Clone fidelity</th><th>Pass rates</th><th>Runs</th>
              <th>Description</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        {!this.state.loading && rows.length === 0 ? <p className="empty">Nothing in this catalog yet.</p> : null}
        {this.state.kind === "endpoints" ? <EndpointsPanel api={this.props.api} /> : null}
        <ParityPanel items={[{ name: "list_catalog", values: { kind: this.state.kind } }]} />
      </div>
    );
  }
}

interface EndpointDraft {
  name: string;
  base_url: string;
  allowed_models: string;
  provider_kind: "frontier" | "self_hosted" | "user";
  api_key_secret: string;
  returns_token_ids: boolean;
  accepts_screenshots: boolean;
}

interface EndpointsState {
  endpoints: EndpointView[];
  draft: EndpointDraft;
  keyDrafts: Record<string, string>;
  probes: Record<string, ProbeResult>;
  problems: string[];
  message: string | null;
}

const EMPTY_DRAFT: EndpointDraft = {
  name: "", base_url: "", allowed_models: "", provider_kind: "user", api_key_secret: "", returns_token_ids: false,
  accepts_screenshots: true,
};

function modelList(text: string): string[] {
  const models: string[] = [];
  for (const model of text.split(",")) {
    if (model.trim() !== "") {
      models.push(model.trim());
    }
  }
  return models;
}

function probeText(probe: ProbeResult | undefined): string {
  if (probe === undefined) {
    return "";
  }
  if (!probe.reachable) {
    return `unreachable: ${probe.error ?? "no answer"}`;
  }
  const latency = probe.latency_seconds === null ? "" : ` in ${Math.round(probe.latency_seconds * 1000)} ms`;
  return `reachable${latency}, token ids ${probe.returns_token_ids ? "yes" : "no"}, logprobs ${
    probe.returns_logprobs ? "yes" : "no"}`;
}

// Register BYO endpoints, test them and store keys in the vault; keys are write only.
export class EndpointsPanel extends Component<{ api: ConsoleApi }, EndpointsState> {
  constructor(props: { api: ConsoleApi }) {
    super(props);
    this.state = { endpoints: [], draft: EMPTY_DRAFT, keyDrafts: {}, probes: {}, problems: [], message: null };
    this.handleDraft = this.handleDraft.bind(this);
    this.handleRegister = this.handleRegister.bind(this);
    this.handleTest = this.handleTest.bind(this);
    this.handleKeyDraft = this.handleKeyDraft.bind(this);
    this.handleSetKey = this.handleSetKey.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.refresh();
  }

  async refresh(): Promise<void> {
    try {
      this.setState({ endpoints: await this.props.api.listEndpoints() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleDraft(event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void {
    const target = event.target;
    const value = target instanceof HTMLInputElement && target.type === "checkbox" ? target.checked : target.value;
    this.setState({ draft: { ...this.state.draft, [target.name]: value } });
  }

  async handleRegister(): Promise<void> {
    const draft = this.state.draft;
    try {
      await this.props.api.registerEndpoint({
        name: draft.name, base_url: draft.base_url, allowed_models: modelList(draft.allowed_models),
        provider_kind: draft.provider_kind, api_key_secret: draft.api_key_secret || null,
        returns_token_ids: draft.returns_token_ids, accepts_screenshots: draft.accepts_screenshots,
      });
      this.setState({ draft: EMPTY_DRAFT, problems: [], message: `registered ${draft.name}` });
      await this.refresh();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleTest(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    const name = event.currentTarget.dataset.name ?? "";
    const probe = await this.props.api.testEndpoint(name);
    this.setState({ probes: { ...this.state.probes, [name]: probe } });
  }

  handleKeyDraft(event: ChangeEvent<HTMLInputElement>): void {
    const name = event.target.dataset.name ?? "";
    this.setState({ keyDrafts: { ...this.state.keyDrafts, [name]: event.target.value } });
  }

  async handleSetKey(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    const name = event.currentTarget.dataset.name ?? "";
    try {
      await this.props.api.setEndpointKey(name, this.state.keyDrafts[name] ?? "");
      this.setState({ keyDrafts: { ...this.state.keyDrafts, [name]: "" }, message: `key stored for ${name}` });
      await this.refresh();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  renderEndpointRow(endpoint: EndpointView): ReactElement {
    return (
      <tr key={endpoint.name}>
        <td><code>{endpoint.name}</code></td>
        <td>{endpoint.base_url}</td>
        <td>{endpoint.allowed_models.join(", ")}</td>
        <td>{endpoint.provider_kind}</td>
        <td>{endpoint.has_key ? "key set" : "no key"}</td>
        <td>
          <button type="button" data-name={endpoint.name} onClick={this.handleTest}>Test</button>
          <span className="probe">{probeText(this.state.probes[endpoint.name])}</span>
        </td>
        <td>
          <input type="password" autoComplete="off" aria-label={`${endpoint.name} key`} data-name={endpoint.name}
                 value={this.state.keyDrafts[endpoint.name] ?? ""} onChange={this.handleKeyDraft} />
          <button type="button" data-name={endpoint.name} onClick={this.handleSetKey}>Set key</button>
        </td>
      </tr>
    );
  }

  render(): ReactElement {
    const draft = this.state.draft;
    const rows: ReactElement[] = [];
    for (const endpoint of this.state.endpoints) {
      rows.push(this.renderEndpointRow(endpoint));
    }
    return (
      <section className="endpoints">
        <h2>Endpoint registry</h2>
        {this.state.message ? <p role="status">{this.state.message}</p> : null}
        <ProblemList title="Endpoint" problems={this.state.problems} />
        <table aria-label="registered endpoints">
          <thead>
            <tr><th>Name</th><th>Base URL</th><th>Pinned models</th><th>Provider</th><th>Key</th><th>Probe</th>
              <th>Vault</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <form className="register-endpoint" onSubmit={preventDefault}>
          <label>Name <input name="name" value={draft.name} onChange={this.handleDraft} /></label>
          <label>Base URL <input name="base_url" value={draft.base_url} onChange={this.handleDraft} /></label>
          <label>Pinned models <input name="allowed_models" value={draft.allowed_models}
                                      onChange={this.handleDraft} /></label>
          <label>Provider
            <select name="provider_kind" value={draft.provider_kind} onChange={this.handleDraft}>
              <option value="user">user</option>
              <option value="self_hosted">self_hosted</option>
              <option value="frontier">frontier</option>
            </select>
          </label>
          <label>Key secret name <input name="api_key_secret" value={draft.api_key_secret}
                                        onChange={this.handleDraft} /></label>
          <label><input type="checkbox" name="returns_token_ids" checked={draft.returns_token_ids}
                        onChange={this.handleDraft} /> Returns token ids</label>
          <label><input type="checkbox" name="accepts_screenshots" checked={draft.accepts_screenshots}
                        onChange={this.handleDraft} /> May receive screenshots</label>
          <button type="button" className="primary" onClick={this.handleRegister}>Register endpoint</button>
        </form>
        <ParityPanel items={[
          { name: "register_endpoint" },
          { name: "test_endpoint", values: { name: draft.name || undefined } },
          { name: "set_endpoint_key", values: { name: draft.name || undefined } },
        ]} />
      </section>
    );
  }
}

function preventDefault(event: { preventDefault(): void }): void {
  event.preventDefault();
}
