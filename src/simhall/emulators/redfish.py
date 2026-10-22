# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""DMTF Redfish resource tree for one node, in the shapes CONTRACT.md section 3.1 lists.

Paths are device-relative (`/redfish/v1/...`), exactly like a real BMC at `https://<ip>`; the
server mounts the tree under `/dev/<node>` so `base_url + @odata.id` resolves in both worlds.
Rendering is a pure function of the node's state: no clocks, no randomness.
"""

from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING, Any

from simhall.models import HallSpec, NodeIntent, NodeState

if TYPE_CHECKING:
    from simhall.hall import NodePortView

ROOT = "/redfish/v1"

FIRMWARE_NAMES: dict[str, tuple[str, str]] = {
    # component key -> (inventory Id, Name). Names are chosen so the CONTRACT 3.1 substring matcher
    # maps each to exactly one component: "BMC" -> bmc, "BIOS" -> bios, "ConnectX"/"NIC" -> nic.
    "bmc": ("BMC", "BMC Firmware"),
    "bios": ("BIOS", "System BIOS"),
    "nic": ("NIC", "ConnectX-8 NIC Firmware"),
    "cx_firmware": ("CX", "ConnectX Firmware"),
    "gpu_vbios": ("GPU_VBIOS", "GPU VBIOS"),
}


def _collection(path: str, type_name: str, name: str, members: list[str]) -> dict[str, Any]:
    return {
        "@odata.id": path,
        "@odata.type": f"#{type_name}Collection.{type_name}Collection",
        "Name": name,
        "Members@odata.count": len(members),
        "Members": [{"@odata.id": m} for m in members],
    }


def _status(health: str, state: str = "Enabled") -> dict[str, str]:
    return {"State": state, "Health": health, "HealthRollup": health}


def _cpu_model(node_model: str) -> str:
    upper = node_model.upper()
    if "GB300" in upper or "GB200" in upper or "NVL72" in upper:
        return "NVIDIA Grace CPU"
    if "MI3" in upper:
        return "AMD EPYC"
    return "x86-64 CPU"


def render_tree(
    spec: HallSpec, nd: NodeIntent, ns: NodeState, ports: list[NodePortView]
) -> dict[str, dict[str, Any]]:
    """Resource path -> JSON for every resource the node's BMC serves."""
    sys_id, mgr_id, ch_id = spec.redfish_system_id, spec.redfish_manager_id, spec.redfish_chassis_id
    sys_path = f"{ROOT}/Systems/{sys_id}"
    mgr_path = f"{ROOT}/Managers/{mgr_id}"
    ch_path = f"{ROOT}/Chassis/{ch_id}"
    power = "On" if (ns.power == "On" and ns.pdu_on) else "Off"
    sys_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{nd.name}.{ns.serial}.simhall"))
    tree: dict[str, dict[str, Any]] = {}

    tree[ROOT] = {
        "@odata.id": f"{ROOT}/",
        "@odata.type": "#ServiceRoot.v1_16_0.ServiceRoot",
        "Id": "RootService",
        "Name": "Root Service",
        "RedfishVersion": "1.18.0",
        "UUID": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{nd.name}.bmc.simhall")),
        "Vendor": nd.manufacturer,
        "Product": nd.model,
        "Systems": {"@odata.id": f"{ROOT}/Systems"},
        "Chassis": {"@odata.id": f"{ROOT}/Chassis"},
        "Managers": {"@odata.id": f"{ROOT}/Managers"},
        "UpdateService": {"@odata.id": f"{ROOT}/UpdateService"},
        "SessionService": {"@odata.id": f"{ROOT}/SessionService"},
        "Links": {"Sessions": {"@odata.id": f"{ROOT}/SessionService/Sessions"}},
    }

    # --- Systems ---------------------------------------------------------------------------------
    tree[f"{ROOT}/Systems"] = _collection(
        f"{ROOT}/Systems", "ComputerSystem", "Computer System Collection", [sys_path]
    )
    tree[sys_path] = {
        "@odata.id": sys_path,
        "@odata.type": "#ComputerSystem.v1_22_0.ComputerSystem",
        "Id": sys_id,
        "Name": "Computer System",
        "SystemType": "Physical",
        "Manufacturer": nd.manufacturer,
        "Model": nd.model,
        "SerialNumber": ns.serial,
        "PartNumber": "",
        "UUID": sys_uuid,
        "HostName": nd.name,
        "PowerState": power,
        "Status": _status(ns.health, "Enabled" if power == "On" else "StandbyOffline"),
        "BiosVersion": ns.firmware.get("bios", "unknown"),
        "ProcessorSummary": {"Count": 2, "Model": _cpu_model(nd.model), "Status": _status("OK")},
        "MemorySummary": {"TotalSystemMemoryGiB": 960, "Status": _status("OK")},
        "Boot": {"BootSourceOverrideEnabled": "Disabled", "BootSourceOverrideTarget": "None"},
        "LogServices": {"@odata.id": f"{sys_path}/LogServices"},
        "Links": {"Chassis": [{"@odata.id": ch_path}], "ManagedBy": [{"@odata.id": mgr_path}]},
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": f"{sys_path}/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues": [
                    "On",
                    "ForceOff",
                    "GracefulShutdown",
                    "GracefulRestart",
                    "ForceRestart",
                    "PowerCycle",
                ],
            }
        },
    }
    log_path = f"{sys_path}/LogServices"
    evt_path = f"{log_path}/EventLog"
    entries_path = f"{evt_path}/Entries"
    tree[log_path] = _collection(log_path, "LogService", "Log Service Collection", [evt_path])
    tree[evt_path] = {
        "@odata.id": evt_path,
        "@odata.type": "#LogService.v1_4_0.LogService",
        "Id": "EventLog",
        "Name": "System Event Log",
        "LogEntryType": "Event",
        "OverWritePolicy": "WrapsWhenFull",
        "ServiceEnabled": True,
        "Status": _status("OK"),
        "Entries": {"@odata.id": entries_path},
        "Actions": {"#LogService.ClearLog": {"target": f"{evt_path}/Actions/LogService.ClearLog"}},
    }
    entry_paths: list[str] = []
    entry_members: list[dict[str, Any]] = []
    for i, e in enumerate(ns.log, start=1):
        p = f"{entries_path}/{i}"
        member = {
            "@odata.id": p,
            "@odata.type": "#LogEntry.v1_16_0.LogEntry",
            "Id": str(i),
            "Name": f"Log Entry {i}",
            "EntryType": "Event",
            "Severity": e.severity,
            "Created": e.created,
            "Message": e.message,
            "MessageId": e.message_id,
            "MessageArgs": [],
        }
        tree[p] = member
        entry_paths.append(p)
        entry_members.append(member)
    tree[entries_path] = {
        "@odata.id": entries_path,
        "@odata.type": "#LogEntryCollection.LogEntryCollection",
        "Name": "System Event Log Entries",
        "Members@odata.count": len(entry_members),
        "Members": entry_members,
    }

    # --- Managers --------------------------------------------------------------------------------
    tree[f"{ROOT}/Managers"] = _collection(f"{ROOT}/Managers", "Manager", "Manager Collection", [mgr_path])
    tree[mgr_path] = {
        "@odata.id": mgr_path,
        "@odata.type": "#Manager.v1_19_0.Manager",
        "Id": mgr_id,
        "Name": "Manager",
        "ManagerType": "BMC",
        "Manufacturer": nd.manufacturer,
        "Model": "Baseboard Management Controller",
        "FirmwareVersion": ns.firmware.get("bmc", "unknown"),
        "UUID": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{nd.name}.manager.simhall")),
        "Status": _status("OK"),
        "PowerState": "On",
        "Links": {
            "ManagerForServers": [{"@odata.id": sys_path}],
            "ManagerForChassis": [{"@odata.id": ch_path}],
        },
    }

    # --- UpdateService ---------------------------------------------------------------------------
    upd_path = f"{ROOT}/UpdateService"
    inv_path = f"{upd_path}/FirmwareInventory"
    tree[upd_path] = {
        "@odata.id": upd_path,
        "@odata.type": "#UpdateService.v1_13_0.UpdateService",
        "Id": "UpdateService",
        "Name": "Update Service",
        "ServiceEnabled": True,
        "Status": _status("OK"),
        "FirmwareInventory": {"@odata.id": inv_path},
        "Actions": {
            "#UpdateService.SimpleUpdate": {"target": f"{upd_path}/Actions/UpdateService.SimpleUpdate"}
        },
    }
    if not ns.partial_redfish:
        members: list[str] = []
        for comp, version in ns.firmware.items():
            inv_id, name = FIRMWARE_NAMES.get(comp, (comp.upper(), f"{comp.upper()} Firmware"))
            p = f"{inv_path}/{inv_id}"
            tree[p] = {
                "@odata.id": p,
                "@odata.type": "#SoftwareInventory.v1_10_0.SoftwareInventory",
                "Id": inv_id,
                "Name": name,
                "Version": version,
                "Updateable": True,
                "SoftwareId": f"{inv_id}-{nd.manufacturer}".upper(),
                "Manufacturer": nd.manufacturer,
                "Status": _status("OK"),
            }
            members.append(p)
        tree[inv_path] = _collection(inv_path, "SoftwareInventory", "Firmware Inventory Collection", members)

    # --- Chassis ---------------------------------------------------------------------------------
    chassis_health = "Critical" if ns.leak else "OK"
    na_path = f"{ch_path}/NetworkAdapters"
    ts_path = f"{ch_path}/ThermalSubsystem"
    tree[f"{ROOT}/Chassis"] = _collection(f"{ROOT}/Chassis", "Chassis", "Chassis Collection", [ch_path])
    tree[ch_path] = {
        "@odata.id": ch_path,
        "@odata.type": "#Chassis.v1_25_0.Chassis",
        "Id": ch_id,
        "Name": "Compute Tray",
        "ChassisType": "RackMount",
        "Manufacturer": nd.manufacturer,
        "Model": nd.model,
        "SerialNumber": ns.serial,
        "PowerState": power,
        "Status": _status(chassis_health),
        "Location": {
            "Placement": {"Rack": nd.rack, "RackOffset": nd.position, "RackOffsetUnits": "EIA_310"},
            "PartLocation": {"LocationType": "Slot", "LocationOrdinalValue": nd.position},
        },
        "NetworkAdapters": {"@odata.id": na_path},
        "ThermalSubsystem": {"@odata.id": ts_path},
        "Links": {"ComputerSystems": [{"@odata.id": sys_path}], "ManagedBy": [{"@odata.id": mgr_path}]},
    }

    adapters: list[str] = []
    n_adapters = max(1, math.ceil(len(ports) / 2))
    for i in range(n_adapters):
        a_path = f"{na_path}/NetworkAdapter_{i}"
        p_coll = f"{a_path}/Ports"
        adapter_ports = ports[2 * i : 2 * i + 2]
        port_paths: list[str] = []
        for pv in adapter_ports:
            p_path = f"{p_coll}/{pv.iface}"
            has_peer = pv.peer_system is not None
            tree[p_path] = {
                "@odata.id": p_path,
                "@odata.type": "#Port.v1_11_0.Port",
                "Id": pv.iface,
                "Name": pv.iface,
                "PortId": pv.iface,
                "PortType": "BidirectionalPort",
                "PortProtocol": "Ethernet",
                "LinkNetworkTechnology": "Ethernet",
                "LinkState": "Enabled",
                "LinkStatus": pv.link_status,
                "CurrentSpeedGbps": pv.speed_gbps,
                "MaxSpeedGbps": spec.speed_gbps,
                "Status": _status("OK" if pv.link_status == "LinkUp" else "Warning"),
                "Ethernet": {
                    "AssociatedMACAddresses": [pv.mac],
                    "LLDPEnabled": True,
                    "LLDPTransmit": {"ChassisId": nd.chassis_mac, "PortId": pv.iface, "SystemName": nd.name},
                    "LLDPReceive": {
                        "ChassisId": pv.peer_chassis if has_peer else None,
                        "ChassisIdSubtype": "MacAddr" if has_peer else None,
                        "PortId": pv.peer_port if has_peer else None,
                        "PortIdSubtype": "IfName" if has_peer else None,
                        "SystemName": pv.peer_system if has_peer else None,
                    },
                },
            }
            port_paths.append(p_path)
        tree[p_coll] = _collection(p_coll, "Port", "Port Collection", port_paths)
        tree[a_path] = {
            "@odata.id": a_path,
            "@odata.type": "#NetworkAdapter.v1_11_0.NetworkAdapter",
            "Id": f"NetworkAdapter_{i}",
            "Name": f"ConnectX-8 Adapter {i}",
            "Manufacturer": "NVIDIA",
            "Model": "ConnectX-8",
            "SerialNumber": f"{ns.serial}-NIC{i}",
            "Status": _status("OK"),
            "Controllers": [{"FirmwarePackageVersion": ns.firmware.get("nic", "unknown")}],
            "Ports": {"@odata.id": p_coll},
        }
        adapters.append(a_path)
    tree[na_path] = _collection(na_path, "NetworkAdapter", "Network Adapter Collection", adapters)

    leak_path = f"{ts_path}/LeakDetection"
    metrics_path = f"{ts_path}/ThermalMetrics"
    tree[ts_path] = {
        "@odata.id": ts_path,
        "@odata.type": "#ThermalSubsystem.v1_3_0.ThermalSubsystem",
        "Id": "ThermalSubsystem",
        "Name": "Thermal Subsystem",
        "Status": _status(chassis_health),
        "LeakDetection": {"@odata.id": leak_path},
        "ThermalMetrics": {"@odata.id": metrics_path},
    }
    detector_state = "Critical" if ns.leak else "OK"
    tree[leak_path] = {
        "@odata.id": leak_path,
        "@odata.type": "#LeakDetection.v1_1_0.LeakDetection",
        "Id": "LeakDetection",
        "Name": "Leak Detection",
        "Status": _status(detector_state),
        "LeakDetectorGroups": [
            {
                "GroupName": "Tray",
                "HumidityPercent": {"Reading": 35.0},
                "Detectors": [
                    {
                        "DataSourceUri": f"{leak_path}/LeakDetectors/LeakDetector_0",
                        "DeviceName": "Tray leak detector",
                        "DetectorState": detector_state,
                    },
                    {
                        "DataSourceUri": f"{leak_path}/LeakDetectors/LeakDetector_1",
                        "DeviceName": "Manifold leak detector",
                        "DetectorState": detector_state,
                    },
                ],
            }
        ],
    }
    inlet = float(ns.inlet_c)
    tree[metrics_path] = {
        "@odata.id": metrics_path,
        "@odata.type": "#ThermalMetrics.v1_3_0.ThermalMetrics",
        "Id": "ThermalMetrics",
        "Name": "Thermal Metrics",
        "TemperatureReadingsCelsius": [
            {"DeviceName": "Inlet Air", "Reading": inlet, "DataSourceUri": f"{ch_path}/Sensors/InletAirTemp"},
            {
                "DeviceName": "Exhaust Air",
                "Reading": round(inlet + 14.0, 1),
                "DataSourceUri": f"{ch_path}/Sensors/ExhaustAirTemp",
            },
            {
                "DeviceName": "Coolant Supply",
                "Reading": 30.0,
                "DataSourceUri": f"{ch_path}/Sensors/CoolantSupplyTemp",
            },
            {
                "DeviceName": "Coolant Return",
                "Reading": 42.0,
                "DataSourceUri": f"{ch_path}/Sensors/CoolantReturnTemp",
            },
        ],
        "TemperatureSummaryCelsius": {
            "Ambient": {"Reading": inlet},
            "Exhaust": {"Reading": round(inlet + 14.0, 1)},
        },
    }

    # --- SessionService (minimal) ---------------------------------------------------------------
    ss_path = f"{ROOT}/SessionService"
    tree[ss_path] = {
        "@odata.id": ss_path,
        "@odata.type": "#SessionService.v1_1_8.SessionService",
        "Id": "SessionService",
        "Name": "Session Service",
        "ServiceEnabled": True,
        "SessionTimeout": 1800,
        "Sessions": {"@odata.id": f"{ss_path}/Sessions"},
    }
    tree[f"{ss_path}/Sessions"] = _collection(f"{ss_path}/Sessions", "Session", "Session Collection", [])
    return tree


def normalize_path(path: str) -> str:
    """`redfish/v1/Systems/` -> `/redfish/v1/Systems`; the root keeps the form `/redfish/v1`."""
    p = "/" + path.strip("/")
    return p


def error_body(code: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "@Message.ExtendedInfo": [{"MessageId": code, "Message": message}],
        }
    }
