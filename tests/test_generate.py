# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Generation: spec defaults, the cabling rule, labels, naming, intent shape, explicit cables."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from simhall import naming
from simhall.hall import Hall, SimError
from simhall.models import INTENT_SCHEMA, CableSpec, HallSpec, gpu_count_for_model

from .conftest import FIXED_TIME, load_fixture


def _cable_for(hall: Hall, node: str, iface: str) -> CableSpec:
    return next(c for c in hall.intent_cables.values() if c.a.device == node and c.a.interface == iface)


def test_spec_defaults_match_contract_6_1() -> None:
    spec = HallSpec()
    assert (spec.site, spec.racks, spec.nodes_per_rack, spec.ports_per_node, spec.leaves_per_rack) == (
        "hall-b",
        1,
        18,
        16,
        2,
    )
    assert (spec.node_name_fmt, spec.leaf_name_fmt, spec.leaf_start, spec.rack_start) == (
        "node{n}",
        "leaf{n}",
        3,
        12,
    )
    assert (spec.switch_vendor, spec.lldp_port_id_mode, spec.node_model, spec.seed) == (
        "nvidia",
        "name",
        "GB300 NVL72 compute tray",
        1,
    )
    assert spec.effective_gpu_count == 4
    assert spec.used_ports_per_leaf == 144 and spec.leaf_ports == 160
    assert spec.firmware_baseline == {"bmc": "1.12.0", "bios": "2.3.1", "nic": "28.43.1014"}


def test_gpu_count_derivation_2_1() -> None:
    assert gpu_count_for_model("GB300 NVL72 compute tray") == 4
    assert gpu_count_for_model("HGX H100") == 8
    assert gpu_count_for_model("MI300X node") == 8
    assert gpu_count_for_model("anything else") == 8


def test_cabling_rule_6_1_default_positions() -> None:
    hall = Hall.generate(HallSpec())
    assert len(hall.intent_cables) == 288
    # node n (1-based) port p (0-based) -> leaf leaf_start + p % 2, port (n-1)*8 + p//2 + 1
    assert _cable_for(hall, "node7", "mlx5_3").b.model_dump() == {"device": "leaf4", "interface": "swp50"}
    assert _cable_for(hall, "node1", "mlx5_0").b.model_dump() == {"device": "leaf3", "interface": "swp1"}
    assert _cable_for(hall, "node1", "mlx5_1").b.model_dump() == {"device": "leaf4", "interface": "swp1"}
    assert _cable_for(hall, "node18", "mlx5_15").b.model_dump() == {"device": "leaf4", "interface": "swp144"}
    for leaf in ("leaf3", "leaf4"):
        used = sorted(
            naming.port_number(c.b.interface) or 0 for c in hall.intent_cables.values() if c.b.device == leaf
        )
        assert used == list(range(1, 145))


def test_cable_labels_and_display_forms() -> None:
    hall = Hall.generate(HallSpec())
    c = _cable_for(hall, "node7", "mlx5_3")
    assert c.label == "CBL-12-07-03"
    assert naming.cable_label("12", 7, 3) == "CBL-12-07-03"
    assert naming.parse_cable_label("CBL-12-07-03") == ("12", 7, 3)
    assert naming.find_cable_labels("Cable CBL-12-07-03 then CBL-12-01-15.") == [
        "CBL-12-07-03",
        "CBL-12-01-15",
    ]
    assert naming.display("leaf4", "swp21") == "leaf4/21"
    assert naming.display("leaf4", "Ethernet21/1") == "leaf4/21/1"
    assert naming.display("node7", "mlx5_3") == "node7 mlx5_3"
    assert naming.port_number("Ethernet21") == 21 and naming.port_number("mlx5_3") is None
    assert naming.node_mac(7, 3) == "b8:3f:d2:00:07:03"
    assert hall.nodes["node7"].chassis_mac == "b8:3f:d2:00:07:00"


def test_non_divisible_ports_never_collide() -> None:
    spec = HallSpec(site="odd", nodes_per_rack=5, ports_per_node=5, leaves_per_rack=3, spare_ports=0)
    hall = Hall.generate(spec)
    assert spec.ports_per_node_per_leaf == 2
    b_ends = [(c.b.device, c.b.interface) for c in hall.intent_cables.values()]
    assert len(b_ends) == len(set(b_ends)) == 25
    for leaf in hall.leaves.values():
        assert all(
            c.b.interface in leaf.ports for c in hall.intent_cables.values() if c.b.device == leaf.name
        )


def test_multi_rack_naming_and_rack_local_ports() -> None:
    hall = Hall.generate(HallSpec(racks=2, nodes_per_rack=4, ports_per_node=4, leaves_per_rack=2))
    assert hall.racks == ["12", "13"]
    assert list(hall.nodes) == [f"node{n}" for n in range(1, 9)]
    assert list(hall.leaves) == ["leaf3", "leaf4", "leaf5", "leaf6"]
    assert hall.nodes["node5"].rack == "13" and hall.nodes["node5"].index_in_rack == 1
    c = _cable_for(hall, "node5", "mlx5_0")
    assert c.label == "CBL-13-01-00"
    assert c.b.model_dump() == {"device": "leaf5", "interface": "swp1"}


