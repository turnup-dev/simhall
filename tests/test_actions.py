# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Actions change the ground truth and retire exactly the faults they physically resolve."""

from __future__ import annotations

import pytest

from simhall import actions, faults
from simhall.hall import Conflict, Hall, SimError, UnknownObject
from simhall.views import normalize_lldp, redfish_firmware, redfish_ports, redfish_system


def test_seat_cable_retires_only_cable_faults_for_that_label(clean_golden: Hall) -> None:
    hall = clean_golden
    f_port = hall.inject(faults.wrong_port("CBL-12-07-03", "leaf4", "swp22"))
    f_node = hall.inject(faults.unreachable_node("node7", mode="http_500"))
    f_other = hall.inject(faults.missing_link("CBL-12-07-04"))
    result = hall.act(actions.seat_cable("CBL-12-07-03", b_interface="swp21", actor="tech-1"))
    assert result["retired_faults"] == [f_port.id]
    assert {f.id for f in hall.faults()} == {f_node.id, f_other.id}
    assert hall.reach("node7", "redfish") == "http_500"
    assert normalize_lldp(hall, "leaf4")[21].remote_port == "mlx5_3"


def test_seat_cable_defaults_to_intent_ends_and_rejects_occupied_ports(clean_golden: Hall) -> None:
    hall = clean_golden
    hall.act(actions.unseat_cable("CBL-12-07-03"))
    assert redfish_ports(hall, "node7")["mlx5_3"]["LinkStatus"] == "NoLink"
    assert hall.state()["cables"]["CBL-12-07-03"]["seated"] is False
    hall.act(actions.seat_cable("CBL-12-07-03"))
    assert redfish_ports(hall, "node7")["mlx5_3"]["LinkStatus"] == "LinkUp"
    occupied = hall.intent_cables["CBL-12-07-04"].b.interface
    with pytest.raises(Conflict, match="occupied"):
        hall.act(actions.seat_cable("CBL-12-07-03", b_interface=occupied))
    with pytest.raises(UnknownObject):
        hall.act(actions.seat_cable("CBL-12-07-03", b_device="leaf9"))
    with pytest.raises(UnknownObject):
        hall.act(actions.unseat_cable("CBL-99-99-99"))


def test_extra_cable_not_in_intent_is_allowed_for_unexpected_links() -> None:
    from simhall.models import HallSpec

    spec = HallSpec(nodes_per_rack=2, ports_per_node=2, spare_ports=2)
    hall = Hall.generate(spec)
    cable = hall.intent_cables["CBL-12-02-01"]
    hall.act(actions.unseat_cable(cable.label))
    with pytest.raises(SimError, match="required"):
        hall.act(actions.seat_cable("CBL-EXTRA-1"))
    hall.act(
        actions.seat_cable(
            "CBL-EXTRA-1", a_device="node2", a_interface="mlx5_1", b_device="leaf3", b_interface="swp3"
        )
    )
    table = normalize_lldp(hall, "leaf3")
    assert table[3].remote_system == "node2" and table[3].remote_port == "mlx5_1"
    st = hall.state()
    assert (
        st["cables"]["CBL-EXTRA-1"]["in_intent"] is False and st["cables"]["CBL-EXTRA-1"]["deviates"] is True
    )
    assert hall.cable_record("CBL-EXTRA-1")["in_intent"] is False


def test_swap_pair_then_manual_fix_needs_the_other_cable_moved(clean_golden: Hall) -> None:
    hall = clean_golden
    a, b = "CBL-12-07-03", "CBL-12-07-05"
    port_a, port_b = (hall.intent_cables[x].b.interface for x in (a, b))
    hall.inject(faults.swap_pair(a, b))
    table = normalize_lldp(hall, "leaf4")
    assert table[int(port_a[3:])].remote_port == "mlx5_5" and table[int(port_b[3:])].remote_port == "mlx5_3"
    with pytest.raises(Conflict):
        hall.act(actions.seat_cable(a))  # b is still physically in a's port
    assert hall.faults() == []  # the swap was materialized into the base when a was touched
    hall.act(actions.unseat_cable(b))
    hall.act(actions.seat_cable(a))
    hall.act(actions.seat_cable(b))
    assert hall.state()["summary"]["deviating_cables"] == 0


