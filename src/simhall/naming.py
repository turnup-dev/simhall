# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Names shared by the generator, the emulators and the technician bot.

Cable labels (`CBL-<rack>-<node>-<port>`), vendor port names (`swp21` / `Ethernet21`), the display
form (`leaf4/21`, `node7 mlx5_3`) and the deterministic MAC scheme all live here so that every
package derives them the same way.
"""

from __future__ import annotations

import re
from typing import Literal

SwitchVendor = Literal["nvidia", "arista"]

_LABEL_RE = re.compile(r"CBL-(?P<rack>[A-Za-z0-9]+)-(?P<node>\d+)-(?P<port>\d+)")
_PORT_NUMBER_RE = re.compile(r"(\d+)(?:/\d+)?$")
_NODE_IFACE_RE = re.compile(r"^mlx5_(\d+)$")


def cable_label(rack: str, node_index: int, port_index: int) -> str:
    """`CBL-12-07-03` for rack 12, node 7 (1-based, within the rack), port 3 (0-based, `mlx5_3`)."""
    return f"CBL-{rack}-{node_index:02d}-{port_index:02d}"


def parse_cable_label(label: str) -> tuple[str, int, int] | None:
    """Inverse of `cable_label`: `(rack, node_index, port_index)` or None when it is not a label."""
    m = _LABEL_RE.search(label)
    if not m:
        return None
    return m.group("rack"), int(m.group("node")), int(m.group("port"))


def find_cable_labels(text: str) -> list[str]:
    """All cable labels mentioned in free text, in order of appearance."""
    return [m.group(0) for m in _LABEL_RE.finditer(text)]


def node_interface(port_index: int) -> str:
    """Fabric interface name on a node: `mlx5_<p>`."""
    return f"mlx5_{port_index}"


def node_interface_index(name: str) -> int | None:
    """`mlx5_3` -> 3; None for anything else."""
    m = _NODE_IFACE_RE.match(name)
    return int(m.group(1)) if m else None


def leaf_port_name(vendor: SwitchVendor, number: int) -> str:
    """Vendor interface name for leaf port `number` (1-based): `swp21` or `Ethernet21`."""
    if vendor == "arista":
        return f"Ethernet{number}"
    return f"swp{number}"


def port_number(ifname: str) -> int | None:
    """Port number of a switch interface name: `swp21` -> 21, `Ethernet21/1` -> 21, `mlx5_3` -> None."""
    if ifname.startswith("mlx5_"):
        return None
    m = _PORT_NUMBER_RE.search(ifname.split("/")[0])
    return int(m.group(1)) if m else None


def display(device: str, ifname: str) -> str:
    """Display form: switch ports `leaf4/21` (`Ethernet21/1` -> `leaf4/21/1`); nodes `node7 mlx5_3`."""
    if ifname.startswith("mlx5_"):
        return f"{device} {ifname}"
    head, *rest = ifname.split("/")
    m = _PORT_NUMBER_RE.search(head)
    if not m:
        return f"{device} {ifname}"
    tail = "/".join([m.group(1), *rest])
    return f"{device}/{tail}"


def node_mac(global_index: int, port_index: int) -> str:
    """Deterministic NIC MAC: `b8:3f:d2:00:07:03` for node 7 port 3; port 0 doubles as the chassis id."""
    hi, lo = (global_index >> 8) & 0xFF, global_index & 0xFF
    return f"b8:3f:d2:{hi:02x}:{lo:02x}:{port_index & 0xFF:02x}"


def leaf_mac(number: int) -> str:
    """Deterministic, locally administered switch chassis MAC."""
    hi, lo = (number >> 8) & 0xFF, number & 0xFF
    return f"02:1e:af:{hi:02x}:{lo:02x}:00"


def leaf_port_mac(number: int, port: int) -> str:
    """Deterministic MAC of one switch port (used as the leaf-side LLDP port id in `mac` mode)."""
    hi, lo = (number >> 8) & 0xFF, number & 0xFF
    return f"02:1e:af:{hi:02x}:{lo:02x}:{port & 0xFF:02x}"
