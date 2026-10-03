// Admin (DESIGN.md 6.14): roles, tenants and quotas, budgets, license and identity pools, per tenant data policy,
// retention and the audit log of every launch, edit, stop and takeover.
import { Component, type ChangeEvent, type ReactElement } from "react";

import type { ConsoleApi } from "../api/client";
import type { AdmissionStatus, AuditRecord, DataPolicy, ExperimentRecord, RetentionReport } from "../api/types";
import { failureProblems, ParityPanel, ProblemList } from "../components/common";
import { formatUsd } from "./LaunchPage";

export const ROLES = ["viewer", "researcher", "operator", "labeler", "admin"];
const EXPORT_KINDS: ReadonlyArray<"sft" | "preferences"> = ["sft", "preferences"];

interface SectionProps {
  api: ConsoleApi;
}

function countRows(counts: Record<string, number>, label: string): ReactElement[] {
  const rows: ReactElement[] = [];
  for (const [name, count] of Object.entries(counts)) {
    rows.push(<tr key={`${label}:${name}`}><td>{label}</td><td>{name}</td><td>{count}</td></tr>);
  }
  return rows;
}

interface RolesState {
  bindings: Record<string, string[]>;
  subject: string;
  roles: string[];
  problems: string[];
}

export class RolesSection extends Component<SectionProps, RolesState> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { bindings: {}, subject: "", roles: [], problems: [] };
    this.handleSubject = this.handleSubject.bind(this);
    this.handleRole = this.handleRole.bind(this);
    this.handleSave = this.handleSave.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.refresh();
  }

  async refresh(): Promise<void> {
    try {
      this.setState({ bindings: await this.props.api.listRoles(), problems: [] });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleSubject(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ subject: event.target.value, roles: this.state.bindings[event.target.value] ?? [] });
  }

  handleRole(event: ChangeEvent<HTMLInputElement>): void {
    const others: string[] = [];
    for (const role of this.state.roles) {
      if (role !== event.target.value) {
        others.push(role);
      }
    }
    this.setState({ roles: event.target.checked ? [...others, event.target.value] : others });
  }

  async handleSave(): Promise<void> {
    try {
      await this.props.api.setRoles(this.state.subject, this.state.roles);
      await this.refresh();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const rows: ReactElement[] = [];
    for (const [subject, roles] of Object.entries(this.state.bindings)) {
      rows.push(<tr key={subject}><td>{subject}</td><td>{roles.join(", ")}</td></tr>);
    }
    const boxes: ReactElement[] = [];
    for (const role of ROLES) {
      boxes.push(<label key={role}><input type="checkbox" value={role} checked={this.state.roles.includes(role)}
                                          onChange={this.handleRole} /> {role}</label>);
    }
    return (
      <section aria-label="roles">
        <h2>Users and roles</h2>
        <ProblemList title="Roles" problems={this.state.problems} />
        <table><thead><tr><th>User</th><th>Granted roles</th></tr></thead><tbody>{rows}</tbody></table>
        <div className="toolbar">
          <label>User <input value={this.state.subject} onChange={this.handleSubject} /></label>
          {boxes}
          <button type="button" className="primary" disabled={!this.state.subject} onClick={this.handleSave}>
            Save roles</button>
        </div>
      </section>
    );
  }
}

interface QuotasState {
  status: AdmissionStatus | null;
  tenant: string;
  quotaText: string;
  problems: string[];
}

export class QuotasSection extends Component<SectionProps, QuotasState> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { status: null, tenant: "", quotaText: "", problems: [] };
    this.handleTenant = this.handleTenant.bind(this);
    this.handleQuota = this.handleQuota.bind(this);
    this.handleSave = this.handleSave.bind(this);
  }

  async componentDidMount(): Promise<void> {
    try {
      this.setState({ status: await this.props.api.fleetAdmission() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleTenant(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ tenant: event.target.value });
  }

  handleQuota(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ quotaText: event.target.value });
  }

  async handleSave(): Promise<void> {
    try {
      this.setState({ status: await this.props.api.setTenantQuota(this.state.tenant, Number(this.state.quotaText)),
                      problems: [] });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const status = this.state.status;
    const tenants: ReactElement[] = [];
    if (status !== null) {
      const names = new Set([...Object.keys(status.tenant_quotas), ...Object.keys(status.usage.by_tenant)]);
      for (const tenant of [...names].sort()) {
        tenants.push(<tr key={tenant}><td>{tenant}</td>
          <td>{status.tenant_quotas[tenant] ?? status.default_tenant_quota}</td>
          <td>{status.usage.by_tenant[tenant] ?? 0}</td></tr>);
      }
    }
    return (
      <section aria-label="tenants and quotas">
        <h2>Tenants and quotas</h2>
        <ProblemList title="Admission" problems={this.state.problems} />
        {status !== null
          ? (
            <>
              <p>{status.usage.total} of {status.capacity} envs in use; zero quota means unlimited.</p>
              <table>
                <thead><tr><th>Tenant</th><th>Quota</th><th>In use</th></tr></thead>
                <tbody>{tenants}</tbody>
              </table>
              <table aria-label="classes and endpoints">
                <tbody>
                  {countRows(status.reserved, "reserved")}
                  {countRows(status.usage.by_class, "in use")}
                  {countRows(status.endpoint_max_envs, "endpoint budget")}
                </tbody>
              </table>
            </>
          )
          : null}
        <div className="toolbar">
          <label>Tenant <input value={this.state.tenant} onChange={this.handleTenant} /></label>
          <label>Quota <input type="number" value={this.state.quotaText} onChange={this.handleQuota} /></label>
          <button type="button" className="primary" disabled={!this.state.tenant} onClick={this.handleSave}>
            Set quota</button>
        </div>
      </section>
    );
  }
}

