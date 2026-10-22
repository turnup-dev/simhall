# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Emulator shapes against the CONTRACT.md section 3 examples (kept verbatim under tests/fixtures/)."""

from __future__ import annotations

import re

from simhall.emulators import dcgm as dcgm_emu
from simhall.emulators import lldp as lldp_emu
from simhall.emulators import redfish as redfish_emu
from simhall.faults import Fault
from simhall.golden import golden_cables, golden_spec
from simhall.hall import Hall
from simhall.views import redfish_firmware, redfish_leak_states, redfish_ports, redfish_system

from .conftest import FIXED_TIME, load_fixture, subset

# CONTRACT 3.1 component matcher, reproduced here to prove the inventory names are unambiguous.
MATCHER = (
    ("gpu_vbios", ("VBIOS", "GPU")),
    ("bmc", ("BMC", "iDRAC", "iLO", "XCC", "Baseboard Management")),
    ("bios", ("BIOS", "UEFI")),
    ("nic", ("ConnectX", "BlueField", "NIC", "Network Adapter")),
)


def _components_for(name: str) -> set[str]:
    return {comp for comp, needles in MATCHER if any(n.lower() in name.lower() for n in needles)}


# ------------------------------------------------------------------------------------------------
# Redfish
# ------------------------------------------------------------------------------------------------


def test_redfish_tree_is_self_consistent(clean_golden: Hall) -> None:
    tree = clean_golden.redfish_tree("node7")
    root = tree["/redfish/v1"]
    assert root["RedfishVersion"] == "1.18.0"
    for link in ("Systems", "Chassis", "Managers", "UpdateService"):
        assert root[link]["@odata.id"] in tree
    for key, resource in tree.items():
        assert resource["@odata.id"].rstrip("/") == key, key
        for member in resource.get("Members", []):
            assert member["@odata.id"] in tree, f"{key} -> {member['@odata.id']}"
        if "Members" in resource:
            assert resource["Members@odata.count"] == len(resource["Members"])


def test_redfish_system_manager_and_inventory(clean_golden: Hall) -> None:
    system = redfish_system(clean_golden, "node7")
    assert system["Status"]["Health"] == "OK" and system["Status"]["State"] == "Enabled"
    assert system["PowerState"] == "On" and system["HostName"] == "node7"
    assert system["Model"] == "GB300 NVL72 compute tray" and system["Manufacturer"] == "NVIDIA"
    assert system["SerialNumber"] == clean_golden.nodes["node7"].serial
    assert system["BiosVersion"] == "2.3.1" and system["ProcessorSummary"]["Count"] == 2
    tree = clean_golden.redfish_tree("node7")
    manager = tree[tree["/redfish/v1/Managers"]["Members"][0]["@odata.id"]]
    assert manager["FirmwareVersion"] == "1.12.0" and manager["Status"]["Health"] == "OK"
    inventory = redfish_firmware(clean_golden, "node7")
    assert inventory == {
        "BMC Firmware": "1.12.0",
        "System BIOS": "2.3.1",
        "ConnectX-8 NIC Firmware": "28.43.1014",
    }
    seen = {}
    for name in inventory:
        comps = _components_for(name)
        assert len(comps) == 1, f"{name} matches {comps}"
        seen[comps.pop()] = name
    assert set(seen) == {"bmc", "bios", "nic"}
    member = tree["/redfish/v1/UpdateService/FirmwareInventory/BMC"]
    assert {
        "Id": "BMC",
        "Name": "BMC Firmware",
        "Version": "1.12.0",
        "Updateable": True,
    }.items() <= member.items()


def test_redfish_gpu_firmware_is_opt_in() -> None:
    spec = golden_spec().model_copy(
        update={"redfish_gpu_firmware": True, "nodes_per_rack": 1, "ports_per_node": 2}
    )
    hall = Hall.generate(spec)
    inventory = redfish_firmware(hall, "node1")
    assert inventory is not None and "GPU VBIOS" in inventory


def test_redfish_ports_match_contract_example(clean_golden: Hall) -> None:
    ports = redfish_ports(clean_golden, "node7")
    assert len(ports) == 16
    subset(load_fixture("redfish_port.json"), ports["mlx5_3"])
    rx = ports["mlx5_3"]["Ethernet"]["LLDPReceive"]
    assert rx["ChassisId"] == clean_golden.leaves["leaf4"].chassis_mac and rx["SystemName"] == "leaf4"
    assert ports["mlx5_3"]["LinkNetworkTechnology"] == "Ethernet"