def test_arista_port_names() -> None:
    hall = Hall.generate(HallSpec(switch_vendor="arista", nodes_per_rack=2, ports_per_node=4))
    assert _cable_for(hall, "node1", "mlx5_0").b.interface == "Ethernet1"
    assert hall.leaves["leaf3"].model == "7060DX5-64S" and hall.leaves["leaf3"].manufacturer == "Arista"
    assert hall.intent()["devices"][-1]["mgmt"]["protocol"] == "eapi"


def test_intent_shape_section_2() -> None:
    hall = Hall.generate(HallSpec(generated_at=FIXED_TIME))
    doc = hall.intent()
    assert doc["schema"] == INTENT_SCHEMA == "turnup.intent/v1"
    assert doc["site"] == "hall-b" and doc["simulated"] is True and doc["generated_at"] == FIXED_TIME
    assert doc["racks"] == [{"name": "12"}]
    node7 = next(d for d in doc["devices"] if d["name"] == "node7")
    assert node7["role"] == "gpu-node" and node7["rack"] == "12" and node7["gpu_count"] == 4
    assert node7["manufacturer"] == "NVIDIA" and node7["model"] == "GB300 NVL72 compute tray"
    assert node7["mgmt"] == {
        "bmc_url": "http://127.0.0.1:8900/dev/node7",
        "dcgm_url": "http://127.0.0.1:8900/dev/node7",
    }
    assert {"name": "mlx5_3", "type": "400gbase-x-osfp", "mac": "b8:3f:d2:00:07:03"} in node7["interfaces"]
    leaf4 = next(d for d in doc["devices"] if d["name"] == "leaf4")
    assert leaf4["role"] == "leaf-switch" and leaf4["mgmt"] == {
        "switch_url": "http://127.0.0.1:8900/dev/leaf4",
        "protocol": "nvue",
    }
    assert {"name": "swp21", "type": "400gbase-x-osfp"} in leaf4["interfaces"]
    assert len(leaf4["interfaces"]) == 160
    assert doc["firmware_baseline"] == {
        "GB300 NVL72 compute tray": {"bmc": "1.12.0", "bios": "2.3.1", "nic": "28.43.1014"}
    }
    cable = next(c for c in doc["cables"] if c["label"] == "CBL-12-07-03")
    assert cable == {
        "label": "CBL-12-07-03",
        "a": {"device": "node7", "interface": "mlx5_3"},
        "b": {"device": "leaf4", "interface": "swp50"},
        "speed_gbps": 400,
    }


def test_write_intent_yaml_and_json(tmp_path) -> None:
    hall = Hall.generate(HallSpec(nodes_per_rack=2, ports_per_node=2, generated_at=FIXED_TIME))
    y = hall.write_intent(tmp_path / "hall-b.yaml")
    j = hall.write_intent(tmp_path / "hall-b.json")
    assert yaml.safe_load(y.read_text()) == hall.intent()
    assert json.loads(j.read_text()) == hall.intent()


def test_explicit_cables_accepted_and_validated() -> None:
    spec = HallSpec(nodes_per_rack=2, ports_per_node=2, spare_ports=2)
    fixture = load_fixture("intent_cable.json")
    fixture["a"] = {"device": "node1", "interface": "mlx5_0"}
    fixture["b"] = {"device": "leaf3", "interface": "swp3"}
    fixture["label"] = "CBL-12-01-00"
    hall = Hall.generate(spec, cables=[fixture])
    assert list(hall.intent_cables) == ["CBL-12-01-00"]
    assert hall.intent_cables["CBL-12-01-00"].b.interface == "swp3"
    assert hall.spare_ports("leaf3") == ["swp1", "swp2", "swp4"]
    with pytest.raises(SimError, match="duplicate cable label"):
        Hall.generate(spec, cables=[fixture, fixture])
    with pytest.raises(SimError, match="not a leaf port"):
        Hall.generate(spec, cables=[{**fixture, "b": {"device": "leaf3", "interface": "swp99"}}])
    with pytest.raises(SimError, match="not a node interface"):
        Hall.generate(spec, cables=[{**fixture, "a": {"device": "nodeX", "interface": "mlx5_0"}}])
    twice = {**fixture, "label": "CBL-12-01-01", "b": {"device": "leaf3", "interface": "swp4"}}
    with pytest.raises(SimError, match="cabled twice"):
        Hall.generate(spec, cables=[fixture, twice])


def test_spec_rejects_unknown_fields_and_bad_formats() -> None:
    with pytest.raises(ValidationError):
        HallSpec(nodes=18)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        HallSpec(node_name_fmt="node")
    with pytest.raises(ValidationError):
        HallSpec(switch_vendor="cisco")  # type: ignore[arg-type]


def test_base_url_follows_server_and_generation_is_deterministic() -> None:
    a = Hall.generate(HallSpec(nodes_per_rack=2, ports_per_node=2, generated_at=FIXED_TIME))
    b = Hall.generate(HallSpec(nodes_per_rack=2, ports_per_node=2, generated_at=FIXED_TIME))
    assert a.intent() == b.intent()
    assert a.redfish_tree("node1") == b.redfish_tree("node1")
    assert a.dcgm_metrics("node1") == b.dcgm_metrics("node1")
    a.base_url = "http://127.0.0.1:9999/"
    assert a.intent()["devices"][0]["mgmt"]["bmc_url"] == "http://127.0.0.1:9999/dev/node1"
    assert a.nodes["node1"].serial != a.nodes["node2"].serial
    assert a.state()["summary"] == {"nodes": 2, "leaves": 2, "cables": 4, "deviating_cables": 0, "faults": 0}
