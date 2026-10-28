# simhall

A virtual GPU hall for testing bring-up tooling without hardware. One ground-truth model serves the
devices a bring-up tool talks to, with fault injection and technician actions: Redfish BMCs, LLDP tables
on Arista (eAPI) and NVIDIA (NVUE) leaves, DCGM exporters, and a minimal NetBox API. Apache-2.0.

```bash
pip install simhall turnup-check
simhall serve --golden hall-b &                                      # 127.0.0.1:8900, any token
turnup-check --site hall-b --intent http://127.0.0.1:8900/netbox
```

```
reading intent      288 links · 18 nodes · 2 leaves
polling             lldp · redfish · dcgm
✓ 286 / 288 links match intent
✓ 17 / 18 nodes reachable, firmware off baseline
✗ mismatch   node7 mlx5_3 → leaf4/22
             expected leaf4/21
fix          move cable one port up
✗ degraded   node11 mlx5_5 → leaf4/83 200G
             expected leaf4/83 up 400G
fix          check transceivers and cable CBL-12-11-05 at leaf4/83 (200G negotiated, intent 400G)
✗ firmware   node4 bmc 1.09.2
             expected 1.12.0
fix          update bmc on node4 to 1.12.0
✗ gpu        node14 gpu2 XID 79
             expected healthy
fix          drain node14 and open an RMA packet for GPU 2
✗ offline    node17 redfish unreachable: timeout after 3 s
fix          check BMC reachability and credentials for node17
elapsed      3.2 s
```

The golden hall `hall-b` is a hall handed over with every light green: five faults of four kinds, one node
each. A technician moves the cable one port up, reseats the degraded one and applies the firmware update, and
the same command prints `288 / 288 links match intent`, `firmware baseline ok` and the two findings that
stay open (the GPU with the XID, the BMC that does not answer) until the trays are replaced:

```bash
simhall act '{"kind":"seat_cable","label":"CBL-12-07-03","b_interface":"swp21"}'
simhall act '{"kind":"seat_cable","label":"CBL-12-11-05"}'
simhall act '{"kind":"update_firmware","device":"node4","component":"bmc","version":"1.12.0"}'
```

Everything here is synthetic: no real hostnames, IPs, serial numbers or topologies. The intent it emits
carries `simulated: true`, so a simulated run never counts as a real one.

## Why

Bring-up tooling is tested on the hardware it is meant to verify, which is the hardware nobody can spare.
simhall gives a tool like [turnup-check](https://github.com/turnup-dev/turnup-check) a hall that behaves
like the real protocols at every layer, where any fault can be injected, observed through every view at
once, and cleared again byte for byte. The test suite of turnup-check runs thousands of random halls and
fault sets against it in-process.

## Install

```bash
pip install simhall                                     # Python 3.12+
pip install git+https://github.com/turnup-dev/simhall   # or straight from this repository
```

From a checkout: `pip install -e '.[dev]'`, then `simhall --help`. Dependencies: fastapi, uvicorn,
pydantic v2, httpx, pyyaml, click; `pynetbox` only for `simhall seed-netbox` (`.[netbox]`).

## Faults and what a tool should see

| Fault | Observable effect | Expected `turnup-check` finding |
|---|---|---|
| `wrong_port(cable, to_device, to_interface)` | neighbor appears on the target port, intended port down; node Redfish port shows the target | `wrong_port` |
| `missing_link(cable)` | intended port down, no neighbor anywhere; node port `NoLink` | `missing_link` |
| `swap_pair(a, b)` | the two cables' leaf ports are exchanged | 2 x `wrong_port` |
| `link_degraded(cable, 200 \| "down")` | speed 200G on both ends, or link down with an aging LLDP entry | `link_degraded` |
| `unreachable_node(node, timeout \| http_500 \| refused)` | Redfish and/or DCGM answer per mode; links stay up | `unreachable_node` |
| `firmware_off_baseline(node, component, version)` | the inventory member's version changes | `firmware_off_baseline` |
| `gpu_unhealthy`, `gpu_missing`, `nvlink_degraded`, `node_health_warning` | DCGM metrics and Redfish health change accordingly | the matching finding |
| `pdu_outlet_off(node)` | BMC and exporter refused, host off, every link down | `unreachable_node` + `missing_link` per port |
| `coolant_leak`, `partial_redfish`, `lldp_port_id_mac` | leak detectors Critical; `FirmwareInventory` 404; port ids as MACs | none, by design |

Actions (`seat_cable`, `unseat_cable`, `power_on`, `power_off`, `replace_node`, `update_firmware`,
`scan_qr`) change the hall the way a technician would and retire the faults they physically resolve.
Faults are applied on top of the base state and clearing one restores the views exactly, in any order.
The complete tables and the rules are in [docs/faults.md](https://github.com/turnup-dev/simhall/blob/main/docs/faults.md).

## Python API

```python
from simhall import HallSpec, Hall, Fault, Action

hall = Hall.generate(HallSpec(site="hall-b", racks=1, nodes_per_rack=18, ports_per_node=16,
                              leaves_per_rack=2, switch_vendor="nvidia", seed=1))
hall.inject(Fault(kind="wrong_port", cable="CBL-12-07-03", to_device="leaf4", to_interface="swp22"))
hall.lldp_table("leaf4")        # what the leaf would say
hall.redfish_tree("node7")      # what the BMC would say
hall.act(Action(kind="seat_cable", label="CBL-12-07-03", b_interface="swp21"))
```

`create_app(hall)` serves that very object over HTTP, so in-process actions are visible to a tool polling
it. The model, the cabling rule and how state is derived: [docs/model.md](https://github.com/turnup-dev/simhall/blob/main/docs/model.md).

## Documentation

- [docs/protocols.md](https://github.com/turnup-dev/simhall/blob/main/docs/protocols.md): every endpoint served, by protocol, and the per-device fault modes.
- [docs/model.md](https://github.com/turnup-dev/simhall/blob/main/docs/model.md): `HallSpec`, `Hall`, the cabling rule, base and derived state.
- [docs/faults.md](https://github.com/turnup-dev/simhall/blob/main/docs/faults.md): all faults, actions and their HTTP status codes.
- [docs/specs.md](https://github.com/turnup-dev/simhall/blob/main/docs/specs.md): the golden scenario `hall-b` and spec files for your own halls.
- [docs/tools.md](https://github.com/turnup-dev/simhall/blob/main/docs/tools.md): the technician bot and seeding a real NetBox from a hall.
- [docs/development.md](https://github.com/turnup-dev/simhall/blob/main/docs/development.md): tests, CI, releasing, source layout.

## License

Apache-2.0. See `LICENSE`.
