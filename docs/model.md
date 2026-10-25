# The Python API and the hall model

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
