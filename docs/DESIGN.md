# Computer Use RL World Platform: Design

Status: draft v2, 3 October 2026. v2 adds the web console.

Origin: merges plan A (substrate, RL plumbing, sizing, phases) with plan B (environment model, state factory, identity, network world, moat) plus the cross review of both.

## 1. Summary

We are building a platform that manufactures, runs, scores and replays worlds for training and evaluating computer use agents. It runs thousands of concurrent microVMs with specific operating systems and apps (Excel, browser variants), seeds them with coherent synthetic state and identities, lets any policy endpoint drive them and grades them with verifiers plus any judge endpoint.

Researchers drive it from one web console: configure an experiment, preflight it, launch it, watch it live and review the results, all as views over the same versioned specs the API accepts.

Six decisions carry the design:

1. **Fork from memory snapshots instead of booting.** One primitive gives fast reset, GRPO groups, branching and most of the density.
2. **Windows is a separate product line shaped by Microsoft licensing**, so licensing is settled in week 1.
3. **Rollout inference, not VMs, is the cost center.** Size everything with Little's law.
4. **Reward quality is the product.** Verify from real state before teardown, outside the agent's trust boundary; judges come last.
5. **The moat is worlds, not VMs:** state factory, identity and licensing, app clones with measured fidelity, validated task generators.
6. **One web console over versioned specs.** Configuration, live watching and review share one app; nothing configured there exists outside a reproducible spec.

## 2. Goals, non goals, success metrics

### Goals

1. Launch thousands of heterogeneous envs (Linux and Windows first) from immutable, content addressed specs.
2. Reset, fork or checkpoint any env in about a second or less.
3. Plug in any policy or judge: frontier APIs, user endpoints or self hosted models.
4. Produce rewards researchers trust, with provenance on every number.
5. Feed on policy and off policy trainers without the trainer knowing how envs run.
6. One web console where a researcher configures, preflights, launches, watches and reviews an experiment, at full parity with the API, SDK and CLI.

### Non goals for v1

1. macOS, mobile and GPU pools (designed for, built in phase 4).
2. Hosting customer model weights (BYO endpoints only).
3. Real SaaS tenants for training (clones train, real tenants only evaluate).
4. Building an RL trainer or an inference engine.
5. Rebuilding an experiment tracker: training curves stay in Weights & Biases or MLflow, linked from the console.

### Platform SLOs (confirm in phase 0)

1. Lease to first observation: p99 under 1 s on Linux, under 3 s on Windows.
2. Env errors under 1% of episodes; restore failures under 0.1%.
3. Verifier precision and recall at least 0.95 against human labels.
4. Judge agreement with humans: Cohen's kappa at least 0.8.
5. At least 95% of shipped tasks still pass every gate when audited again.
6. Rollout supply keeps trainer GPUs above 90% busy.
7. Cost per 1,000 episodes reported per run, split into VMs, inference, judge and storage.
8. Console: preflight returns in under 10 s; a 50 trajectory cohort renders in under 3 s; a dry run on a warm pool shows its first live frame in under 30 s.

## 3. Environment model

Environment = runtime + OS + apps + state + identity + network + task + observations + actions + verifier + reward.

The parts change at different rates, so they live in four content addressed, versioned layers:

1. **Image:** runtime, OS, apps, display, hardening. Built rarely, snapshotted once per pool at "app ready".
2. **World:** state, identities, network, clock. Generated from a seed; one world hosts many tasks.
3. **Task:** instruction, allowed actions, observations, budget, verifiers, judge rubric, oracle, user simulator, difficulty, split. Generated from templates over a world.
4. **Run:** type (rl, eval or data), policy endpoint, judge endpoint, trainer template, sampling, group size, concurrency, priority.

An **episode** is the tuple (image snapshot digest, world digest, task version, run id, seed, policy version), recorded on every trajectory.

An **experiment** groups runs (a sweep or an ablation) under one hypothesis with notes and a budget. It is what the web console creates, launches and compares.

Lifecycle:

1. The image builder boots an image, waits for its readiness probe and takes the **base snapshot**.
2. Materializers load a world into a restored base VM plus its sidecars. If loading takes more than ~5 s or is nondeterministic, take a **world diff snapshot**; otherwise materialize on every restore.
3. Task setup injects files, opens windows and mints sessions, producing the **task start state**.
4. `fork(n)` clones the start state for a group; `checkpoint()` takes a diff snapshot mid episode for branching.
5. `finish()` freezes input, extracts state, verifies and scores; only then is the VM destroyed or recycled.

Example spec:

```yaml
experiment:                                # what the web console saves; each sweep point becomes a run
  name: recon_difficulty_sweep
  hypothesis: distractors hurt reconciliation more than corrupted inputs
  sweep: {task.difficulty.distractors: [0, 5, 10], task.difficulty.corrupted_inputs: [0, 2]}
  budget_usd: 2500
  tracker: wandb

image:                                     # built rarely, snapshotted at "app ready"
  id: win2025_office_v4
  base: windows_server_2025_desktop@sha256:<digest>
  pool: {vmm: qemu_templated, machine: pc-q35-9.0, cpu_model: fleet_baseline}
  apps: {excel: office_ltsc_2024, edge: stable_pinned}
  display: {width: 1280, height: 800, dpi: 96, theme: light, locale: en_US}
  hardening: [no_auto_update, no_defender_scan, no_first_run, no_protected_view, no_animations]
  ready_probe: {window: Excel, screen_stable_ms: 500}

world:                                     # one seed, many tasks
  generator: worlds.mid_market_company
  seed: 1842
  size: {employees: 40, customers: 300, emails_per_person: 120}
  materialize: [mail, calendar, crm, files, chat, browser_profile]
  identity: {idp: keycloak, users: world.employees, sessions: mint_after_restore}
  network:
    dns_overrides: {crm.example.com: crm_clone, mail.example.com: mail_sidecar}
    trusted_ca: world_ca
    internet: replay_only
  clock: {fixed: "2026-03-14T09:00:00"}

task:                                      # injected after restore, versioned with its verifiers
  generator: tasks.finance.reconcile_invoices
  difficulty: {apps: 3, oracle_steps: 30, distractors: 5, corrupted_inputs: 1, ambiguity: low, dependency_depth: 2}
  allowed_actions: [computer, control]
  observation: [screenshot, accessibility_tree]
  user_simulator: {persona: finance_manager, hidden_intent: from_generator}
  verifiers:
    exact: [verifiers.excel.reconciliation_values_match]
    semantic: [verifiers.mail.summary_lists_discrepant_invoices]
  side_effects: {allowed_changes: [files/reconciliation.xlsx, mail.sent]}
  judge_rubric: rubrics/summary_email_quality.md
  oracle: oracles.finance.reconcile_invoices
  network_faults: {latency_ms: [0, 200], packet_loss: [0.0, 0.01]}
  split: train

run:                                       # who plays, who grades, how much
  type: rl                                 # rl, eval or data
  trainer: {template: verl_grpo_7b_vlm, launcher: kubernetes}
  policy: {endpoint: "http://vllm.internal/v1", model: my_policy, token_ids: true, logprobs: true}
  judge: {endpoint: frontier, model: <pinned_model_id>}
  group_size: 8
  max_concurrent_envs: 4000
  priority: training
```