def test_power_actions_and_pdu(clean_golden: Hall) -> None:
    hall = clean_golden
    f = hall.inject(faults.pdu_outlet_off("node7"))
    assert hall.reach("node7", "redfish") == "refused"
    assert normalize_lldp(hall, "leaf4")[21].link_up is False
    result = hall.act(actions.power_on("node7"))
    assert result["retired_faults"] == [f.id] and hall.reach("node7", "redfish") == "ok"
    assert normalize_lldp(hall, "leaf4")[21].remote_port == "mlx5_3"
    hall.act(actions.power_off("node7"))
    assert redfish_system(hall, "node7")["PowerState"] == "Off"
    assert hall.reach("node7", "redfish") == "ok" and hall.reach("node7", "dcgm") == "refused"
    assert normalize_lldp(hall, "leaf4")[21].remote_system is None
    hall.act(actions.power_on("node7"))
    assert redfish_system(hall, "node7")["PowerState"] == "On"
    assert hall.act(actions.power_on("leaf4"))["ok"]


def test_replace_node_retires_node_faults_and_changes_serial(clean_golden: Hall) -> None:
    hall = clean_golden
    old_serial = hall.nodes["node7"].serial
    ids = {
        hall.inject(faults.gpu_unhealthy("node7", 1)).id,
        hall.inject(faults.node_health_warning("node7")).id,
        hall.inject(faults.firmware_off_baseline("node7", "bmc", "1.0.0")).id,
        hall.inject(faults.coolant_leak("node7")).id,
    }
    keep = hall.inject(faults.wrong_port("CBL-12-07-03", "leaf4", "swp22"))
    result = hall.act(actions.replace_node("node7", "SN-NEW-1"))
    assert set(result["retired_faults"]) == ids and result["old_serial"] == old_serial
    assert [f.id for f in hall.faults()] == [keep.id]
    system = redfish_system(hall, "node7")
    assert system["SerialNumber"] == "SN-NEW-1" and system["Status"]["Health"] == "OK"
    assert redfish_firmware(hall, "node7")["BMC Firmware"] == "1.12.0"
    assert (
        "replaced"
        in hall.redfish_tree("node7")["/redfish/v1/Systems/System.Embedded.1/LogServices/EventLog/Entries"][
            "Members"
        ][-1]["Message"]
    )


def test_update_firmware_and_scan_qr(clean_golden: Hall) -> None:
    hall = clean_golden
    f = hall.inject(faults.firmware_off_baseline("node7", "nic", "28.40.0000"))
    other = hall.inject(faults.firmware_off_baseline("node7", "bios", "2.0.0"))
    result = hall.act(actions.update_firmware("node7", "nic", "28.43.1014"))
    assert result["retired_faults"] == [f.id] and result["old_version"] == "28.40.0000"
    assert [x.id for x in hall.faults()] == [other.id]
    assert redfish_firmware(hall, "node7")["ConnectX-8 NIC Firmware"] == "28.43.1014"
    record = hall.act(actions.scan_qr("CBL-12-07-03"))["record"]
    assert record["a_display"] == "node7 mlx5_3" and record["b_display"] == "leaf4/21"
    assert (record["rack"], record["node_index"], record["port_index"]) == ("12", 7, 3)
    with pytest.raises(UnknownObject):
        hall.act(actions.scan_qr("CBL-00-00-00"))


def test_fault_validation_errors(clean_golden: Hall) -> None:
    hall = clean_golden
    with pytest.raises(UnknownObject):
        hall.inject(faults.wrong_port("CBL-00-00-00", "leaf4", "swp22"))
    with pytest.raises(UnknownObject):
        hall.inject(faults.wrong_port("CBL-12-07-03", "leaf4", "swp999"))
    with pytest.raises(Conflict):
        hall.inject(faults.wrong_port("CBL-12-07-03", "leaf4", "swp23"))  # occupied by intent cabling
    with pytest.raises(UnknownObject):
        hall.inject(faults.gpu_missing("node7", 9))
    with pytest.raises(UnknownObject):
        hall.inject(faults.coolant_leak("nowhere"))
    with pytest.raises(UnknownObject):
        hall.clear("flt-9999")
    with pytest.raises(ValueError):
        faults.Fault(kind="wrong_port", cable="x")
    with pytest.raises(ValueError):
        faults.Fault(kind="swap_pair", cable_a="x", cable_b="x")
    leak = hall.inject(faults.coolant_leak("12"))
    assert all(hall.physical.nodes[n].leak for n in hall.nodes)
    hall.clear(leak.id)
    assert not any(hall.physical.nodes[n].leak for n in hall.nodes)
    assert len(hall.history()) >= 2
