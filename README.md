# rl-world

rl-world manufactures, runs, scores and replays worlds for training and evaluating computer use agents. It launches
Linux and Windows microVMs from content addressed specs, seeds them with coherent synthetic companies (mail, files,
chat, identities), lets any policy endpoint drive them and grades every episode with verifiers plus judge endpoints.

[`docs/DESIGN.md`](docs/DESIGN.md) is the design and [`docs/TASKS.md`](docs/TASKS.md) tracks how it was built. The code
calls itself `rlworld`: the Python package, the Go module, the CLIs and the metrics all use that name.

## Layout

| Path | What lives there |
| --- | --- |
| `fleet/` | Go, standard library only. `fleetd` serves the env API: leases, admission, scheduling, the node agent, the Firecracker, QEMU and Docker drivers, snapshots, warm pools and operator takeover. `egressproxy` enforces an env's egress allowlist. |
| `python/` | The `rlworld` package: specs and preflight, `experimentd` (experiments, runs, rewards, review), the SDK and the `rlworld` CLI, model gateway adapters, world and task generators, verifiers, the Linux guest agent, the Windows in session helper and the image builders. |
| `console/` | The web console, React on Vite. |
| `schemas/` | Shared contracts the Python, Go and TypeScript code is tested against: the spec JSON schema, the API operations and the metrics catalog. |
| `examples/` | Specs, laptop configs for fleetd and experimentd, model endpoints and a judge rubric. |
| `observability/` | The Prometheus scrape config, Grafana provisioning and the platform health dashboard. |
| `docs/` | `DESIGN.md` and `TASKS.md`. |

## What to install

For development (tests, the CLI, the console):

* [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.12 and every Python
  dependency itself.
* [Go](https://go.dev/dl/) 1.25.5 or newer.
* [Node.js](https://nodejs.org/) 22.12 or newer, with npm.

To run episodes on a laptop:

* Docker. The dev backend runs Linux envs as containers, so no KVM is needed.
* A policy behind an OpenAI compatible API, such as [vLLM](https://docs.vllm.ai/).

For VM hosts, Linux with KVM:

* iproute2, iptables and a filesystem with reflinks (XFS or btrfs) for disk overlays.
* Firecracker and its jailer for the Linux pool.
* QEMU 9 or newer (`qemu-system-x86_64`, `qemu-img`) for the Windows pool.
* For image builds: Docker with buildx and e2fsprogs (`mke2fs`) for Linux; Packer and bsdtar for Windows.
* Optional: mitmproxy (`mitmdump`) to record and replay web traffic; Prometheus and Grafana for dashboards.

## Set up

From the repo root:

```bash
(cd python && uv sync)
(cd fleet && go build ./...)
(cd console && npm install)
```

## Run the tests

```bash
(cd python && uv run pytest)
(cd fleet && go vet ./... && go test -race ./...)
(cd console && npm run typecheck && npm test)
```

None of them needs Docker, KVM or a model: VMs, containers and endpoints are faked.

## Run an episode on a laptop

fleetd's Docker driver serves the Linux pool as containers (build decision 4 in `docs/TASKS.md`), so the whole loop
runs on one machine: fleetd on port 8700, experimentd on 8080 and the console on 5173.

### 1. Build the dev image of the reference task

[`examples/specs/reconcile_invoices_linux.yaml`](examples/specs/reconcile_invoices_linux.yaml) is the reference task:
reconcile a month of invoices in LibreOffice Calc, then mail a summary. Its image pins a placeholder base digest, so
pin the real one first:

```bash
docker buildx imagetools inspect ubuntu:24.04    # put its Digest after ubuntu_noble@ in image.base
```

Then write a build config and an artifact lock in `python/`. Apt sources must be dated snapshot mirrors; pick a date
whose archive still holds the package versions the spec pins.

```yaml
# python/build.yaml
linux:
  base_images: {ubuntu_noble: docker.io/library/ubuntu}
  apt_sources:
    - deb http://snapshot.ubuntu.com/ubuntu/YYYYMMDDT000000Z noble main universe
    - deb http://snapshot.ubuntu.com/ubuntu/YYYYMMDDT000000Z noble-updates main universe
  guest_agent_wheel: dist/rlworld-0.1.0-py3-none-any.whl
  guest_agent_requirements: dist/guest.txt
```

```yaml
# python/lock.yaml: the reference image downloads nothing outside apt
artifacts: {}
```

Build the guest agent and the image:

```bash
cd python
uv build --wheel
uv export --extra guest --no-dev --no-emit-project --output-file dist/guest.txt
uv run rlworld-build-image ../examples/specs/reconcile_invoices_linux.yaml \
    --config build.yaml --lock lock.yaml --work-dir /tmp/rlworld/build --target dev
```

`/tmp/rlworld/build/manifest.json` names the image's `image_digest` and `image_tag`. Put them under
`static_snapshots` in [`examples/experimentd/laptop.yaml`](examples/experimentd/laptop.yaml): the digest as the key and
as `digest`, the tag as `base_disk_path`. Docker builds the image for linux/amd64, which Apple Silicon runs under
emulation.

### 2. Start the services

```bash
# terminal 1: fleetd
cd fleet && go run ./cmd/fleetd -config ../examples/fleetd/laptop.json

# terminal 2: experimentd, whose secrets come from the environment
cd python
export RLWORLD_RUN_TOKEN_SECRET=$(openssl rand -hex 32) RLWORLD_JUDGE_SECRET=$(openssl rand -hex 32)
uv run rlworld-experimentd --config ../examples/experimentd/laptop.yaml

# terminal 3: the console at http://localhost:5173
cd console && npm run dev
```

The reference spec plays the model `my_policy` at `http://localhost:8000/v1`, as
[`examples/experimentd/endpoints.yaml`](examples/experimentd/endpoints.yaml) registers it. Serve one there, for example
`vllm serve <model> --served-model-name my_policy --port 8000`, or point both files at your own endpoint.

### 3. Launch

```bash
cd python
uv run rlworld specs save ../examples/specs/reconcile_invoices_linux.yaml    # prints the spec's digest
uv run rlworld specs preflight <digest>
uv run rlworld specs dry-run <digest>                                           # one episode end to end
uv run rlworld specs launch <digest> --budget-usd 5
uv run rlworld runs list
```

The console shows the same runs with their episodes, frames and live takeover. `uv run rlworld --help` lists every
command. `RLWORLD_API_URL` points the CLI and the console at another experimentd; `RLWORLD_FLEET_URL` points the
console at another fleetd.

## Dashboards

[`observability/prometheus/prometheus.yml`](observability/prometheus/prometheus.yml) scrapes `/metrics` on hosts named
`fleetd` and `experimentd`; on a laptop change the targets to `127.0.0.1:8700` and `127.0.0.1:8080`. Grafana takes
`observability/grafana/provisioning` as its provisioning directory and loads the dashboards in
`observability/grafana/dashboards`, mounted at `/var/lib/grafana/dashboards/rlworld`.

## Windows pool

Windows hosts run fleetd with `"driver": "qemu_templated"` and a `qemu` section naming `qemu_path`, `run_dir`,
`machine_type` and `cpu_model`. Screens, input and the clock stay at the hypervisor. The in session helper,
`rlworld-windows-helper` from the `windows` extra, serves UIA trees, Excel through COM, injection and extraction over a
virtio serial port. Windows images come from `rlworld-build-image --target vm` with a `windows` build config and Packer.
