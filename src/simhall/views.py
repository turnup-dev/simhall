# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Normalizers over the emulated vendor views, for tests and oracles.

They parse the *rendered* eAPI / NVUE / Redfish / DCGM output back into plain records, so a test
that uses them exercises the exact shapes the server serves rather than internal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from simhall import naming
from simhall.emulators import dcgm as dcgm_emu
from simhall.hall import Hall


@dataclass(frozen=True)
class PortRecord:
    """One switch port as a collector would normalize it (CONTRACT 3.2 `LldpNeighbor`)."""

    local_port: str
    number: int
    link_up: bool
    speed_gbps: int | None
    remote_system: str | None
    remote_port: str | None
    remote_port_is_mac: bool
    remote_chassis: str | None


def normalize_lldp(hall: Hall, leaf: str) -> dict[int, PortRecord]:
    """Port number -> record, parsed from `hall.lldp_table(leaf)` in whichever vendor shape it has."""
    table = hall.lldp_table(leaf)
    out: dict[int, PortRecord] = {}
    if "interfaceStatuses" in table:  # eAPI
        for port, status in table["interfaceStatuses"].items():
            number = naming.port_number(port)
            if number is None:
                continue
            info = table["lldpNeighbors"].get(port, {}).get("lldpNeighborInfo", [])
            n = info[0] if info else None
            bw = status.get("bandwidth") or 0
            out[number] = PortRecord(
                local_port=port,
                number=number,
                link_up=status.get("linkStatus") == "connected",
                speed_gbps=bw // 10**9 if bw else None,
                remote_system=n["systemName"] if n else None,
                remote_port=n["neighborInterfaceInfo"]["interfaceId"] if n else None,
                remote_port_is_mac=bool(n and n["neighborInterfaceInfo"]["interfaceIdType"] == "macAddress"),
                remote_chassis=n["chassisId"] if n else None,
            )
        return out
    for port, entry in table.items():  # NVUE
        if entry.get("type") != "swp":
            continue
        number = naming.port_number(port)
        if number is None:
            continue
        link = entry.get("link", {})
        up = "up" in link.get("state", {})
        speed_text = str(link.get("speed", "")).rstrip("G")
        neighbors = entry.get("lldp", {}).get("neighbor", {})
        name, n = next(iter(neighbors.items()), (None, None))
        out[number] = PortRecord(
            local_port=port,
            number=number,
            link_up=up,
            speed_gbps=int(speed_text) if (up and speed_text.isdigit()) else None,
            remote_system=n["chassis"]["system-name"] if n else name,
            remote_port=n["port"]["id"] if n else None,
            remote_port_is_mac=bool(n and n["port"]["id-type"] == "mac"),
            remote_chassis=n["chassis"]["chassis-id"] if n else None,
        )
    return out


def redfish_ports(hall: Hall, node: str) -> dict[str, dict[str, Any]]:
    """Interface name -> the Redfish `Ports/<id>` resource, found by walking the chassis tree."""
    tree = hall.redfish_tree(node)
    root = tree["/redfish/v1"]
    chassis_coll = tree[root["Chassis"]["@odata.id"]]
    out: dict[str, dict[str, Any]] = {}
    for member in chassis_coll["Members"]:
        chassis = tree[member["@odata.id"]]
        adapters = tree[chassis["NetworkAdapters"]["@odata.id"]]
        for a in adapters["Members"]:
            adapter = tree[a["@odata.id"]]
            ports = tree[adapter["Ports"]["@odata.id"]]
            for p in ports["Members"]:
                port = tree[p["@odata.id"]]
                out[port["Id"]] = port
    return out


def redfish_system(hall: Hall, node: str) -> dict[str, Any]:
    tree = hall.redfish_tree(node)
    systems = tree[tree["/redfish/v1"]["Systems"]["@odata.id"]]
    return tree[systems["Members"][0]["@odata.id"]]


def redfish_firmware(hall: Hall, node: str) -> dict[str, str] | None:
    """Inventory `Name -> Version`, or None when the inventory is missing (partial Redfish)."""
    tree = hall.redfish_tree(node)
    upd = tree[tree["/redfish/v1"]["UpdateService"]["@odata.id"]]
    inv = tree.get(upd["FirmwareInventory"]["@odata.id"])
    if inv is None:
        return None
    return {tree[m["@odata.id"]]["Name"]: tree[m["@odata.id"]]["Version"] for m in inv["Members"]}


def redfish_leak_states(hall: Hall, node: str) -> list[str]:
    tree = hall.redfish_tree(node)
    chassis = tree[tree["/redfish/v1/Chassis"]["Members"][0]["@odata.id"]]
    ts = tree[chassis["ThermalSubsystem"]["@odata.id"]]
    leak = tree[ts["LeakDetection"]["@odata.id"]]
    return [d["DetectorState"] for g in leak["LeakDetectorGroups"] for d in g["Detectors"]]


def dcgm_table(hall: Hall, node: str) -> dict[str, dict[str, float]]:
    return dcgm_emu.parse(hall.dcgm_metrics(node))