def test_redfish_thermal_and_logs(clean_golden: Hall) -> None:
    assert redfish_leak_states(clean_golden, "node7") == ["OK", "OK"]
    tree = clean_golden.redfish_tree("node7")
    chassis = tree["/redfish/v1/Chassis/Chassis.Embedded.1"]
    metrics = tree[tree[chassis["ThermalSubsystem"]["@odata.id"]]["ThermalMetrics"]["@odata.id"]]
    inlets = [r for r in metrics["TemperatureReadingsCelsius"] if "Inlet" in r["DeviceName"]]
    assert len(inlets) == 1 and inlets[0]["Reading"] == 24.0
    entries = tree["/redfish/v1/Systems/System.Embedded.1/LogServices/EventLog/Entries"]
    assert entries["Members@odata.count"] == 3
    for e in entries["Members"]:
        assert set(e) >= {"Created", "Severity", "Message"}
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", e["Created"])


def test_redfish_fault_views(clean_golden: Hall) -> None:
    hall = clean_golden
    hall.inject(Fault(kind="node_health_warning", node="node1"))
    hall.inject(Fault(kind="coolant_leak", node="node2"))
    hall.inject(Fault(kind="firmware_off_baseline", node="node3", component="bios", version="2.2.9"))
    hall.inject(Fault(kind="partial_redfish", node="node4"))
    hall.inject(Fault(kind="pdu_outlet_off", node="node5"))
    assert redfish_system(hall, "node1")["Status"]["Health"] == "Warning"
    assert redfish_system(hall, "node2")["Status"]["Health"] == "OK"  # leak stays a chassis-level signal
    assert redfish_leak_states(hall, "node2") == ["Critical", "Critical"]
    assert (
        redfish_firmware(hall, "node3")["System BIOS"]
        == "2.2.9"
        == redfish_system(hall, "node3")["BiosVersion"]
    )
    assert redfish_firmware(hall, "node4") is None
    assert "/redfish/v1/UpdateService" in hall.redfish_tree("node4")
    assert (
        hall.reach("node5", "redfish") == "refused" and redfish_system(hall, "node5")["PowerState"] == "Off"
    )
    assert all(p["LinkStatus"] == "LinkDown" for p in redfish_ports(hall, "node5").values())
    assert redfish_emu.normalize_path("redfish/v1/Systems/") == "/redfish/v1/Systems"


# ------------------------------------------------------------------------------------------------
# LLDP: eAPI and NVUE
# ------------------------------------------------------------------------------------------------


def test_eapi_shapes_match_contract_example(arista_golden: Hall) -> None:
    table = arista_golden.lldp_table("leaf4")
    response = lldp_emu.eapi_response(load_fixture("eapi_request.json"), arista_golden.leaves["leaf4"], table)
    assert response["jsonrpc"] == "2.0" and response["id"] == "turnup" and len(response["result"]) == 2
    subset(load_fixture("eapi_lldp_detail.json"), response["result"][0])
    subset(load_fixture("eapi_interfaces_status.json"), response["result"][1])
    statuses = response["result"][1]["interfaceStatuses"]
    assert statuses["Ethernet22"]["linkStatus"] == "notconnect" and statuses["Ethernet22"]["bandwidth"] == 0
    assert "Ethernet22" not in response["result"][0]["lldpNeighbors"]
    assert len(statuses) == 160


def test_eapi_mac_mode_and_errors(arista_golden: Hall) -> None:
    arista_golden.inject(Fault(kind="lldp_port_id_mac", leaf="leaf4"))
    table = arista_golden.lldp_table("leaf4")
    subset(load_fixture("eapi_lldp_detail_mac.json"), {"lldpNeighbors": table["lldpNeighbors"]})
    leaf = arista_golden.leaves["leaf4"]
    bad = lldp_emu.eapi_response(
        {"jsonrpc": "2.0", "method": "runCmds", "params": {"cmds": ["show foo"]}, "id": 1}, leaf, table
    )
    assert bad["error"]["code"] == 1002 and "invalid command" in bad["error"]["message"]
    assert lldp_emu.eapi_response(None, leaf, table)["error"]["code"] == -32700
    assert lldp_emu.eapi_response({"method": "nope"}, leaf, table)["error"]["code"] == -32601
    version = lldp_emu.eapi_response(
        {"method": "runCmds", "params": {"cmds": ["show version"]}, "id": 2}, leaf, table
    )
    assert version["result"][0]["serialNumber"] == leaf.serial


def test_nvue_shape_matches_contract_example(clean_golden: Hall) -> None:
    table = clean_golden.lldp_table("leaf4")
    subset(load_fixture("nvue_interface.json"), table)
    assert table["swp22"]["link"]["state"] == {"down": {}} and "lldp" not in table["swp22"]
    assert len(table) == 160
    clean_golden.inject(Fault(kind="lldp_port_id_mac", leaf="leaf4"))
    port = clean_golden.lldp_table("leaf4")["swp21"]["lldp"]["neighbor"]["node7"]["port"]
    assert port == {"id": "b8:3f:d2:00:07:03", "id-type": "mac", "description": "mlx5_3", "ttl": 120}
    assert lldp_emu.nvue_system(clean_golden.leaves["leaf4"])["hostname"] == "leaf4"


