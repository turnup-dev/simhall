# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Switch-side LLDP and port state in the two public JSON transports of CONTRACT.md section 3.2.

Arista eAPI (`runCmds` JSON-RPC: `show lldp neighbors detail`, `show interfaces status`) and NVIDIA
NVUE REST (`GET /nvue_v1/interface?rev=applied`). Both render from the same `LeafPortView` list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from simhall import naming
from simhall.models import LeafIntent

if TYPE_CHECKING:
    from simhall.hall import LeafPortView

SYSTEM_DESCRIPTION = "Linux 6.8.0 x86_64, lldpd 1.0.18"


def render_eapi(lf: LeafIntent, views: list[LeafPortView]) -> dict[str, Any]:
    """`{"lldpNeighbors": result[0] body, "interfaceStatuses": result[1] body}` for an Arista leaf."""
    neighbors: dict[str, Any] = {}
    statuses: dict[str, Any] = {}
    for v in views:
        statuses[v.port] = {
            "linkStatus": "connected" if v.link_up else "notconnect",
            "bandwidth": v.speed_gbps * 10**9 if (v.link_up and v.speed_gbps) else 0,
            "description": "",
            "interfaceType": "400GBASE-DR4" if v.label else "Not Present",
            "autoNegotiateActive": False,
            "duplex": "duplexFull",
            "lineProtocolStatus": "up" if v.link_up else "notPresent",
            "vlanInformation": {"interfaceMode": "routed", "interfaceForwardingModel": "routed"},
        }
        if v.neighbor_system is not None:
            neighbors[v.port] = {
                "lldpNeighborInfo": [
                    {
                        "chassisIdType": "macAddress",
                        "chassisId": v.neighbor_chassis,
                        "systemName": v.neighbor_system,
                        "systemDescription": SYSTEM_DESCRIPTION,
                        "neighborInterfaceInfo": {
                            "interfaceIdType": "macAddress" if v.neighbor_port_is_mac else "interfaceName",
                            "interfaceId": v.neighbor_port_id,
                            "interfaceDescription": v.neighbor_iface or "",
                        },
                        "ttl": 120,
                        "lastChangeTime": 1760630400.0,
                        "lastContactTime": 1760630430.0,
                        "systemCapabilities": {"bridge": False, "router": False, "stationOnly": True},
                        "managementAddresses": [],
                    }
                ]
            }
    return {"lldpNeighbors": neighbors, "interfaceStatuses": statuses}


def eapi_response(body: Any, lf: LeafIntent, table: dict[str, Any]) -> dict[str, Any]:
    """Answer one eAPI JSON-RPC request from the rendered table; unknown commands get an eAPI-style error."""
    if not isinstance(body, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    req_id = body.get("id")
    if body.get("method") != "runCmds":
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
    params = body.get("params") or {}
    cmds = params.get("cmds") or []
    results: list[dict[str, Any]] = []
    neighbor_count = len(table["lldpNeighbors"])
    for i, raw in enumerate(cmds):
        cmd = raw.get("cmd", "") if isinstance(raw, dict) else str(raw)
        norm = " ".join(cmd.split()).lower()
        if norm == "show lldp neighbors detail":
            results.append(
                {
                    "tablesLastChangeTime": 1760630400.0,
                    "tablesAgeOuts": 0,
                    "tablesInserts": neighbor_count,
                    "lldpNeighbors": table["lldpNeighbors"],
                    "tablesDeletes": 0,
                    "tablesDrops": 0,
                }
            )
        elif norm == "show interfaces status":
            results.append({"interfaceStatuses": table["interfaceStatuses"]})
        elif norm == "show lldp neighbors":
            brief = [
                {
                    "port": port,
                    "neighborDevice": info["lldpNeighborInfo"][0]["systemName"],
                    "neighborPort": info["lldpNeighborInfo"][0]["neighborInterfaceInfo"]["interfaceId"],
                    "ttl": 120,
                }
                for port, info in table["lldpNeighbors"].items()
            ]
            results.append(
                {
                    "tablesLastChangeTime": 1760630400.0,
                    "tablesAgeOuts": 0,
                    "tablesInserts": neighbor_count,
                    "lldpNeighbors": brief,
                    "tablesDeletes": 0,
                    "tablesDrops": 0,
                }
            )
        elif norm == "show version":
            results.append(
                {
                    "modelName": f"DCS-{lf.model}",
                    "internalVersion": "4.33.2F",
                    "version": "4.33.2F",
                    "serialNumber": lf.serial,
                    "systemMacAddress": lf.chassis_mac,
                    "architecture": "x86_64",
                    "hardwareRevision": "11.00",
                    "mfgName": "Arista",
                }
            )
        elif norm == "show hostname":
            results.append({"hostname": lf.name, "fqdn": lf.name})
        elif norm in ("enable", "terminal length 0"):
            results.append({})
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": 1002,
                    "message": f"CLI command {i + 1} of {len(cmds)} '{cmd}' failed: invalid command",
                    "data": [*results, {"errors": ["Invalid input"]}],
                },
            }
    return {"jsonrpc": "2.0", "id": req_id, "result": results}


def render_nvue(lf: LeafIntent, views: list[LeafPortView], nominal_gbps: int = 400) -> dict[str, Any]:
    """NVUE `GET /nvue_v1/interface?rev=applied` body for an NVIDIA leaf: `{"swp21": {...}, ...}`."""
    out: dict[str, Any] = {}
    for v in views:
        speed = v.speed_gbps if (v.link_up and v.speed_gbps) else nominal_gbps
        entry: dict[str, Any] = {
            "type": "swp",
            "link": {
                "state": {"up": {}} if v.link_up else {"down": {}},
                "speed": f"{speed}G",
                "mtu": 9216,
                "auto-negotiate": "off",
                "duplex": "full",
                "fec": "rs",
                "mac": naming.leaf_port_mac(lf.number, v.number),
            },
            "ifindex": v.number + 2,
        }
        if v.neighbor_system is not None:
            entry["lldp"] = {
                "neighbor": {
                    v.neighbor_system: {
                        "chassis": {
                            "chassis-id": v.neighbor_chassis,
                            "chassis-id-type": "mac",
                            "system-name": v.neighbor_system,
                            "system-description": SYSTEM_DESCRIPTION,
                        },
                        "port": {
                            "id": v.neighbor_port_id,
                            "id-type": "mac" if v.neighbor_port_is_mac else "if_name",
                            "description": v.neighbor_iface or "",
                            "ttl": 120,
                        },
                        "age": "0:00:30",
                    }
                }
            }
        out[v.port] = entry
    return out


def nvue_system(lf: LeafIntent) -> dict[str, Any]:
    """`GET /nvue_v1/system` body."""
    return {
        "hostname": lf.name,
        "build": "Cumulus Linux 5.12.0",
        "platform": {"manufacturer": lf.manufacturer, "model": lf.model, "serial-number": lf.serial},
        "uptime": "2 days, 4:11:09",
        "timezone": "Etc/UTC",
    }
