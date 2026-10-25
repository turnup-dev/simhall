# simhall

A virtual GPU hall for testing bring-up tooling without hardware. simhall emulates the devices
[`turnup-check`](https://github.com/turnup-dev/turnup-check) and Turnup Acceptance talk to - Redfish
BMCs, LLDP tables on Arista (eAPI) and NVIDIA (NVUE) leaves, DCGM exporters, and a minimal NetBox API -
from one ground-truth model with fault injection and technician actions. Apache-2.0.

```bash
# Terminal 1: the golden hall from the demo (hall-b, rack 12, 288 cables, one wrong port)
simhall serve --golden hall-b --port 8900

# Terminal 2: the open-source check against it (no token needed; simhall accepts any)
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox
reading intent      288 links · 18 nodes · 2 leaves
polling             lldp · redfish · dcgm
✓ 287 / 288 links match intent
✓ 18 / 18 nodes reachable, firmware baseline ok
✗ mismatch   node7 mlx5_3 → leaf4/22
             expected leaf4/21
fix          move cable one port up
elapsed      0.4 s

report       hall-b-<YYYY-MM-DD>.json
share stats  --telemetry opt-in (racks, links)

# A technician seats the cable (or the fault is cleared), and the same command exits 0 with 288 / 288.
simhall act '{"kind":"seat_cable","label":"CBL-12-07-03","b_interface":"swp21"}'
```

The virtual-hall path in three commands (no token needed; simhall accepts any):

```bash
pip install simhall turnup-check
simhall serve --golden hall-b &                  # 127.0.0.1:8900
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox
```

Everything in this package is synthetic: there are no real hostnames, IPs, serial numbers or
topologies, and the intent it emits carries `simulated: true` so it never counts toward the public
counters.

## Install

```bash
pip install simhall                                     # Python 3.12+
pip install git+https://github.com/turnup-dev/simhall   # or straight from this repository
```

From a checkout, activate your venv first:

```bash
python3 -m venv .venv && source .venv/bin/activate   # or activate an existing venv
pip install -e '.[dev]'                              # the package plus pytest, hypothesis and ruff
simhall --help
```

Dependencies: fastapi, uvicorn, pydantic v2, httpx, pyyaml, click. `pynetbox` is only needed for
`simhall seed-netbox` against a real NetBox (`pip install -e '.[netbox]'`) and is imported lazily.

## What it serves

`simhall serve` is one FastAPI server on `127.0.0.1` (port 8900 by default). In Python,
`create_app(hall)` serves that very `Hall` object (in-process actions are visible over HTTP) and
`ServerThread(app)` runs it on a free port for tests. Devices are distinguished
by path prefix:

| Path | Protocol | Shape |
|---|---|---|
| `/dev/<node>/redfish/v1/...` | DMTF Redfish (GET, any basic auth) | service root, `Systems` (follow `Members`), `Managers`, `UpdateService/FirmwareInventory`, `Chassis/<id>/NetworkAdapters/<id>/Ports/<ifname>` with `Ethernet.LLDPReceive`, `ThermalSubsystem/LeakDetection`, `ThermalSubsystem/ThermalMetrics`, `Systems/<id>/LogServices/EventLog/Entries` |
| `/dev/<node>/metrics` | DCGM-exporter Prometheus text | eleven `DCGM_FI_DEV_*` families, labels `gpu`, `UUID`, `modelName`, `Hostname`, `device`, `pci_bus_id` |
| `POST /dev/<leaf>/command-api` | Arista eAPI JSON-RPC (`runCmds`) | `show lldp neighbors detail`, `show interfaces status`, `show lldp neighbors`, `show version`, `show hostname` |
| `/dev/<leaf>/nvue_v1/interface?rev=applied` | NVIDIA NVUE REST | `{"swp21": {"type": "swp", "link": {...}, "lldp": {"neighbor": {...}}}}`; also `/nvue_v1/interface/<name>` and `/nvue_v1/system` |
| `/netbox/api/dcim/{sites,racks,devices,interfaces,cables}/` | NetBox REST (any token) | `{count, next, previous, results}`, filters `site=`, `device_id=`, `device=`, `name=`, `rack=`, `role=`, `limit=`, `offset=`; `/netbox/api/status/` for pynetbox |
| `/sim/...` | control plane | `GET /sim/intent`, `GET /sim/state`, `GET /sim/spec`, `GET/POST /sim/faults`, `DELETE /sim/faults/{id}`, `POST /sim/actions`, `POST /sim/reset`, `GET /sim/history`, `GET /sim/health` |

Redfish `@odata.id` values are device-relative (`/redfish/v1/...`), like a real BMC at `https://<ip>`.
A collector that joins `base_url + odata_id` works against both the simulator and real hardware.
Device mgmt URLs in the intent and in the NetBox custom fields (`turnup_bmc_url`, `turnup_dcgm_url`,
`turnup_switch_url`) always point at the host and port the server is actually listening on, so
`NETBOX_URL=http://127.0.0.1:8900/netbox` exercises the real NetBox adapter end to end. Node devices
also carry `config_context.turnup.firmware_baseline`.

Per-device fault modes: `timeout` sleeps `--timeout-sleep` seconds
(default 10, longer than the collector's 3 s), `http_500` answers 500, `refused` answers 404 at the
prefix ("device not registered"), `partial` answers 404 for `FirmwareInventory` only.

## Python API

```python
from simhall import HallSpec, Hall, Fault, Action

spec = HallSpec(site="hall-b", racks=1, nodes_per_rack=18, ports_per_node=16, leaves_per_rack=2,
                node_name_fmt="node{n}", leaf_name_fmt="leaf{n}", leaf_start=3, rack_start=12,
                switch_vendor="nvidia",     # nvidia | arista
                lldp_port_id_mode="name",   # name | mac
                node_model="GB300 NVL72 compute tray", seed=1)
hall = Hall.generate(spec)                  # ground truth: intent + physical state, all correct
hall.intent()                               # turnup.intent/v1 with simulated: true
hall.write_intent("hall-b.yaml")
f = hall.inject(Fault(kind="wrong_port", cable="CBL-12-07-03", to_device="leaf4", to_interface="swp22"))
hall.faults(); hall.clear(f.id)             # clearing restores truth exactly, in any order
hall.act(Action(kind="seat_cable", label="CBL-12-07-03", b_interface="swp21"))
hall.redfish_tree("node7")                  # {"/redfish/v1": {...}, "/redfish/v1/Systems": {...}, ...}
hall.lldp_table("leaf4")                    # NVUE dict, or {"lldpNeighbors", "interfaceStatuses"} for Arista
hall.dcgm_metrics("node7")                  # Prometheus text
hall.state()                                # cabling, power, firmware, GPU health, reachability, faults
```

`Hall.generate(spec, cables=[...])` accepts an explicit cable list (same shape as the intent's
`cables`), which is how the golden hall puts `node7 mlx5_3` on `leaf4/swp21`. Helpers in
`simhall.faults` and `simhall.actions` build records by name: `faults.wrong_port(...)`,
`actions.seat_cable(...)`, and so on.

### Cabling rule

Node `n` (1-based within its rack), port `p` (0-based) connects to leaf `leaf_start + (p % L)` of the
rack, port `(n-1) * ceil(P/L) + (p // L) + 1`, where `P = ports_per_node` and `L = leaves_per_rack`.
Whenever `L` divides `P` the ceiling is exact (18 x 16 ports on 2 leaves: ports 1..144 on each leaf);
the rounding only prevents collisions for specs where it does not. Each leaf has
`spare_ports` (default 16) free ports above the used range. Cable labels are `CBL-<rack>-<node>-<port>`
(`CBL-12-07-03` = rack 12, node 7, `mlx5_3`). Node NIC MACs are `b8:3f:d2:00:<node>:<port>`; port 0
doubles as the LLDP chassis id. In `lldp_port_id_mode="mac"` the NIC advertises its MAC as the port
id and the intent's `mac` fields are what a collector matches on.

### How state works

The *base* physical state is what has physically been done to the hall (initially: everything built
to intent). The *derived* state is the base plus every active fault applied in order. Views render
from the derived state as pure functions (no clocks, no randomness), so:

- `clear(id)` removes the fault and re-derives: the views come back byte for byte, in any order;
- an action mutates the base and retires the faults it physically resolves (field-scoped: seating
  `CBL-12-07-03` retires a `wrong_port` on that cable, not an `unreachable_node` on node7);
- `POST /sim/reset` rebuilds the hall as served at start-up; for the golden hall that includes its
  fault, so a demo can be replayed.

A leaf port holds one cable. Injecting a `wrong_port` onto an occupied port is a 409; so is seating a
cable onto one. A cable with a label that is not in the intent can be seated (that is how
`unexpected_link` becomes testable).

## Faults and what they look like

| Fault | Observable effect | Expected `turnup-check` finding |
|---|---|---|
| `wrong_port(cable, to_device, to_interface)` | neighbor appears on the target port, intended port down with no neighbor; node Redfish port `LinkUp` with `LLDPReceive.PortId` = target | `wrong_port` |
| `missing_link(cable)` | intended port down, no neighbor anywhere; node port `NoLink` | `missing_link` |
| `swap_pair(a, b)` | the two cables' leaf ports are exchanged | 2 x `wrong_port` |
| `link_degraded(cable, 200)` | neighbor correct, speed 200G on both ends | `link_degraded` |
| `link_degraded(cable, "down")` | link down, LLDP neighbor still present (aging entry) | `link_degraded` |
| `unreachable_node(node, mode, scope=all\|redfish\|dcgm)` | Redfish and/or DCGM answer per `mode`; fabric links stay up. Also accepted for a leaf (eAPI/NVUE) | `unreachable_node` (DCGM-only scope: `gpu: unknown`, no finding) |
| `partial_redfish(node)` | `FirmwareInventory` 404, everything else OK | none (firmware `unknown`) |
| `firmware_off_baseline(node, component, version)` | inventory member version changes (`BiosVersion`/`FirmwareVersion` follow) | `firmware_off_baseline` |
| `gpu_unhealthy(node, gpu, xid=79)` | `DCGM_FI_DEV_XID_ERRORS{gpu}` = xid, event log entry | `gpu_unhealthy` |
| `gpu_missing(node, gpu)` | the GPU disappears from `/metrics` | `gpu_count_mismatch` |
| `nvlink_degraded(node)` | `NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` > 100 on every GPU, bandwidth quartered | `nvlink_degraded` |
| `node_health_warning(node)` | `Systems/<id>.Status.Health` = `Warning`, event log entry | `node_health_warning` |
| `coolant_leak(node \| rack)` | `LeakDetection` detectors `Critical`, chassis health Critical; `Systems` health unchanged | none (acceptance reads it) |
| `pdu_outlet_off(node)` | BMC and exporter refused, host off, all of the node's links down | `unreachable_node` + `missing_link` per port |
| `lldp_port_id_mac(leaf)` | that leaf reports NIC port ids as MACs (`id-type: mac` / `macAddress`) | none when MAC matching works |

Faults get ids `flt-0001`, `flt-0002`, ... Validation errors are 400 (unknown kind, missing fields),
unknown objects 404, occupied ports 409; the server never answers 500 for a bad request.

## Actions

`seat_cable(label, a_device?, a_interface?, b_device?, b_interface?)` (ends default to the intent),
`unseat_cable(label)`, `power_on(device)` (also restores a PDU outlet), `power_off(device)` (host off:
BMC still answers with `PowerState: Off`, exporter refused, links down), `replace_node(device,
new_serial)` (fresh healthy GPUs, baseline firmware, node faults retired, cables stay),
`update_firmware(device, component, version)`, `scan_qr(label)` (no state change; returns the label
record). Over HTTP: `POST /sim/actions {"kind": ..., ...}`.

## Golden scenario `hall-b`

`simhall serve --golden hall-b` builds site `hall-b`, rack `12`, `node1..node18` (GB300 NVL72 compute
trays, 4 GPUs, `mlx5_0..mlx5_15`), NVIDIA leaves `leaf3` and `leaf4` (NVUE), 288 cables. The cabling
rule would put `node7 mlx5_3` on `leaf4/swp50`; the golden override moves it to `leaf4/swp21`, moves
`node3 mlx5_9` (which was on swp21) to swp50 and parks `node3 mlx5_11` (which was on swp22) on the
first spare port, swp145, so `leaf4/swp22` is free. One fault is injected:
`wrong_port(CBL-12-07-03, leaf4, swp22)`. `simhall golden-spec hall-b` exports all of this as a spec
file.

## Spec files

```yaml
# spec.yaml
site: hall-a
racks: 2
nodes_per_rack: 9
ports_per_node: 8
leaves_per_rack: 2
switch_vendor: arista
lldp_port_id_mode: mac
cables:                       # optional: replaces the cabling rule
  - {label: CBL-12-01-00, a: {device: node1, interface: mlx5_0}, b: {device: leaf3, interface: Ethernet1}}
faults:                       # optional: injected at start-up and on every reset
  - {kind: missing_link, cable: CBL-12-01-00}
  - {kind: unreachable_node, node: node4, mode: timeout}
```

`simhall serve --spec spec.yaml --port 8901`, `simhall intent --spec spec.yaml -o hall-a.yaml`.
Every `HallSpec` field is allowed at the top level; unknown keys are rejected.

## Technician bot

```bash
simhall technician --acceptance-url http://127.0.0.1:8800 --sim-url http://127.0.0.1:8900 \
                   --worker tech-1 --error-rate 0.05 --speed 20 --seed 1
```

The bot pulls `GET /api/workers/{worker}/next?rack=`, posts `start`, performs the step as an action on
the hall, posts evidence (a QR scan of the cable label for cable steps, a note otherwise) and posts
`done`. With probability `--error-rate` a cable step goes wrong: onto an adjacent free port (chosen so
the verifier's fix reads "move cable one port up/down") or left unseated. `--speed 20` makes a nominal
60 s step take 3 s. It exits after `--idle-polls` consecutive empty polls (HTTP 204) or `--max-steps`.
The cable label is taken from any `cable`/`label` field on the step, else from a `CBL-rr-nn-pp` token in
the title or instructions, else from "node N port P" in the title plus the hall's intent.
`--approver NAME` approves `requires_approval` steps as that named person before starting them.

## Seeding a real NetBox

```bash
simhall seed-netbox --netbox-url http://127.0.0.1:8000 --token $NETBOX_TOKEN --golden hall-b
simhall seed-netbox --netbox-url http://127.0.0.1:8000 --golden hall-b --dry-run   # plan only, no pynetbox
```

Creates (idempotently, by natural key) the three `turnup_*` custom fields, the site, racks,
manufacturers, roles, device types, devices (with custom fields and `local_context_data.turnup`),
interfaces (MACs as `dcim.mac_addresses` objects on NetBox >= 4.2, inline before), and cables with
`a_terminations`/`b_terminations`. It has been exercised against a pynetbox stand-in in the tests, not
against a live NetBox yet.

## Tests

```bash
make install  # pip install -e '.[dev]' into the active venv (else ./.venv)
make test     # pytest -q, about 20 s
make lint     # ruff check + ruff format --check
```

Unit tests cover generation and every emulator shape against the vendor-shaped examples kept under
`tests/fixtures/`; hypothesis property tests draw random specs (1-3 racks, 2-18 nodes, 4-16 ports,
1-4 leaves, both vendors, both LLDP id modes) and random fault sets, and check that the emulated LLDP,
Redfish and DCGM views reflect each fault exactly, that nothing else changes, and that clearing the
faults in random order restores the pristine views; an HTTP test starts the real server on a free
`127.0.0.1` port and fetches one resource of each protocol plus every fault mode; the technician bot
runs against an in-repo stub of the Acceptance routes it uses.

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check` and pytest on Python 3.12 and 3.13
for every push to `main` and every pull request, and checks that the sdist and wheel build.

## Releasing

`.github/workflows/release.yml` publishes to PyPI when a `v*` tag is pushed. It checks that the tag
matches `version` in `pyproject.toml` and `__version__` in `src/simhall/__init__.py`, builds the sdist
and wheel with `python -m build`, and uploads them with `pypa/gh-action-pypi-publish` through PyPI
trusted publishing (OIDC: no API token is stored anywhere). A tag for a version that is already on PyPI
(uploaded by hand with twine) publishes nothing and passes.

Before the first tag, the PyPI trusted publisher must be configured: on pypi.org, Account settings,
Publishing ("Add a new pending publisher" while the project does not exist yet; the project's Settings,
Publishing afterwards) with PyPI project name `simhall`, owner `turnup-dev`, repository name `simhall`,
workflow name `release.yml` and environment name `pypi`. GitHub creates the `pypi` environment on the
first run; add required reviewers to it under Settings, Environments if releases should wait for a click.
Tag only while the repository is public (GitHub Free offers environments on public repositories only).

```bash
# bump the version in pyproject.toml and src/simhall/__init__.py, commit, then:
git tag v0.1.1 && git push origin v0.1.1
```

## Layout

```
src/simhall/
  models.py        HallSpec, CableSpec, physical-state records
  naming.py        labels, port names, display forms, MAC scheme
  hall.py          Hall: generation, faults, actions, views, state
  faults.py        Fault model and constructors
  actions.py       Action model and constructors
  golden.py        hall-b
  specfile.py      spec YAML/JSON
  emulators/       redfish.py, lldp.py (eAPI + NVUE), dcgm.py, netbox.py
  server.py        FastAPI app, ServerThread for tests
  technician.py    the bot
  seed_netbox.py   pynetbox seeding
  views.py         normalizers used by tests and oracles
  cli.py           click commands
```

## License

Apache-2.0. See `LICENSE`.