def test_link_degraded_keeps_the_neighbor(clean_golden: Hall) -> None:
    clean_golden.inject(Fault(kind="link_degraded", cable="CBL-12-07-03", speed_gbps=200))
    e = clean_golden.lldp_table("leaf4")["swp21"]
    assert (
        e["link"] == {**e["link"], "state": {"up": {}}, "speed": "200G"} and "node7" in e["lldp"]["neighbor"]
    )
    assert redfish_ports(clean_golden, "node7")["mlx5_3"]["CurrentSpeedGbps"] == 200
    clean_golden.clear_all()
    clean_golden.inject(Fault(kind="link_degraded", cable="CBL-12-07-03", speed_gbps="down"))
    e = clean_golden.lldp_table("leaf4")["swp21"]
    assert e["link"]["state"] == {"down": {}} and "node7" in e["lldp"]["neighbor"]
    port = redfish_ports(clean_golden, "node7")["mlx5_3"]
    assert port["LinkStatus"] == "LinkDown" and port["Ethernet"]["LLDPReceive"]["SystemName"] == "leaf4"


# ------------------------------------------------------------------------------------------------
# DCGM
# ------------------------------------------------------------------------------------------------


def test_dcgm_text_has_every_contract_family(clean_golden: Hall) -> None:
    text = clean_golden.dcgm_metrics("node7")
    families = {
        "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_POWER_USAGE",
        "DCGM_FI_DEV_SM_CLOCK",
        "DCGM_FI_DEV_FB_TOTAL",
        "DCGM_FI_DEV_XID_ERRORS",
        "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
        "DCGM_FI_DEV_ROW_REMAP_FAILURE",
        "DCGM_FI_DEV_PCIE_REPLAY_COUNTER",
        "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
        "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
        "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
    }
    for fam in families:
        assert f"# TYPE {fam} " in text and text.count(f"{fam}{{") == 4
    table = dcgm_emu.parse(text)
    assert set(table) == families
    assert sorted(table["DCGM_FI_DEV_GPU_TEMP"]) == ["0", "1", "2", "3"]
    sample = next(line for line in text.splitlines() if line.startswith("DCGM_FI_DEV_GPU_TEMP{"))
    for label in ("gpu=", "UUID=", "modelName=", "Hostname=", "device=", "pci_bus_id="):
        assert label in sample
    assert 'Hostname="node7"' in sample and 'modelName="NVIDIA GB300"' in sample
    assert all(v == 0 for v in table["DCGM_FI_DEV_XID_ERRORS"].values())
    assert dcgm_emu.CONTENT_TYPE.startswith("text/plain; version=0.0.4")


def test_dcgm_fault_views(clean_golden: Hall) -> None:
    hall = clean_golden
    hall.inject(Fault(kind="gpu_unhealthy", node="node1", gpu=2, xid=79))
    hall.inject(Fault(kind="gpu_missing", node="node2", gpu=0))
    hall.inject(Fault(kind="nvlink_degraded", node="node3"))
    t1 = dcgm_emu.parse(hall.dcgm_metrics("node1"))
    assert t1["DCGM_FI_DEV_XID_ERRORS"] == {"0": 0, "1": 0, "2": 79, "3": 0}
    t2 = dcgm_emu.parse(hall.dcgm_metrics("node2"))
    assert sorted(t2["DCGM_FI_DEV_GPU_TEMP"]) == ["1", "2", "3"]
    t3 = dcgm_emu.parse(hall.dcgm_metrics("node3"))
    assert all(v > 100 for v in t3["DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL"].values())
    t4 = dcgm_emu.parse(hall.dcgm_metrics("node4"))
    assert all(v <= 100 for v in t4["DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL"].values())
    hall.inject(Fault(kind="unreachable_node", node="node4", mode="http_500", scope="dcgm"))
    assert hall.reach("node4", "dcgm") == "http_500" and hall.reach("node4", "redfish") == "ok"
    hall.act({"kind": "power_off", "device": "node5"})
    assert hall.reach("node5", "dcgm") == "refused" and hall.reach("node5", "redfish") == "ok"


def test_rendering_is_deterministic_for_a_fixed_spec() -> None:
    spec = golden_spec().model_copy(update={"generated_at": FIXED_TIME})
    a = Hall.generate(spec, cables=golden_cables(spec))
    b = Hall.generate(spec, cables=golden_cables(spec))
    for node in ("node1", "node7"):
        assert a.redfish_tree(node) == b.redfish_tree(node)
        assert a.dcgm_metrics(node) == b.dcgm_metrics(node)
    assert a.lldp_table("leaf3") == b.lldp_table("leaf3")