## 4. Decisions that shape everything

1. **Memory snapshot fork is the core primitive.** Copy on write disks plus warm pools are not enough; without memory snapshots every Windows plus Excel episode pays a 60 to 90 s boot. With them Linux resets take ~100 to 300 ms and Windows ~1 to 2 s, while clones share RAM pages, which is where most density comes from. Firecracker's 125 ms boot figure doesn't matter for GUI envs because app readiness dominates.
2. **One pool per OS family.** Firecracker for Linux; QEMU/KVM VM templating for Windows, since Firecracker only runs Linux and OSv guests; license included cloud Windows instances as a slow eval stopgap; Apple hardware for macOS later. Budget ~40% of infra effort for Windows.
3. **Licensing picks the cloud and the Office strategy.** Windows Server Datacenter, licensed on every core of a host, covers unlimited Windows Server VMs on it, so Windows envs run Windows Server with Desktop Experience rather than Windows 11 (per user VDA or Enterprise rights plus a vTPM per VM). Microsoft's Listed Provider rules restrict BYOL on AWS, Google Cloud and Alibaba, so Windows hosts live on Azure Dedicated Host or a bare metal provider outside that list renting Datacenter under SPLA. Office is ambiguous for non human users: M365 Apps under Shared Computer Activation needs an eligible license and activation token per user; Office LTSC is licensed per device; copying one activated image to 1,000 clones is not legitimate. Until counsel rules, train mostly on LibreOffice or OnlyOffice and keep desktop Excel for a licensed eval slice.
4. **Little's law sizes the system and inference is the cost center.** ~4,000 concurrent envs need 10 to 20 hosts but ~100 H100s for a 7B VLM policy (section 8).
5. **Frontier endpoints serve eval and data, not training scale rollouts.** ~1,000 calls/s at ~20k tokens is ~1.2B input tokens per minute, far past standard rate limits. Admission control is tied to the inference budget so VMs and seats never idle behind a rate limiter.
6. **Verify before teardown, outside the trust boundary.** Rewards come from state pulled from the live VM and its sidecars at episode end, checked by code the agent can't see or reach; judges run last and only where code can't decide.
7. **Determinism beats realism for training.** Self hosted clones, fake DNS, record/replay, fixed clocks, pinned versions and turn based stepping; the live web and real SaaS are for eval.
8. **Invest in worlds; rent or reuse the VM layer where possible.** Fork and reset are table stakes that vendors are commoditizing; differentiation is the world factory, identity and licensing, clone fidelity, verifiers and task generators.
9. **One web console, specs as the source of truth.** The console edits the same versioned specs the API accepts and shows the equivalent CLI and SDK call on every page; preflight (validation, cost estimate, dry run) turns the hard lessons of this design into guardrails before budget is spent.

## 5. Architecture

```text
┌──────────────────────┐ ┌───────────────────────┐    ┌───────────────────────┐
│ web console          │ │ rollout workers       │ act│ model gateway         │
│ configure, preflight │ │ in trainer jobs for   │◄──►│ policy: own vLLM or   │
│ launch, watch live,  │ │ RL, platform hosted   │    │ SGLang, frontier, BYO │
│ review, label, admin │ │ for eval and data     │    │ judge: frontier or    │
└──────────┬───────────┘ └───────────┬───────────┘    │ custom                │
           ▼ API                     ▼ env API        └───────────┬───────────┘
┌──────────┴─────────────────────────┴───────────┐                ▲ judge calls
│ control plane API: specs, runs, sweeps,        │    ┌───────────┴───────────┐
│ launcher, scheduler, warm pools, leases,       │    │ reward service        │
│ quotas, identity and license broker,           │    │ exact, semantic,      │
│ task registry, curriculum sampler,             │    │ judge tiers           │
│ snapshot store, trajectory store, live streams │    └───────────┬───────────┘
└──────────┬─────────────────────────────────────┘                ▲ state pull
           ▼ fork, pause, restore                                 │
┌──────────┴──────────────────────────────────────────────────────┴───────────┐
│ node agents on bare metal KVM hosts                                         │
│ Firecracker pool: Linux GUI envs      QEMU templated pool: Windows envs     │
│ per env private network: sidecars, fake DNS and CA, replay proxy, IdP       │
└──────────┬──────────────────────────────────────────────────────────────────┘
           ▲ images, worlds, task start states
┌──────────┴──────────────────────────────────────────────────────────────────┐
│ factory: image builder, world generators, task generators, validation gates │
└─────────────────────────────────────────────────────────────────────────────┘
```

Episode flow:

1. A researcher saves an experiment in the web console, runs preflight and launches it. The control plane expands sweeps into runs, then starts platform hosted rollout workers for eval and data runs or a trainer job through the launcher for RL runs.
2. A rollout worker asks the control plane for a lease on (task, world seed, group size).
3. The scheduler places by snapshot locality, capacity, OS pool, license and identity availability plus tenant quota; node agents fork the start state n times.
4. Each step: the env API returns an observation, the worker calls the policy through the gateway, the adapter maps the reply to canonical actions, the env acts, waits for the screen to settle, pauses the VM and returns the next observation.
5. On a terminal action or budget exhaustion the worker calls `finish`: the reward service freezes input, pulls state, runs exact then semantic verifiers, then the judge through the gateway, returning a reward record with provenance.
6. The worker writes the trajectory to the trajectory store and hands it to the trainer; the console streams progress, live frames and results from the control plane.
7. The VM is destroyed or recycled; leases, seats and identities are released.

