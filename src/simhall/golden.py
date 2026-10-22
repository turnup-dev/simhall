# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The golden scenario `hall-b` (CONTRACT.md section 8): the demo run, reproducibly.

Site `hall-b`, rack `12`, 18 nodes with 16 ports each, two NVIDIA leaves `leaf3`/`leaf4`, 288 cables.
The cabling rule would put `node7 mlx5_3` on `leaf4/swp50`; the golden cabling moves it to
`leaf4/swp21`, moves the cable that was there (`node3 mlx5_9`) to `swp50`, and parks the cable that
was on `swp22` (`node3 mlx5_11`) on the first spare port so that `leaf4/swp22` is free. One fault is
injected: `wrong_port(CBL-12-07-03, leaf4, swp22)`.
"""

from __future__ import annotations

from simhall import naming
from simhall.faults import Fault, wrong_port
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
    vendor = (spec or golden_spec()).switch_vendor
    return [wrong_port(GOLDEN_CABLE, GOLDEN_LEAF, naming.leaf_port_name(vendor, GOLDEN_WRONG_PORT_NUMBER))]


def build_golden(name: str = "hall-b", base_url: str | None = None, with_faults: bool = True) -> Hall:
    """A fresh golden hall; `with_faults=False` gives the same hall built exactly to intent."""
    spec = golden_spec(name, base_url)
    hall = Hall.generate(spec, cables=golden_cables(spec), golden=name)
    if with_faults:
        for f in golden_faults(spec):
            hall.inject(f)
    return hall
