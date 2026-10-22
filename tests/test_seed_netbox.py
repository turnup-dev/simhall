# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""seed-netbox against a stand-in for the pynetbox API surface (no NetBox is available here)."""

from __future__ import annotations

from typing import Any

from simhall.golden import build_golden
from simhall.seed_netbox import plan_from_intent, seed


class FakeRecord:
    def __init__(self, data: dict[str, Any], obj_id: int) -> None:
        self.__dict__.update(data)
        self.id = obj_id
        self.saved = False

    def save(self) -> bool:
        self.saved = True
        return True


class FakeEndpoint:
    def __init__(self, store: dict[str, int], interfaces: FakeEndpoint | None = None) -> None:
        self.records: list[FakeRecord] = []
        self.created: list[dict[str, Any]] = []
        self._store = store
        self._interfaces = interfaces

    def _new(self, payload: dict[str, Any]) -> FakeRecord:
        self._store["next_id"] += 1
        rec = FakeRecord(payload, self._store["next_id"])
        self.records.append(rec)
        self.created.append(payload)
        if self._interfaces is not None:  # a cable: mark its terminations occupied, like NetBox does
            for end in ("a_terminations", "b_terminations"):
                for term in payload.get(end, []):
                    for iface in self._interfaces.records:
                        if iface.id == term["object_id"]:
                            iface.cable = rec
        return rec

    def get(self, **lookup: Any) -> FakeRecord | None:
        for rec in self.records:
            if all(
                getattr(rec, k.removesuffix("_id"), None) == v or getattr(rec, k, None) == v
                for k, v in lookup.items()
            ):
                return rec
        return None

    def filter(self, **lookup: Any) -> list[FakeRecord]:
        return [
            r
            for r in self.records
            if all(
                getattr(r, k.removesuffix("_id"), None) == v or getattr(r, k, None) == v
                for k, v in lookup.items()
            )
        ]

    def create(self, payload: Any) -> Any:
        if isinstance(payload, list):
            return [self._new(p) for p in payload]
        return self._new(payload)


class FakeNetBox:
    """Just enough of pynetbox: `nb.version`, `nb.extras.custom_fields`, `nb.dcim.<endpoint>`."""

    def __init__(self, version: str = "4.3.6") -> None:
        self.version = version
        store = {"next_id": 0}

        class NS:
            pass

        self.extras = NS()
        self.extras.custom_fields = FakeEndpoint(store)
        self.dcim = NS()
        for name in (
            "sites",
            "racks",
            "manufacturers",
            "device_roles",
            "device_types",
            "devices",
            "interfaces",
            "mac_addresses",
            "cables",
        ):
            setattr(self.dcim, name, FakeEndpoint(store))
        self.dcim.cables = FakeEndpoint(store, interfaces=self.dcim.interfaces)


def test_plan_counts_for_the_golden_hall() -> None:
    plan = plan_from_intent(build_golden().intent())
    assert plan.counts() == {
        "custom_fields": 3,
        "sites": 1,
        "racks": 1,
        "manufacturers": 1,
        "device_roles": 2,
        "device_types": 2,
        "devices": 20,
        "interfaces": 608,
        "cables": 288,
    }
    node7 = next(d for d in plan.devices if d["name"] == "node7")
    assert node7["custom_fields"]["turnup_bmc_url"] == "http://127.0.0.1:8900/dev/node7"
    assert node7["local_context_data"]["turnup"]["firmware_baseline"]["bmc"] == "1.12.0"
    assert plan.cables[0]["status"] == "connected"


def test_seed_is_idempotent_against_a_fake_netbox() -> None:
    intent = build_golden().intent()
    nb = FakeNetBox("4.3.6")
    messages: list[str] = []
    summary = seed(intent, nb, log=messages.append)
    assert (
        summary["netbox_version"] == "4.3"
        and summary["created"] == 3 + 1 + 1 + 1 + 2 + 2 + 20 + 608 + 288 + 288
    )
    assert len(nb.dcim.devices.records) == 20 and len(nb.dcim.interfaces.records) == 608
    assert len(nb.dcim.cables.records) == 288 and len(nb.dcim.mac_addresses.records) == 288
    cf = nb.extras.custom_fields.created[0]
    assert cf["object_types"] == ["dcim.device"]
    device = nb.dcim.devices.created[0]
    assert device["role"] and "device_role" not in device and device["custom_fields"]["turnup_bmc_url"]
    cable = nb.dcim.cables.created[0]
    assert cable["a_terminations"][0]["object_type"] == "dcim.interface" and cable["label"].startswith(
        "CBL-12-"
    )
    assert "mac_address" not in nb.dcim.interfaces.created[0]  # 4.2+: MACs are objects
    again = seed(intent, nb, log=messages.append)
    assert again["created"] == 0
    assert len(nb.dcim.cables.records) == 288 and len(nb.dcim.devices.records) == 20
    assert any("done" in m for m in messages)


def test_seed_on_netbox_3_uses_legacy_fields() -> None:
    intent = build_golden(with_faults=False).intent()
    nb = FakeNetBox("3.7.8")
    seed(intent, nb)
    assert nb.extras.custom_fields.created[0]["content_types"] == ["dcim.device"]
    assert "device_role" in nb.dcim.devices.created[0]
    assert nb.dcim.interfaces.created[0]["mac_address"] == "B8:3F:D2:00:01:00"
    assert nb.dcim.mac_addresses.records == []
