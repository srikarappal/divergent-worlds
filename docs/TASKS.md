# Build plan

Tracks the build of `docs/DESIGN.md`, one task at a time. Each task lands with unit tests and its own commit.

## Build decisions

1. Monorepo layout: `python/` holds the `rlworld` package (specs, SDK, rewards, judge, gates, curriculum, preflight, gateway, trajectories, worlds, experiment and reward services); `fleet/` holds the Go services (lease manager, scheduler, env data path, node agent, VMM drivers); `console/` holds the TypeScript and React web console; `schemas/` holds the JSON Schema shared by all three; `examples/` holds sample specs.
2. The control plane API is served by two services behind one gateway: `fleetd` in Go for the hot path (leases, scheduling, env steps, node agents) and `experimentd` in Python for specs, preflight, sweeps, runs, the launcher and the reward pipeline. Specs cross the boundary as validated JSON plus a digest.
3. Python code is synchronous; concurrency comes from threads or from the Go services.
4. The development Mac has no KVM, so VMM drivers are unit tested against fakes; real Firecracker and QEMU runs happen on Linux hosts. A Docker backend (task 29) gives a local end to end loop.

## Tasks

Status is one of todo, doing or done.

### Python core

1. [done] Spec models: experiment, image, world, task and run specs; YAML load and dump; cross layer validation; canonical digests; sweep expansion; JSON Schema export.
2. [done] Actions and adapters: canonical action and observation models; adapters for a frontier computer use tool format, OpenAI style function calls and an open model text format; coordinate scaling and key normalization with golden tests.
3. [done] Reward core: verifier protocol, tier 1 and tier 2 check helpers, world diff and side effect checks, reward composition, termination labels, reward records with provenance.
4. [done] Judge: rubric model, prompt builder that fences untrusted screen text, structured verdict parsing, cache keyed by final state hash, ensembles for low confidence.
5. [done] Task gates: null action, oracle, alternative solution, near miss, rerun stability and solvability gates with a gate report.
6. [done] Curriculum: pass rate tracking per template, difficulty bucket and policy version; learnable band sampling; zero advantage group filtering.
7. [done] Estimator and preflight: Little's law capacity and cost model; validation, endpoint probes and trap warnings producing a preflight report.
8. [done] Model gateway: endpoint registry, OpenAI compatible client, retries with jitter, circuit breaker, token bucket rate limits, response cache, metering, pinned versions, token in token out records.
9. [done] Trajectories: step and episode records, filesystem store behind an object store interface, exact replay, SFT and preference pair export.
10. [done] Failure clustering: labeler protocol, cluster rollups per template and policy version, verifier false negative routing.
11. [done] Worlds: seeded synthetic organization generator, app integration contract, reference mail integration, world digests.
12. [done] Identity and entitlements broker: identity and seat pools, per episode session minting, leases with TTL, audit log.
13. [done] Network world renderers: DNS overrides, private CA issuance, egress allowlist rules, netem fault commands.

### Go fleet

14. [done] Fleet scaffold and lease manager: leases with TTL and heartbeats, reaper, priority classes with reserved capacity, tenant quotas, endpoint concurrency budgets derived from rate limits.
15. [done] Scheduler: placement by snapshot locality, capacity, OS pool, seats and quota.
16. [done] VMM drivers: driver interface, fake driver, Firecracker API client, QMP client, clone network plan.
17. [done] Node agent: restore, fork, pause, resume, checkpoint and destroy; settle detection; turn based stepping.
18. [done] fleetd env API: lease, reset, step, fork, checkpoint, finish and close over HTTP.

### Python services and SDK

19. [done] Env client SDK and MCP facade.
20. [done] Reward service: the finish pipeline (freeze, extract, verify, judge, compose, record).
21. [done] experimentd: specs, experiments, sweeps, preflight, dry run, launch, control and event stream; trainer launcher with Kubernetes and Slurm renderers; run tokens.
22. [done] Rollout worker library: episode loop, group forks, adapters, gateway calls, trajectory writes; trainer adapter shims.

### Web console

23. [done] Console scaffold: API client, spec editor with YAML view and schema validation, catalogs; experimentd catalog and endpoint registry routes; the SDK and CLI held to the console's operations by parity tests.
24. [done] Console preflight, launch and live: preflight panel, run dashboard over server sent events, live wall.
25. [todo] Console review: trajectory scrubber, cohort view, comparisons, reward provenance, one click actions.
26. [todo] Console queues and admin: labeling, review queues, roles, audit log.

### Guests and images

27. [todo] Linux guest agent: screenshots, input, accessibility tree, settle hashing, vsock server.
28. [todo] Image pipeline: Dockerfile to ext4 rootfs builder, Windows Packer template and hardening scripts.
29. [todo] Docker dev backend: local Linux GUI envs behind the VMM driver interface for an end to end loop on a laptop.

## Follow ups

1. Native transports for frontier computer use APIs behind the gateway Transport protocol; the OpenAI compatible transport ships first.
2. App integrations for crm, calendar, chat and browser profiles against the v1 clones; mail and files ship first as references.
