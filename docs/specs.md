# Spec files and the golden scenario

## Golden scenario `hall-b`

`simhall serve --golden hall-b` builds site `hall-b`, rack `12`, `node1..node18` (GB300 NVL72 compute
trays, 4 GPUs, `mlx5_0..mlx5_15`), NVIDIA leaves `leaf3` and `leaf4` (NVUE), 288 cables. The cabling
rule would put `node7 mlx5_3` on `leaf4/swp50`; the golden override moves it to `leaf4/swp21`, moves
`node3 mlx5_9` (which was on swp21) to swp50 and parks `node3 mlx5_11` (which was on swp22) on the
first spare port, swp145, so `leaf4/swp22` is free.

Five faults of four kinds are injected at start-up and on every reset, each on its own node, so the hall
reads as "handed over with every light green" until a check runs (`turnup-check` prints five findings
and exits 1):

| # | Fault | What a check finds | Closed by |
|---|---|---|---|
| `flt-0001` | `wrong_port(CBL-12-07-03, leaf4, swp22)` | `wrong_port`: node7 mlx5_3 one port off, fix `move cable one port up` | `seat_cable` on swp21 (the demo's cable) |
| `flt-0002` | `link_degraded(CBL-12-11-05, 200)` | `link_degraded`: node11 mlx5_5 at 200G on leaf4/swp83, intent 400G | `seat_cable` (reseat; the transceiver swap) |
| `flt-0003` | `firmware_off_baseline(node4, bmc, 1.09.2)` | `firmware_off_baseline`: node4 BMC below the 1.12.0 baseline | `update_firmware` |
| `flt-0004` | `gpu_unhealthy(node14, 2, xid=79)` | `gpu_unhealthy`: node14 GPU 2, XID 79 | `replace_node` (an RMA) |
| `flt-0005` | `unreachable_node(node17)` | `unreachable_node`: node17's BMC (and exporter) time out | `replace_node` |

`simhall golden-spec hall-b` exports all of this as a spec file; `build_golden(with_faults=False)` is the
same hall built exactly to intent.

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
