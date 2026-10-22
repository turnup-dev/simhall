# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The golden scenario hall-b (CONTRACT.md section 8): 288 cables, 18 nodes, 2 leaves, node7 mlx5_3 on 21."""

from __future__ import annotations

import yaml

from simhall import naming
from simhall.golden import GOLDEN_CABLE, build_golden, golden_cables, golden_faults, golden_spec
from simhall.hall import Hall
from simhall.server import builder_for
from simhall.specfile import SpecFile, dump_spec_file
from simhall.views import normalize_lldp, redfish_ports


def _links_matching_intent(hall: Hall) -> tuple[int, int]:
    """(matching, total) intent links judged from the leaves' own LLDP tables, like the check does."""
    tables = {leaf: normalize_lldp(hall, leaf) for leaf in hall.leaves}
    ok = 0
    for c in hall.intent_cables.values():
        number = naming.port_number(c.b.interface) or 0
        rec = tables[c.b.device][number]
        if (
            rec.link_up
            and rec.remote_system == c.a.device
            and rec.remote_port == c.a.interface
            and rec.speed_gbps == c.speed_gbps
        ):
            ok += 1
    return ok, len(hall.intent_cables)


def test_golden_intent_counts_and_node7_port(golden: Hall) -> None:
    doc = golden.intent()
    assert len(doc["cables"]) == 288
    assert sum(d["role"] == "gpu-node" for d in doc["devices"]) == 18
    assert sum(d["role"] == "leaf-switch" for d in doc["devices"]) == 2
    assert doc["racks"] == [{"name": "12"}] and doc["site"] == "hall-b" and doc["simulated"] is True
    c = golden.intent_cables[GOLDEN_CABLE]
    assert c.a.model_dump() == {"device": "node7", "interface": "mlx5_3"}
    assert c.b.model_dump() == {"device": "leaf4", "interface": "swp21"}
    assert "swp22" in golden.spare_ports("leaf4")
    # The two cables the override displaced have deterministic homes.
    by_a = {(c.a.device, c.a.interface): c.b.interface for c in golden.intent_cables.values()}
    assert by_a[("node3", "mlx5_9")] == "swp50" and by_a[("node3", "mlx5_11")] == "swp145"
    assert golden.golden == "hall-b"


def test_golden_fault_is_visible_as_the_demo_shows(golden: Hall) -> None:
    faults = golden.faults()
    assert len(faults) == 1 and faults[0].kind == "wrong_port" and faults[0].id == "flt-0001"
    table = normalize_lldp(golden, "leaf4")
    assert table[21].link_up is False and table[21].remote_system is None
    assert table[22].link_up is True and table[22].speed_gbps == 400
    assert (table[22].remote_system, table[22].remote_port) == ("node7", "mlx5_3")
    port = redfish_ports(golden, "node7")["mlx5_3"]
    assert port["LinkStatus"] == "LinkUp" and port["Ethernet"]["LLDPReceive"]["PortId"] == "swp22"
    assert _links_matching_intent(golden) == (287, 288)
    state = golden.state()
    assert (
        state["summary"]["deviating_cables"] == 1 and state["cables"][GOLDEN_CABLE]["b_interface"] == "swp22"
    )


def test_seat_cable_action_fixes_the_golden_fault(golden: Hall) -> None:
    result = golden.act({"kind": "seat_cable", "label": GOLDEN_CABLE, "b_interface": "swp21"})
    assert result["ok"] and result["matches_intent"] and result["retired_faults"] == ["flt-0001"]
    assert golden.faults() == []
    assert _links_matching_intent(golden) == (288, 288)
    assert normalize_lldp(golden, "leaf4")[22].remote_system is None


def test_clearing_the_fault_also_restores(golden: Hall) -> None:
    pristine = build_golden(with_faults=False)
    golden.clear("flt-0001")
    assert _links_matching_intent(golden) == (288, 288)
    for leaf in golden.leaves:
        assert normalize_lldp(golden, leaf) == normalize_lldp(pristine, leaf)


def test_reset_builder_brings_the_fault_back(golden: Hall) -> None:
    golden.act({"kind": "seat_cable", "label": GOLDEN_CABLE, "b_interface": "swp21"})
    rebuilt = builder_for(build_golden())()
    assert [f.kind for f in rebuilt.faults()] == ["wrong_port"]
    assert normalize_lldp(rebuilt, "leaf4")[22].remote_system == "node7"


def test_golden_spec_export_round_trips() -> None:
    spec = golden_spec()
    spec_file = SpecFile(spec=spec, cables=golden_cables(spec), faults=golden_faults(spec))
    text = dump_spec_file(spec_file)
    loaded = SpecFile.from_mapping(yaml.safe_load(text))
    hall = loaded.build()
    assert len(hall.intent_cables) == 288
    assert hall.intent_cables[GOLDEN_CABLE].b.interface == "swp21"
    assert [f.summary()["to_interface"] for f in hall.faults()] == ["swp22"]
    assert hall.intent()["cables"] == build_golden().intent()["cables"]


def test_arista_variant_of_the_golden(arista_golden: Hall) -> None:
    c = arista_golden.intent_cables[GOLDEN_CABLE]
    assert c.b.model_dump() == {"device": "leaf4", "interface": "Ethernet21"}
    assert "Ethernet22" in arista_golden.spare_ports("leaf4")
    assert _links_matching_intent(arista_golden) == (288, 288)