Feedback loops:

1. Trainer jobs to the policy servers behind the gateway: weight sync with bounded staleness.
2. Trajectory analytics to curriculum: pass rates per template and difficulty bucket steer the sampler and the generators.
3. Failure clustering to bug queues for verifiers, adapters, envs and tasks.
4. Judge and verifier disagreement plus human audits to rubric and verifier fixes.
5. Console review actions (flags, labels, verifier bug reports, failure to task drafts) to the factory and verifier queues.

## 6. Components

### 6.1 Execution substrate

1. Bare metal KVM hosts with large RAM and local NVMe. Nested virtualization works but taxes VM exits, which GUI workloads generate constantly.
2. **Linux pool:** Firecracker under its jailer, Xvfb plus a light window manager in the guest. Guest RAM is restored by `mmap(MAP_PRIVATE)` of a page cached snapshot file so clones share untouched pages (UFFD lazy loading while the file is still remote). Disks are reflink or dm snapshot overlays of the exact disk captured with that memory.
3. **Windows pool:** QEMU templating. The template's RAM lives in a file; clones map it private and load only device state through QEMU's `x-ignore-shared` migration capability, the trick Kata Containers uses. Windows Server 2025 (no TPM state to clone), virtio devices, qcow2 overlays, pinned machine type and CPU model. Cloud Hypervisor deserves one spike if a single Rust VMM matters. Hyper V hosts give automatic activation (AVMA) but no public memory fork primitive, so they only suit a slow pool.
4. **Cloud Windows pool:** license included cloud instances for early eval while licensing settles. Resets take minutes, so never for training.
5. **Later pools:** macOS on Apple hardware via Virtualization.framework (the EULA caps each Mac at 2 VMs, so Safari and iOS mean a Mac fleet; Playwright WebKit covers the Safari engine on Linux); Android via Cuttlefish; GPU envs on QEMU with GPU partitioning, since Firecracker has no GPU.
6. **Snapshot compatibility:** snapshots are bound to VMM version, machine type, host CPU model and CPU features. Pin a CPU template or model per pool, treat VMM upgrades and new host SKUs as a rebuild wave and keep the snapshot builder fully automated.
7. **Clone hygiene:** clones share MAC, IP, hostname, SID, RNG state and clock. Each clone gets its own network namespace with the same guest IP behind NAT (Firecracker's documented clone networking). After every restore: resync the clock (guest agent, or `guest-set-time` through the QEMU guest agent), reseed the RNG, mint fresh identities and sessions, refresh license tokens.
8. **Turn based stepping:** act, wait for settle (screen hash stable for 300 to 500 ms plus app hints such as CDP network idle, 3 s cap), observe, pause vCPUs. Slow and fast endpoints see the same world; paused VMs burn no CPU.
9. **Resource caps per VM:** vCPU and memory through cgroups, block and network rate limiters, disk overlay quota, egress bandwidth cap. Fork bombs and disk fills stay inside one VM.
10. **No hugepages for cloned guest RAM:** copy on write at 2 MB granularity multiplies memory per clone.
11. **Density planning numbers** (replace in phase 0): a Linux browser env uses ~0.5 to 1 GB unique RAM and ~0.4 cores on average, so 200 to 400 per 192 thread, 768 GB host; Windows plus Excel uses ~1.5 GB and ~1 core, so ~100 per host.

### 6.2 Image and snapshot pipeline

1. Linux images are authored as Dockerfiles and converted to an ext4 rootfs plus a minimal guest kernel (the E2B pattern). Windows images come from Packer with autounattend, `virtio-win` drivers and sysprep.
2. Hardening removes drift and first run friction: OS, Office and browser auto updates; Defender scans; search indexing; telemetry; OOBE and first run dialogs; Protected View and activation prompts; UI animations.
3. Pinned browser matrix: Chrome for Testing builds, Firefox ESR, Edge, Playwright WebKit as the Safari engine stand in. Display, DPI, theme, locale and fonts are image parameters.
4. Snapshot store: content addressed chunks in object storage, pulled lazily to local NVMe, prefetched to hosts when runs are scheduled.
5. One base snapshot per image; world diff snapshots only when materialization is slow; task state injected after restore. Otherwise thousands of variants at 4 to 8 GB each mean tens of TB to distribute.
6. An image bump automatically reruns the validation gates of every dependent task and verifier.
7. Retention: every snapshot referenced by a training run, eval report or published result is pinned. Specs alone don't reproduce worlds because installers and package mirrors change.

### 6.3 Observation and action interface

1. **Observations:** screenshots (WebP), accessibility trees (`AT-SPI` on Linux, UIA on Windows) pruned to visible elements so an Excel grid doesn't explode, DOM through CDP, terminal output, file and app state diffs, app API reads (Excel COM, CDP).
2. **Canonical actions** in five modalities:
   * computer: move, click, double click, drag, scroll, type, key combo, wait
   * browser: goto, click or fill by element id, select, back, tab control (BrowserGym style primitives)
   * terminal: exec with timeout, returning stdout, stderr and exit code
   * native: app API calls such as Excel range writes
   * control: ask_user, answer, done, give_up
3. **Every task declares its allowed modalities.** An Excel GUI task that also allows terminal gets solved with openpyxl, which trains a different skill than intended.
4. **Per model adapters** map canonical actions to each frontier computer use API and open model format (`UI-TARS`, Qwen VL style): schema, coordinate convention, preferred resolution, key names. Adapter bugs silently cap scores, so every adapter has golden tests.
5. **Linux guest agent:** runs as root on vsock only, unreachable from the policy's unprivileged shell.
6. **Windows IO at the hypervisor:** QMP `screendump` and `input-send-event` on a USB tablet, so session 0 isolation, the UAC secure desktop or a tampering agent can't break observation. An autologon helper in the interactive session serves the UIA tree and Excel COM state, since Microsoft doesn't support Office automation from services.
7. **Exposed interfaces:** gRPC/HTTP env API, a Python Gymnasium style client, an MCP server so any agent harness can drive an env as tools, plus importers for OSWorld tasks and the BrowserGym interface so public suites run as eval.

### 6.4 World and state factory

1. A world is a coherent synthetic organization: people, roles, projects, customers, deals, invoices and their history, generated from a seed by an LLM assisted, schema validated generator.
2. **Materializers** write the world into apps so everything agrees: mailboxes, calendars, CRM records, files and folders, spreadsheets, documents, PDFs, chat history, browser profiles (history, bookmarks), database rows.
3. Start states include **partially completed workflows**: a half filled expense report, a draft reply, a spreadsheet with a broken formula.
4. **Scripted world events and NPCs:** an email that arrives mid task, a colleague who answers in chat, driven by the same simulator stack as the user simulator.
5. **App integration contract:** every app ships a materializer (write world state), an extractor (read normalized state for verifiers and diffs) and a reset hook. This contract is the unit of work for adding an app and the place fidelity gets tested.
6. **v1 app clones** are open source equivalents behind real looking hostnames: Odoo (ERP), Twenty or EspoCRM (CRM), Mattermost (chat), Stalwart or Postfix with Dovecot (mail), Nextcloud (files, calendar), Gitea or GitLab (code), WebArena's shop, forum and wiki sites.
7. **Clone fidelity metric:** for a fixed task set, compare agent success on each clone with success on the real app (eval slice only). A large gap means training won't transfer.
8. **Perturbations:** distractor records, corrupted inputs (typos, malformed CSV), popups, cookie banners, slow loads.
9. Data is synthetic end to end; no real PII enters a world.

### 6.5 Identity and entitlements plane

1. One broker leases everything an episode must hold: synthetic identities, IdP accounts, OAuth sessions, SaaS seats, license seats, secrets.
2. A self hosted IdP (Keycloak) fronts every clone with SSO, so agents face realistic login flows.
3. **Sessions are minted per episode after restore** and injected into the browser profile by the guest agent, never baked into shared snapshots. A thousand clones replaying one session cookie look like account takeover to any real SaaS and get the account locked.
4. **Real SaaS state lives outside the snapshot.** VM reset never undoes a sent email or an edited record; a shared tenant also leaks one rollout's actions into another's world. Real SaaS is eval only, with one disposable tenant or sandbox per episode where the vendor offers it (Salesforce scratch orgs, for example), reset through vendor APIs.
5. **License seats:** pools per product (Windows Server per host, Office, FlexLM style floating licenses), acquired at lease and released at close. Scheduling blocks when a pool is empty. KMS runs inside the private network for activation; license tokens are injected after restore.
6. **Secrets:** vault backed, short lived, scoped to one episode, injected just in time. BYO endpoint keys never enter a VM.
7. **Audit:** the seat hour and identity lease log is the compliance record (SPLA usage is reported monthly).

### 6.6 Network world

1. Every env gets a private network with its sidecars: app clones, mail, chat, IdP, DNS, replay proxy.
2. **Fake internet:** DNS overrides map real hostnames to clones; a private CA trusted by the image keeps HTTPS working unchanged.
3. **Record/replay proxy** (mitmproxy or WARC based) serves sites you can't clone. Replay mode is deterministic; record mode refreshes captures on a schedule.
4. **Live internet is eval only**, through a logging egress proxy with allowlists and rate limits.
5. **Fault injection** (netem latency, loss, bandwidth) is a task parameter for robustness training.
6. Default deny egress per namespace; `169.254.169.254` and control plane subnets are blocked.

### 6.7 Orchestrator and control plane

1. **Stack:** the control plane API is served by two services behind one gateway: `fleetd` in Go for the hot path (leases, scheduling, env steps) plus the node agents (Firecracker Go SDK, QMP), and `experimentd` in Python for specs, preflight, sweeps, runs, the launcher and the reward pipeline. Python also covers the SDK, verifiers and generators. Postgres for specs, experiments, runs, tasks and leases; NATS or Redis for queues and heartbeats; S3 or GCS for snapshots and trajectories; ClickHouse for step analytics.
2. **VMs are not Kubernetes pods:** pod scheduling latency and etcd churn can't keep up with thousands of VMs that live for seconds. Node agents run under k8s or Nomad; VM placement is ours. Read `e2b-dev/infra` (Firecracker plus Nomad) before writing ours.
3. **API parity:** the web console, SDK and CLI all use the same public API; there is no private UI backdoor.
4. **Experiments and sweeps:** the control plane expands an experiment into runs, enforces its budget and owns each run's lifecycle.
5. **Trainer launcher:** RL runs start trainer jobs from templates (verl, SkyRL, `prime-rl`, AReaL configs) through pluggable backends (Kubernetes jobs, Slurm, SkyPilot); external trainers attach with a scoped run token instead. The platform launches trainers but never becomes one.
6. **Placement dimensions:** snapshot locality, CPU, memory, GPU, OS pool, license and identity availability, tenant quota.
7. **Priority classes with reserved capacity:** interactive (teleop, debugging, dry runs) over eval over training. Preempting an RL episode wastes it, so classes reserve capacity rather than preempt.
8. **Admission control** shares a token bucket with the gateway so envs never idle behind rate limits.
9. **Leases** carry TTLs and heartbeats; a reaper destroys orphans and releases seats and identities; every lease is tagged with run, tenant and cost center.
10. **Warm pools** of restored, paused VMs for hot snapshots, mostly for Windows.
11. **Env error vs agent failure:** every episode ends with a termination label (success, failure, max steps, agent error, env error). Env errors (restore failure, VM crash, guest agent timeout, sidecar failure, verifier exception, judge timeout) are excluded from training and retried; an "unknown" bucket is audited weekly.
12. **Multi tenancy** (product mode): per tenant namespaces, no shared sidecars across tenants, dedicated host pools for enterprise tenants, quotas, billing, per tenant data policy on which endpoints may receive screenshots.

### 6.8 Model gateway

1. Two independent endpoints per run, policy and judge, each pointing at a frontier API, a user endpoint or self hosted vLLM/SGLang. OpenAI compatible chat covers most; native adapters cover frontier computer use APIs.
2. Every call records model version, sampling config, latency, tokens and cost. Responses are cached for reproducible evals.
3. **Exact model versions are pinned:** an alias that moves silently changes your baseline or, worse, your reward function.
4. Per endpoint concurrency limits, retries with jitter, circuit breakers, rate limit state shared with admission control.
5. **Training mode is token in, token out:** passthrough to the trainer's engines recording exact prompt and completion token IDs, logprobs, loss masks (only model generated tokens train) and policy version. Generic OpenAI compatible endpoints don't return token IDs, so they are eval and data endpoints, not RL endpoints.
6. **Cache friendly prompts:** only the tail changes (text history grows, the single latest screenshot sits last). Deleting an old screenshot mid prompt kills prefix caching, a 3x to 6x swing in rollout GPUs.

### 6.9 Reward system

1. **Order:** freeze input; extract state from the VM and sidecars (optionally keep a final snapshot for audits); compute the world diff; run tier 1, tier 2, tier 3; compose; record provenance; only then tear down.
2. **Tier 1, exact state checks:** cell values, formulas judged by computed results, formatting, file location, DB rows, URL and DOM state. For Excel: `SaveCopyAs`, then openpyxl reading cached values.
3. **Tier 2, semantic programmatic checks:** normalization, tolerances, equivalence (any formula that yields the right values from the right references passes), fuzzy matching of named entities in text.
4. **Tier 3, judge:** only for what code can't decide (tone, summary quality, layout). Inputs are a rubric of binary items, extracted state, the world diff and key screenshots, with on screen text treated as untrusted. Output is structured JSON per item with evidence. Results are cached by (final state hash, rubric version, judge model).
5. **Side effect checks:** each task declares allowed changes; any world diff outside that scope (deleted files, extra emails, edited unrelated records) fails or penalizes the episode.
6. **Hidden ground truth:** answers, oracles and verifier code live outside the VM; the agent can never read or modify its own reward.
7. **Reward record:** total, components (outcome, partial credit, side effects, efficiency), termination label and provenance (verifier versions, judge model, rubric version, state hash). Default to binary outcome behind a side effect gate; allow partial credit only for long horizon tasks after validation, since shaped rewards get hacked first.
8. **Judge quality:** calibrate on 300 to 500 human labeled episodes (kappa at least 0.8), recalibrate on any judge model change, keep a red team suite of episodes whose screens try to manipulate the judge, use ensembles only for low confidence cases.
9. **Monitoring:** judge and verifier disagreement, reward spikes per task (the classic hacking signal), human audit samples from every run.

### 6.10 Task system and curriculum

1. Tasks are generators over worlds with difficulty axes: number of apps, oracle steps, ambiguity, distractors, corrupted inputs, dependency depth across apps, UI perturbations.
2. **Ambiguity requires a user simulator:** an LLM persona holding the hidden intent answers `ask_user` (the tau bench pattern). Tasks score whether asking was warranted.
3. **Validation gates** before a task ships:
   * the verifier fails on the untouched start state
   * it passes on the scripted oracle
   * it passes on at least one alternative valid solution (false negatives hurt as much as reward hacking)
   * it fails on near misses such as the right value in the wrong cell
   * results are identical across 3 reruns
   * a strong agent solves the task at least once in k tries

   Tasks failing a gate go to human review.
4. **Curriculum:** the sampler tracks pass rate per template and difficulty bucket per policy version, targets the learnable band (roughly 20% to 80%), drops groups that are all success or all failure (zero advantage) and asks generators for more tasks at the frontier difficulty.
5. **Splits:** hold out whole apps, sites, templates and world seeds for eval; block overlap with OSWorld, Windows Agent Arena, WebArena and WorkArena.
6. Offline worlds run at a fixed date so "last week" means the same thing in every episode.
7. **Authoring kit:** the CLI and the console's authoring view (6.14) share one flow: lease a dev env, record an oracle through teleop, run every gate, submit for review.

### 6.11 Trajectory system and analytics

1. **Per step:** observation refs, sampled accessibility snapshots, action, raw model input and output with token IDs and logprobs, tool calls, state diff (cheap per step sources such as file events and sidecar change logs; full extraction at episode end), timings (model, env, settle), tokens, cost, VM events.
2. **Per episode:** the episode tuple, reward record, termination label, final state extract, optional final snapshot reference.
3. **Replay two ways:** exact replay from recorded observations; rerunning the actions from the start snapshot, which measures divergence as a determinism health metric.
4. **Views:** the trajectory scrubber, cohort view, comparisons and failure clusters render in the web console (6.14).
5. **Failure clustering:** an LLM labels each failed episode (grounding, planning, wrong state, gave up, env issue, suspected verifier false negative); clusters roll up per template and policy version; suspected false negatives feed the verifier bug queue.
6. **Data products:** SFT sets from successful trajectories, preference pairs from GRPO groups, eval reports.
7. **Storage:** ~1,000 steps/s yields 5 to 13 TB of screenshots per day. Keep full resolution for eval, failures and samples, thumbnails for the rest, with lifecycle rules.

### 6.12 Trainer and eval interface

1. Training stays separate from envs: rollout workers are a library trainers import, with adapters for verl, SkyRL, `prime-rl` and AReaL.
2. Supported regimes: GRPO and PPO (on policy, need logprobs), async RL with a bounded staleness window, rejection sampling and expert iteration, DPO from group pairs.
3. GRPO groups are k forks of one start state; tree rollouts branch from `checkpoint` at step t.
4. Every trajectory carries policy version and staleness; the trainer drops or reweights stale ones per its algorithm.
5. **Eval mode:** fixed suites, k samples per task, confidence intervals on success rates, cached model responses, breakdowns per app and difficulty.
6. **Launch path:** RL runs launched from the console start trainer jobs through the launcher (6.7); trainers started elsewhere attach with a run token.

### 6.13 Security

Threat model: the agent is hostile code with a shell, tenants are mutually untrusted and the evaluator is a target.

1. **Escape:** KVM isolation, jailer and seccomp, unprivileged VMM processes, prompt host kernel patching. Mixed tenancy hosts follow Firecracker's production host guidance (SMT and KSM off); single tenant training pools may keep SMT for density.
2. **Tampering with observation or reward:** hypervisor level IO on Windows, root guest agent on Linux, verifiers and answers outside the VM, the judge red team suite.
3. **Exfiltration:** default deny egress, metadata and control plane blocked, just in time scoped secrets.
4. **Abuse of third parties:** no open internet in training; eval egress goes through a logging, rate limited proxy, because an agent with a shell on your IPs is your legal exposure.
5. **Resource exhaustion:** per VM caps (6.1).
6. **Cross tenant leakage:** per tenant namespaces and sidecars, dedicated pools for enterprise tenants.

### 6.14 Web console (configure, launch, watch, review)

One web app is the front door for configuring, launching, watching and reviewing experiments. It talks only to the public control plane API, so anything done in the console can be done from the SDK or CLI and the reverse.

Principles:

1. **Specs are the only source of truth.** Every form edits the same versioned experiment, image, world, task and run specs the API accepts. Saving creates an immutable digest; a YAML view sits beside every form with import, export and diff. No configuration lives only in the UI.
2. **Parity:** every page shows the equivalent CLI command and SDK snippet; parity tests keep the console, SDK and CLI equivalent.
3. **Running runs are immutable:** live controls touch only operational knobs (concurrency, priority, budget, pause); changing tasks, endpoints or rewards creates a new run.
4. **Guardrails over documentation:** preflight blocks or warns on known traps before budget is spent.

Configure:

1. **Experiment builder:** pick an image (OS, apps, browser matrix, display), a world generator and its parameters, task generators with a difficulty distribution and allowed modalities, policy and judge endpoints, reward composition, run type, group size, concurrency, budget and priority.
2. **Catalogs:** images, worlds, task templates, verifiers, rubrics and endpoints, each with version, gate status, clone fidelity, pass rates per policy and usage.
3. **Sweeps:** grid or random over any spec field (model, temperature, difficulty, group size, perturbations); each sweep point becomes a run inside one experiment.
4. **Presets and cloning:** start from a template or clone any past run, change a few fields, review the spec diff.
5. **Endpoint registry:** register BYO policy and judge endpoints, test them, store keys in the vault (write only from the UI).

Preflight, before every launch:

1. **Validation:** schema, allowed modalities against verifiers, gate status of selected tasks, image and task compatibility, license and identity pool headroom, tenant quota.
2. **Endpoint checks:** reachability, latency, rate limits, token IDs and logprobs present when the run trains.
3. **Estimate** from Little's law using measured pool densities and endpoint latencies: concurrent envs, hosts per pool, policy GPUs, judge tokens, storage, cost, wall clock time.
4. **Trap warnings:** a frontier policy at training scale concurrency, a generic OpenAI compatible endpoint on an RL run, a prompt format that defeats prefix caching, tasks missing the alternative solution gate, Office pools short of seats.
5. **Dry run:** one episode end to end, streamed live with its reward breakdown, before the budget is committed.

Launch and control:

1. Eval and data runs start on platform hosted rollout workers; RL runs start a trainer job through the trainer launcher (6.7) or issue a run token that an external trainer attaches with.
2. Live controls: pause, resume, stop, change concurrency or priority within quota, extend the budget.
3. Hard budget caps are enforced by the control plane; in product mode, launches above a threshold need approval.

Watch live:

1. **Run dashboard:** episodes per second, success and reward over time, env error rate, latency split (model, env, settle), rate limit hits, cost burn against budget, trainer GPU utilization for RL runs, with links to the experiment tracker for training curves.
2. **Live wall:** sampled thumbnails (about one frame per second) from running envs; click any tile to watch at full rate.
3. **Takeover:** an operator can teleop a live env through noVNC; the episode is marked human intervened and excluded from training and eval statistics. Recorded demonstrations double as SFT data and oracle solutions.
4. **Alerts:** reward spikes per task, error rate jumps, budget thresholds, run completion, sent to Slack or email.

Review:

1. **Trajectory scrubber:** screenshots with actions overlaid, model input and output, state diffs, reward components with verifier output and judge rationale, deep links to any step.
2. **Cohort view:** scrub 50 failures side by side, filtered by task, difficulty, termination label or failure cluster.
3. **Comparisons:** failure clusters per template and policy version, run against run, policy version against policy version on the same tasks.
4. **Reward provenance:** drill from a score down to the verifier results, judge rationale, extracted state and world diff behind it.
5. **One click actions:** flag, label, file a verifier bug, turn a failure into a task draft, add to an audit sample.
6. **Reproduce:** rerun with the same spec digests, or clone and edit.

Human queues:

1. Labeling for verifier and judge calibration sets.
2. Review of tasks failing gates, audit samples and episodes flagged by reward spike alerts.
3. Task authoring in the browser with the same flow as the CLI kit: lease a dev env, record an oracle through teleop, run gates, submit for review.

Admin:

1. SSO (OIDC) for platform users, separate from the synthetic identity plane; roles for viewer, researcher, operator, labeler and admin.
2. Tenants, quotas, budgets, license and identity pool status, plus an audit log of every launch, edit, stop and takeover.
3. Per tenant data policy: which endpoints may receive screenshots, retention windows, export rights.

Implementation:

1. TypeScript and React frontend; a thin backend for frontend aggregating the control plane API, ClickHouse and signed object store URLs.
2. Server sent events for metrics and episode status; frame streams relayed from node agents through the control plane with per session authorization, opened only while someone is watching.
3. Thumbnails first and full resolution on demand, so a 50 trajectory cohort loads in seconds.
4. Training curves stay in the experiment tracker; the console links runs to tracker runs instead of rebuilding them.

### 6.15 Observability

Platform health dashboards for operators (Grafana over Prometheus and ClickHouse): lease latency p50/p99 per pool, restore failures, env error rate per image and app, step latency split, seat and identity utilization, gateway rate limit hits, judge spend, verifier precision and recall, replay divergence, clone fidelity, cost per 1,000 episodes. Run level views live in the web console (6.14). Chaos tests kill hosts, sidecars and the gateway mid run.

## 7. Determinism and reproducibility

1. **Same image:** guaranteed by image digests and pinned versions.
2. **Same start state:** guaranteed by forking a pinned snapshot.
3. **Same observations for the same actions:** best effort through turn based stepping, fixed clocks, replayed network, disabled animations and updates; measured as replay divergence.
4. **Same trajectory:** only through exact replay of recorded observations, since policies sample.

Reproducing a run from six months ago needs retained artifacts (images, snapshots, world data, verifier code, judge model version, gateway cache), not just a spec hash.

## 8. Capacity and cost model

Worked example:

1. **RL step:** 512 tasks × 8 rollouts = 4,096 episodes of 25 steps at ~4 s per step (3 s model, 1 s env), ~100 s per episode.
2. **Concurrency:** ~4,000 envs and ~1,000 policy calls/s (Little's law: concurrency = throughput × latency).
3. **VM fleet:** Linux browser envs need 10 to 20 hosts (192 threads, 768 GB); Windows costs ~3x per env.
4. **Policy inference, 7B VLM:** ~1.3k image tokens per 1280x800 screenshot, contexts of 10k to 40k tokens. Prefill is ~1.5M new tokens/s when prefix caching holds and ~10M when it doesn't; KV cache for 4,000 live contexts at ~20k tokens is ~4.6 TB (~56 KB per token for a Qwen 7B class model in bf16). Net: on the order of 100 H100s, several hundred with a cache hostile prompt format.
5. **Judge:** scoring every episode at ~10k tokens is ~40M input tokens per RL step; verifiers first and state hash caching cut this sharply.
6. **Frontier policy at the same rate:** ~1.2B input tokens per minute, far past standard rate limits.
7. **Storage and network:** ~150 MB/s of screenshots (~1.2 Gbps), so env hosts and policy GPUs share a region; 5 to 13 TB/day before lifecycle rules.
8. **Bill shape:** rollout GPUs dominate; the VM fleet is smaller; a frontier judge can rival both unless verifiers do most grading; Windows Server licenses amortize per host while Office licenses scale per user or device.

The console's preflight estimator runs this same model per run, fed by measured densities and endpoint latencies.

## 9. Build vs buy, moat

1. **Adopt:** Firecracker, QEMU, vLLM or SGLang, the RL trainers, Postgres, NATS, ClickHouse, Keycloak, open source app clones, mitmproxy, an existing experiment tracker.
2. **Rent or evaluate:** Linux sandboxes with snapshot fork (E2B, Morph, Daytona, Prime Intellect sandboxes) for phases 1 to 3 if speed beats ownership. Benchmark GUI desktop support, memory fork latency, concurrency limits, cost per VM hour and Windows support; treat vendor scale claims as unverified until measured.
3. **Build:** Windows pool and templating, world factory and materializers, identity and entitlements broker, reward service and verifier library, task generators and gates, trajectory analytics, gateway adapters, the web console.
4. **Moat:** vendors are commoditizing Linux sandboxes, forking included. The defensible assets are legal Windows and Office at scale, the identity and entitlements plane, coherent multi app worlds, app clones with measured fidelity, verifiers with measured precision and recall, a large library of validated parameterized tasks. Together that is less a sandbox company than an operating system for training computer use agents.

## 10. Roadmap

### Phase 0, weeks 1 to 2: decisions and spikes

1. Engage Microsoft licensing counsel; pick the host provider with Windows rights in mind; decide product or internal platform.
2. Spike A: Firecracker plus Xvfb plus Chromium, forked from a memory snapshot.
3. Spike B: QEMU templated Windows Server plus Excel, forked from a memory snapshot.
4. Spike C: Keycloak SSO into one self hosted clone with per episode sessions minted after restore.
5. Spike D: one world generator with mail and CRM materializers and extractors.
6. Console wireframes for the configure, launch, watch and review flow, tested with two researchers.

Exit: Linux lease to first screenshot under 300 ms, Windows under 2 s; measured RAM and CPU per clone; zero session collisions across 100 clones; licensing path written down.

### Phase 1, weeks 3 to 6: single host vertical slice

1. Spec v1 (experiment, image, world, task, run); image and snapshot builders; node agent for both pools; guest agents; canonical actions with two frontier adapters and one open model adapter; env API and Python client; trajectory store.
2. Apps: Chrome against a shop and a CRM clone, LibreOffice Calc, Excel. One world generator, verifier tiers 1 and 2, 50 tasks through every gate.
3. Web console v0: spec editor with YAML view and validation, eval run launch, live view of one env, trajectory scrubber.

Exit: 200 concurrent envs on one host with env errors under 1%; a published OSWorld or Windows Agent Arena score reproduced within a few points for a known model; verifier precision and recall at least 0.95 on the labeled set; an eval run configured, launched and reviewed entirely in the console.

### Phase 2, weeks 7 to 10: fleet, gateway, judge, identity, network

1. Scheduler with priority classes, warm pools, leases and reaper, snapshot store with lazy fetch, admission control tied to the inference budget.
2. Model gateway with pinned versions and token in, token out; judge service with calibration and the red team suite.
3. Identity and entitlements broker; network world (DNS and CA, mail, chat, replay proxy, fault injection); security controls and vault.
4. Web console v1: experiments and sweeps, preflight with cost estimate and dry run, run dashboard, live wall, cohort view, comparisons, labeling and review queues, SSO and roles.

Exit: 2,000 concurrent envs across hosts for 24 h; zero leaked VMs, seats or identities; judge kappa at least 0.8; red team suite passes; preflight cost estimates within 25% of actuals.

### Phase 3, weeks 11 to 14: RL loop

1. Rollout workers in the chosen trainer, GRPO through forks, env error masking, curriculum sampler over difficulty axes, failure clustering, side effect checks, user simulator v0.
2. Trainer launcher with one trainer template; console RL launch, tracker links, curriculum and failure cluster views, failure to task drafts.
3. Train an open 7B class VLM on ~1,000 tasks across ~50 worlds.

Exit: clear gain on held out tasks and worlds; clean human audit sample; trainer GPUs above 90% busy; replay divergence measured; an RL run launched from the console with no manual steps.

### Phase 4, weeks 15 to 26: breadth, fidelity, scale

1. World and task factory at volume, more app clones, clone fidelity measured against real apps, real SaaS tenants for eval.
2. macOS, Android and GPU pools as demand justifies.
3. 5,000 to 10,000 concurrent envs, chaos tests, multi tenant quotas and billing if this is a product.
4. Console: tenants, budgets with approvals, billing views, browser task authoring.

## 11. Team

1. Virtualization: 2 engineers (KVM, Firecracker, QEMU, snapshot store).
2. Control plane and orchestration: 1.
3. Windows and app automation: 1.
4. RL and inference (gateway, trainer adapters, launcher, sizing): 1.
5. World factory and app integrations: 1.
6. Web console: 1 full stack engineer from phase 0, a second from phase 2.
7. Task authors and QA: 2 to 4 from week 3, since tasks and verifiers are the long pole.
8. Phase 2 additions: an identity and licensing specialist; a security engineer before any external tenant.

## 12. Risks

1. **Microsoft licensing blocks Windows or Office at scale.** Counsel in week 1, the Windows Server Datacenter route, LibreOffice or OnlyOffice for training, a licensed Excel eval slice, license included cloud as a stopgap.
2. **Inference cost outruns budget.** Little's law sizing per run, cache friendly prompts, smaller policies early, admission control.
3. **Reward hacking and verifier false negatives.** Tiers, gates, side effect checks, the red team suite, audits, failure clustering feeding verifier fixes.
4. **Clone fidelity gap, so training doesn't transfer.** Fidelity metric plus a real app eval slice, early for the first vertical.
5. **Frontier provider terms.** Some providers restrict using outputs to train competing models, which can cover distillation data and judge scores; check terms per provider before offering either.
6. **Snapshot compatibility churn** on VMM or hardware changes. Pinned CPU models and machine types, automated rebuild waves.
7. **Windows operational burden.** Dedicated owner, aggressive hardening, hypervisor level IO.
8. **Real SaaS terms of service and account bans.** Clones for training, disposable tenants for eval, vendor partnerships.
9. **Sandbox layer commoditization.** Invest in worlds, identity and verifiers; rent Linux sandboxes when cheaper.
10. **Storage growth.** Lifecycle rules, thumbnails, sampling.
11. **Scope creep across OSes and apps.** The app integration contract plus a ranked app list per vertical.
12. **The console drifts into a second source of truth.** Specs only, immutable running runs, parity tests against the SDK and CLI.
13. **Live viewing load.** Sampled thumbnails, streams opened only while someone watches, limits per session.

## 13. Alternatives considered and rejected

1. **Copy on write disks plus warm pools as the reset mechanism:** Windows boot time dominates; memory fork wins.
2. **Windows 11 with VDA rights at scale:** per user licensing plus vTPM cloning; Windows Server Datacenter instead.
3. **Hyper V for the training pool:** automatic activation but no public memory fork.
4. **Each VM as a Kubernetes pod:** scheduling latency and etcd churn at thousands of short lived VMs.
5. **Real SaaS for training:** state outside the snapshot, account bans, terms of service.
6. **Live web for training:** nonstationary rewards.
7. **Generic OpenAI compatible endpoints for on policy RL:** no token IDs or reliable logprobs.
8. **LLM judge as the primary reward:** cost at scale, noise, injection risk.
9. **Rebuilding from spec for reproducibility:** installers and mirrors drift.
10. **Sliding window screenshot history:** breaks prefix caching.
11. **Separate apps for configuration and review:** context switching plus two models of a run; one console instead.
12. **Configuration stored only in the UI database:** unreproducible runs; specs stay the source of truth.
13. **Building our own training curve tracker:** duplicates Weights & Biases and MLflow; link to them instead.

## 14. Open questions

1. External product or internal platform? The answer sets multi tenancy, billing and security scope.
2. First vertical: office and finance workflows, browser and SaaS, or developer tools?
3. What does counsel say about Office for non human users?
4. Policy size and GPU budget for the first RL run.
5. Hosted custom weights, or BYO endpoints only?
6. Real SaaS tenants for training, or eval only?
7. How much demand is there for macOS and mobile?
8. Which experiment tracker and trainer launcher backend do teams already use?

## Appendix A: interface sketch

```python
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Lease:
    lease_id: str
    episode_id: str
    snapshot_digest: str
    expires_at_unix: float


@dataclass
class Observation:
    screenshot_uri: str | None
    accessibility_tree: dict[str, Any] | None
    text_outputs: dict[str, str] = field(default_factory=dict)


@dataclass
class Action:
    modality: str  # computer, browser, terminal, native or control
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    observation: Observation
    is_terminal: bool
    env_error: str | None
    settle_ms: int


@dataclass
class RewardRecord:
    total: float
    components: dict[str, float]
    termination: str  # success, failure, max_steps, agent_error or env_error
    provenance: dict[str, str]  # verifier versions, judge model, rubric version, state hash


@dataclass
class PreflightReport:
    is_valid: bool
    errors: list[str]
    warnings: list[str]
    estimated_concurrent_envs: int
    estimated_hosts_by_pool: dict[str, int]
    estimated_policy_gpus: int
    estimated_judge_tokens: int
    estimated_cost_usd: float
    estimated_wall_clock_hours: float


class EnvironmentService(Protocol):
    def lease(self, task_id: str, world_seed: int, group_size: int) -> list[Lease]: ...

    def reset(self, lease_id: str) -> Observation: ...

    def step(self, lease_id: str, action: Action) -> StepResult: ...

    def fork(self, lease_id: str, count: int) -> list[Lease]: ...

    def checkpoint(self, lease_id: str) -> str: ...

    # freezes input, extracts state, runs verifiers then the judge, all before teardown
    def finish(self, lease_id: str) -> RewardRecord: ...

    def close(self, lease_id: str) -> None: ...


class ExperimentService(Protocol):
    """Everything the web console does goes through this API, so the SDK and CLI can do it too."""

    # returns an immutable spec digest
    def save_spec(self, spec_yaml: str) -> str: ...

    def preflight(self, spec_digest: str) -> PreflightReport: ...

    # runs one episode end to end and returns its episode id for live viewing
    def dry_run(self, spec_digest: str) -> str: ...

    # returns one run id per sweep point
    def launch(self, spec_digest: str, budget_usd: float) -> list[str]: ...

    # operational knobs only: pause, resume, stop, set_concurrency, set_priority, extend_budget
    def control(self, run_id: str, command: str, value: float | None = None) -> None: ...

    def stream_events(self, run_id: str) -> Iterator[dict[str, Any]]: ...


class AppIntegration(Protocol):
    """Unit of work for adding an app: write world state, read it back normalized, reset it."""

    def materialize(self, world: dict[str, Any], lease_id: str) -> None: ...

    def extract(self, lease_id: str) -> dict[str, Any]: ...

    def reset(self, lease_id: str) -> None: ...


class Verifier(Protocol):
    def verify(
        self,
        task: dict[str, Any],
        initial_state: dict[str, Any],
        final_state: dict[str, Any],
    ) -> dict[str, float]: ...
```