interface ListState<Row> {
  rows: Row[];
  problems: string[];
}

export class BudgetsSection extends Component<SectionProps, ListState<ExperimentRecord>> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { rows: [], problems: [] };
  }

  async componentDidMount(): Promise<void> {
    try {
      this.setState({ rows: await this.props.api.listExperiments() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const rows: ReactElement[] = [];
    for (const experiment of this.state.rows) {
      rows.push(<tr key={experiment.experiment_id}><td>{experiment.experiment_id}</td><td>{experiment.tenant}</td>
        <td>{formatUsd(experiment.spent_usd)}</td><td>{formatUsd(experiment.budget_usd)}</td>
        <td>{experiment.approved ? "approved" : "waiting"}</td></tr>);
    }
    return (
      <section aria-label="budgets">
        <h2>Budgets</h2>
        <ProblemList title="Budgets" problems={this.state.problems} />
        <table><thead><tr><th>Experiment</th><th>Tenant</th><th>Spent</th><th>Budget</th><th>Approval</th></tr></thead>
          <tbody>{rows}</tbody></table>
      </section>
    );
  }
}

interface PoolsState {
  pools: Record<string, number>;
  report: RetentionReport | null;
  problems: string[];
}

export class PoolsSection extends Component<SectionProps, PoolsState> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { pools: {}, report: null, problems: [] };
    this.handleSweep = this.handleSweep.bind(this);
  }

  async componentDidMount(): Promise<void> {
    try {
      this.setState({ pools: await this.props.api.poolStatus() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleSweep(): Promise<void> {
    try {
      this.setState({ report: await this.props.api.sweepRetention() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const rows: ReactElement[] = [];
    for (const [pool, free] of Object.entries(this.state.pools)) {
      rows.push(<tr key={pool}><td>{pool}</td><td>{free}</td></tr>);
    }
    const report = this.state.report;
    return (
      <section aria-label="license and identity pools">
        <h2>License and identity pools</h2>
        <ProblemList title="Pools" problems={this.state.problems} />
        <table><thead><tr><th>Pool</th><th>Free</th></tr></thead><tbody>{rows}</tbody></table>
        <button type="button" onClick={this.handleSweep}>Apply retention now</button>
        {report !== null
          ? <p role="status">Deleted {report.deleted_episodes.length} episodes and {report.deleted_blobs} blobs;
            kept{" "}
            {report.thumbnail_only_frames} frames as thumbnails only.</p>
          : null}
      </section>
    );
  }
}

interface PoliciesState {
  policies: DataPolicy[];
  tenant: string;
  endpointsText: string;
  trajectoryDays: string;
  fullFrameDays: string;
  exports: Array<"sft" | "preferences">;
  problems: string[];
}

export class DataPoliciesSection extends Component<SectionProps, PoliciesState> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { policies: [], tenant: "", endpointsText: "", trajectoryDays: "365", fullFrameDays: "30",
                   exports: ["sft", "preferences"], problems: [] };
    this.handleField = this.handleField.bind(this);
    this.handleExport = this.handleExport.bind(this);
    this.handleSave = this.handleSave.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.refresh();
  }

  async refresh(): Promise<void> {
    try {
      this.setState({ policies: await this.props.api.listDataPolicies() });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleField(event: ChangeEvent<HTMLInputElement>): void {
    const name = event.target.name as "tenant" | "endpointsText" | "trajectoryDays" | "fullFrameDays";
    this.setState({ [name]: event.target.value } as Pick<PoliciesState, typeof name>);
  }

  handleExport(event: ChangeEvent<HTMLInputElement>): void {
    const kind = event.target.value as "sft" | "preferences";
    const others: Array<"sft" | "preferences"> = [];
    for (const allowed of this.state.exports) {
      if (allowed !== kind) {
        others.push(allowed);
      }
    }
    this.setState({ exports: event.target.checked ? [...others, kind] : others });
  }

  async handleSave(): Promise<void> {
    const endpoints: string[] = [];
    for (const name of this.state.endpointsText.split(",")) {
      if (name.trim() !== "") {
        endpoints.push(name.trim());
      }
    }
    try {
      await this.props.api.setDataPolicy(this.state.tenant, {
        screenshot_endpoints: endpoints.length > 0 ? endpoints : null,
        trajectory_days: Number(this.state.trajectoryDays), full_frame_days: Number(this.state.fullFrameDays),
        exports: this.state.exports,
      });
      this.setState({ problems: [] });
      await this.refresh();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const rows: ReactElement[] = [];
    for (const policy of this.state.policies) {
      rows.push(<tr key={policy.tenant}><td>{policy.tenant}</td>
        <td>{policy.screenshot_endpoints === null ? "any endpoint" : policy.screenshot_endpoints.join(", ")}</td>
        <td>{policy.trajectory_days} days</td><td>{policy.full_frame_days} days</td>
        <td>{policy.exports.join(", ") || "none"}</td></tr>);
    }
    const exports: ReactElement[] = [];
    for (const kind of EXPORT_KINDS) {
      exports.push(<label key={kind}><input type="checkbox" value={kind} checked={this.state.exports.includes(kind)}
                                            onChange={this.handleExport} /> {kind} exports</label>);
    }
    return (
      <section aria-label="data policies">
        <h2>Data policies</h2>
        <ProblemList title="Data policy" problems={this.state.problems} />
        <table><thead><tr><th>Tenant</th><th>Screenshots may go to</th><th>Trajectories kept</th>
          <th>Full frames kept</th><th>Exports</th></tr></thead><tbody>{rows}</tbody></table>
        <div className="toolbar">
          <label>Tenant <input name="tenant" value={this.state.tenant} onChange={this.handleField} /></label>
          <label>Screenshot endpoints <input name="endpointsText" placeholder="any" value={this.state.endpointsText}
                                             onChange={this.handleField} /></label>
          <label>Trajectory days <input name="trajectoryDays" type="number" value={this.state.trajectoryDays}
                                        onChange={this.handleField} /></label>
          <label>Full frame days <input name="fullFrameDays" type="number" value={this.state.fullFrameDays}
                                        onChange={this.handleField} /></label>
          {exports}
          <button type="button" className="primary" disabled={!this.state.tenant} onClick={this.handleSave}>
            Save policy</button>
        </div>
      </section>
    );
  }
}

interface AuditState {
  records: AuditRecord[];
  actor: string;
  action: string;
  problems: string[];
}

export class AuditSection extends Component<SectionProps, AuditState> {
  constructor(props: SectionProps) {
    super(props);
    this.state = { records: [], actor: "", action: "", problems: [] };
    this.handleActor = this.handleActor.bind(this);
    this.handleAction = this.handleAction.bind(this);
    this.handleFilter = this.handleFilter.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.handleFilter();
  }

  handleActor(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ actor: event.target.value });
  }

  handleAction(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ action: event.target.value });
  }

  async handleFilter(): Promise<void> {
    try {
      this.setState({ records: await this.props.api.auditLog({ actor: this.state.actor || undefined,
                                                               action: this.state.action || undefined }),
                      problems: [] });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const rows: ReactElement[] = [];
    for (const record of this.state.records) {
      rows.push(<tr key={record.sequence}><td>{new Date(record.at * 1000).toISOString()}</td><td>{record.actor}</td>
        <td>{record.action}</td><td><code>{JSON.stringify(record.target)}</code></td>
        <td><code>{JSON.stringify(record.detail)}</code></td></tr>);
    }
    return (
      <section aria-label="audit log">
        <h2>Audit log</h2>
        <ProblemList title="Audit" problems={this.state.problems} />
        <div className="toolbar">
          <label>Actor <input value={this.state.actor} onChange={this.handleActor} /></label>
          <label>Action <input value={this.state.action} onChange={this.handleAction} /></label>
          <button type="button" onClick={this.handleFilter}>Filter</button>
        </div>
        <table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Target</th><th>Detail</th></tr></thead>
          <tbody>{rows}</tbody></table>
      </section>
    );
  }
}

export function AdminPage(props: SectionProps): ReactElement {
  return (
    <div className="admin">
      <RolesSection api={props.api} />
      <QuotasSection api={props.api} />
      <BudgetsSection api={props.api} />
      <PoolsSection api={props.api} />
      <DataPoliciesSection api={props.api} />
      <AuditSection api={props.api} />
      <ParityPanel items={[{ name: "audit_log" }, { name: "set_roles" }, { name: "set_tenant_quota" },
                           { name: "set_data_policy" }]} />
    </div>
  );
}
