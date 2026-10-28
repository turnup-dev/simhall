# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The golden scenario `hall-b` (CONTRACT.md section 8): the demo run, reproducibly.

Site `hall-b`, rack `12`, 18 nodes with 16 ports each, two NVIDIA leaves `leaf3`/`leaf4`, 288 cables.
The cabling rule would put `node7 mlx5_3` on `leaf4/swp50`; the golden cabling moves it to
`leaf4/swp21`, moves the cable that was there (`node3 mlx5_9`) to `swp50`, and parks the cable that
was on `swp22` (`node3 mlx5_11`) on the first spare port so that `leaf4/swp22` is free.

Five faults of four kinds are injected, each on its own node, so a hall "handed over with every light
green" shows what a check finds on day 0 (`turnup-check` prints five findings, exit 1):

* `wrong_port(CBL-12-07-03, leaf4, swp22)`: node7 mlx5_3 one port off (`flt-0001`, the demo's cable);
* `link_degraded(CBL-12-11-05, 200)`: node11 mlx5_5 trains at 200G instead of 400G;
* `firmware_off_baseline(node4, bmc, 1.09.2)`: node4's BMC below the 1.12.0 baseline;
* `gpu_unhealthy(node14, 2, xid=79)`: node14 GPU 2 reports XID 79;
* `unreachable_node(node17)`: node17's BMC and exporter time out.

Seating the two cables (`seat_cable`) and `update_firmware` retire the first three; the GPU and the
unreachable node stay open until the tray is replaced (`replace_node`), the way a real hall would.
"""

from __future__ import annotations

from simhall import naming
from simhall.faults import (
    Fault,
    firmware_off_baseline,
    gpu_unhealthy,
    link_degraded,
    unreachable_node,
    wrong_port,
)
from simhall.hall import Hall
from simhall.models import CableSpec, HallSpec

GOLDEN_NAMES: tuple[str, ...] = ("hall-b",)

GOLDEN_CABLE = "CBL-12-07-03"
GOLDEN_NODE = "node7"
GOLDEN_IFACE = "mlx5_3"
GOLDEN_LEAF = "leaf4"
GOLDEN_PORT_NUMBER = 21
GOLDEN_WRONG_PORT_NUMBER = 22
GOLDEN_PORT = naming.leaf_port_name("nvidia", GOLDEN_PORT_NUMBER)  # swp21
GOLDEN_WRONG_PORT = naming.leaf_port_name("nvidia", GOLDEN_WRONG_PORT_NUMBER)  # swp22

# The other four faults (one node each; none shares a node with the wrong port).
GOLDEN_DEGRADED_CABLE = "CBL-12-11-05"  # node11 mlx5_5 -> leaf4/swp83
GOLDEN_DEGRADED_SPEED = 200
GOLDEN_FIRMWARE_NODE, GOLDEN_FIRMWARE_COMPONENT, GOLDEN_FIRMWARE_VERSION = "node4", "bmc", "1.09.2"
GOLDEN_GPU_NODE, GOLDEN_GPU_INDEX, GOLDEN_GPU_XID = "node14", 2, 79
GOLDEN_UNREACHABLE_NODE = "node17"
GOLDEN_FAULT_KINDS: tuple[str, ...] = (
    "wrong_port",
    "link_degraded",
    "firmware_off_baseline",
    "gpu_unhealthy",
    "unreachable_node",
)


def golden_spec(name: str = "hall-b", base_url: str | None = None) -> HallSpec:
    if name not in GOLDEN_NAMES:
        raise KeyError(f"unknown golden scenario {name!r}; known: {', '.join(GOLDEN_NAMES)}")
    spec = HallSpec(
        site="hall-b",
        racks=1,
        nodes_per_rack=18,
        ports_per_node=16,
        leaves_per_rack=2,
        node_name_fmt="node{n}",
        leaf_name_fmt="leaf{n}",
        leaf_start=3,
        rack_start=12,
        switch_vendor="nvidia",
        lldp_port_id_mode="name",
        node_model="GB300 NVL72 compute tray",
        seed=1,
    )
    if base_url:
        spec = spec.model_copy(update={"base_url": base_url})
    return spec


def golden_cables(spec: HallSpec | None = None) -> list[CableSpec]:
    """Default cabling with the explicit override that frees `leaf4/swp22` and lands node7 mlx5_3 on swp21.

    Port names follow the spec's switch vendor (`swp21` or `Ethernet21`), so an Arista variant of the
    golden hall is one `model_copy(update={"switch_vendor": "arista"})` away.
    """
    spec = spec or golden_spec()
    good_port = naming.leaf_port_name(spec.switch_vendor, GOLDEN_PORT_NUMBER)
    wrong = naming.leaf_port_name(spec.switch_vendor, GOLDEN_WRONG_PORT_NUMBER)
    plain = Hall.generate(spec)
    cables = [c.model_copy(deep=True) for c in plain.intent_cables.values()]
    by_leaf_port = {(c.b.device, c.b.interface): c for c in cables}
    target = next(c for c in cables if c.a.device == GOLDEN_NODE and c.a.interface == GOLDEN_IFACE)
    displaced_21 = by_leaf_port.get((GOLDEN_LEAF, good_port))
    displaced_22 = by_leaf_port.get((GOLDEN_LEAF, wrong))
    old_port = target.b.interface
    target.b.device, target.b.interface = GOLDEN_LEAF, good_port
    if displaced_21 is not None and displaced_21 is not target:
        displaced_21.b.interface = old_port
    if displaced_22 is not None and displaced_22 is not target:
        used = {c.b.interface for c in cables if c.b.device == GOLDEN_LEAF and c is not displaced_22}
        spare = next(p for p in plain.leaves[GOLDEN_LEAF].ports if p not in used and p != wrong)
        displaced_22.b.interface = spare
    return cables


def golden_faults(spec: HallSpec | None = None) -> list[Fault]:
    """The five start-up faults, wrong_port first (it is `flt-0001`, the cable the demo moves)."""
    vendor = (spec or golden_spec()).switch_vendor
    return [
        wrong_port(GOLDEN_CABLE, GOLDEN_LEAF, naming.leaf_port_name(vendor, GOLDEN_WRONG_PORT_NUMBER)),
        link_degraded(GOLDEN_DEGRADED_CABLE, GOLDEN_DEGRADED_SPEED),
        firmware_off_baseline(GOLDEN_FIRMWARE_NODE, GOLDEN_FIRMWARE_COMPONENT, GOLDEN_FIRMWARE_VERSION),
        gpu_unhealthy(GOLDEN_GPU_NODE, GOLDEN_GPU_INDEX, GOLDEN_GPU_XID),
        unreachable_node(GOLDEN_UNREACHABLE_NODE),
    ]


def build_golden(name: str = "hall-b", base_url: str | None = None, with_faults: bool = True) -> Hall:
    """A fresh golden hall; `with_faults=False` gives the same hall built exactly to intent."""
    spec = golden_spec(name, base_url)
    hall = Hall.generate(spec, cables=golden_cables(spec), golden=name)
    if with_faults:
        for f in golden_faults(spec):
            hall.inject(f)
    return hall
