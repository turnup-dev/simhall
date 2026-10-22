# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Spec files: load, dump, build; faults and explicit cables travel with the spec."""

from __future__ import annotations

import pytest
import yaml

from simhall.specfile import SpecFile, dump_spec_file, load_spec_file


def test_load_build_dump_round_trip(tmp_path) -> None:
    doc = {
        "site": "hall-t",
        "nodes_per_rack": 2,
        "ports_per_node": 2,
        "spare_ports": 2,
        "switch_vendor": "arista",
        "cables": [
            {
                "label": "CBL-12-01-00",
                "a": {"device": "node1", "interface": "mlx5_0"},
                "b": {"device": "leaf3", "interface": "Ethernet1"},
            },
            {
                "label": "CBL-12-02-01",
                "a": {"device": "node2", "interface": "mlx5_1"},
                "b": {"device": "leaf4", "interface": "Ethernet2"},
            },
        ],
        "faults": [
            {"kind": "missing_link", "cable": "CBL-12-01-00"},
            {"kind": "gpu_unhealthy", "node": "node2", "gpu": 1, "xid": 31},
        ],
    }
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(doc))
    spec_file = load_spec_file(path)
    assert spec_file.spec.site == "hall-t" and spec_file.spec.switch_vendor == "arista"
    hall = spec_file.build()
    assert list(hall.intent_cables) == ["CBL-12-01-00", "CBL-12-02-01"]
    assert [f.kind for f in hall.faults()] == ["missing_link", "gpu_unhealthy"]
    assert hall.state()["cables"]["CBL-12-01-00"]["seated"] is False
    dumped = yaml.safe_load(dump_spec_file(spec_file))
    assert dumped["faults"] == [
        {"kind": "missing_link", "cable": "CBL-12-01-00"},
        {"kind": "gpu_unhealthy", "node": "node2", "gpu": 1, "xid": 31},
    ]
    assert len(dumped["cables"]) == 2 and dumped["site"] == "hall-t"
    json_path = tmp_path / "spec.json"
    json_path.write_text(SpecFile.model_validate(spec_file.model_dump()).model_dump_json())
    assert load_spec_file(json_path).spec.site == "hall-t"


def test_spec_file_rejects_garbage(tmp_path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- not\n- a\n- mapping\n")
    with pytest.raises(ValueError, match="mapping"):
        load_spec_file(path)
    with pytest.raises(ValueError):
        SpecFile.from_mapping({"site": "x", "nodes": 3})
    assert SpecFile.from_mapping({}).spec.site == "hall-b"
    nested = SpecFile.from_mapping({"spec": {"site": "nested"}, "faults": []})
    assert nested.spec.site == "nested"
