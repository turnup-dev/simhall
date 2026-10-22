# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Seed a real NetBox (for example netbox-docker) with a hall, via pynetbox (CONTRACT.md 6.3).

`pynetbox` is imported lazily by `connect()` so the rest of simhall never needs it. `seed()` takes
any object with the pynetbox API surface it uses, which is how the tests exercise it without a
NetBox. Idempotent: every object is looked up by its natural key before it is created.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CUSTOM_FIELDS: tuple[dict[str, Any], ...] = (
    {"name": "turnup_bmc_url", "label": "Turnup BMC URL", "type": "text", "description": "Redfish base URL"},
    {
        "name": "turnup_dcgm_url",
        "label": "Turnup DCGM URL",
        "type": "text",
        "description": "DCGM exporter base URL",
    },
    {
        "name": "turnup_switch_url",
        "label": "Turnup switch URL",
        "type": "text",
        "description": "eAPI / NVUE base URL",
    },
)

ROLE_COLORS = {
    "gpu-node": "4caf50",
    "leaf-switch": "2196f3",
    "spine-switch": "3f51b5",
    "nvlink-switch": "9c27b0",
    "pdu": "ff9800",
    "cdu": "00bcd4",
}


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "x"


@dataclass
class SeedPlan:
    """Everything `seed()` will create, derived from a `turnup.intent/v1` document."""

    site: dict[str, Any]
    racks: list[dict[str, Any]]
    manufacturers: list[dict[str, Any]]
    roles: list[dict[str, Any]]
    device_types: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    interfaces: list[dict[str, Any]]
    cables: list[dict[str, Any]]
    custom_fields: list[dict[str, Any]] = field(default_factory=lambda: [dict(cf) for cf in CUSTOM_FIELDS])

    def counts(self) -> dict[str, int]:
        return {
            "custom_fields": len(self.custom_fields),
            "sites": 1,
            "racks": len(self.racks),
            "manufacturers": len(self.manufacturers),
            "device_roles": len(self.roles),
            "device_types": len(self.device_types),
            "devices": len(self.devices),
            "interfaces": len(self.interfaces),
            "cables": len(self.cables),
        }


def plan_from_intent(intent: dict[str, Any]) -> SeedPlan:
    site_name = intent["site"]
    site = {
        "name": site_name,
        "slug": _slug(site_name),
        "status": "active",
        "description": "Seeded by simhall",
    }
    racks = [
        {"name": r["name"], "site": site["slug"], "status": "active", "u_height": 48}
        for r in intent.get("racks", [])
    ]
    manufacturers: dict[str, dict[str, Any]] = {}
    roles: dict[str, dict[str, Any]] = {}
    device_types: dict[tuple[str, str], dict[str, Any]] = {}
    devices: list[dict[str, Any]] = []
    interfaces: list[dict[str, Any]] = []
    baselines = intent.get("firmware_baseline", {})
    for d in intent.get("devices", []):
        manufacturer = d.get("manufacturer") or "Unknown"
        model = d.get("model") or "Unknown"
        role = d.get("role", "gpu-node")
        manufacturers.setdefault(manufacturer, {"name": manufacturer, "slug": _slug(manufacturer)})
        roles.setdefault(
            role,
            {
                "name": role.replace("-", " ").capitalize(),
                "slug": role,
                "color": ROLE_COLORS.get(role, "9e9e9e"),
            },
        )
        u_height = 1 if role == "gpu-node" else 2
        device_types.setdefault(
            (manufacturer, model),
            {"manufacturer": _slug(manufacturer), "model": model, "slug": _slug(model), "u_height": u_height},
        )
        mgmt = d.get("mgmt", {})
        custom_fields = {
            "turnup_bmc_url": mgmt.get("bmc_url"),
            "turnup_dcgm_url": mgmt.get("dcgm_url"),
            "turnup_switch_url": mgmt.get("switch_url"),
        }
        local_context: dict[str, Any] = {}
        if role == "gpu-node":
            local_context["turnup"] = {
                "firmware_baseline": baselines.get(model, {}),
                "gpu_count": d.get("gpu_count"),
            }
        elif mgmt.get("protocol"):
            local_context["turnup"] = {"lldp_protocol": mgmt["protocol"]}
        devices.append(
            {
                "name": d["name"],
                "device_type": _slug(model),
                "role": role,
                "site": site["slug"],
                "rack": d.get("rack"),
                "position": d.get("position"),
                "face": "front",
                "serial": d.get("serial") or "",
                "status": "active",
                "custom_fields": custom_fields,
                "local_context_data": local_context,
            }
        )
        for i in d.get("interfaces", []):
            interfaces.append(
                {
                    "device": d["name"],
                    "name": i["name"],
                    "type": i.get("type", "other"),
                    "mac_address": (i.get("mac") or "").upper() or None,
                }
            )
    cables = [
        {
            "label": c["label"],
            "a": {"device": c["a"]["device"], "interface": c["a"]["interface"]},
            "b": {"device": c["b"]["device"], "interface": c["b"]["interface"]},
            "status": "connected",
            "speed_gbps": c.get("speed_gbps"),
        }
        for c in intent.get("cables", [])
    ]
    return SeedPlan(
        site=site,
        racks=racks,
        manufacturers=list(manufacturers.values()),
        roles=list(roles.values()),
        device_types=list(device_types.values()),
        devices=devices,
        interfaces=interfaces,
        cables=cables,
    )


def connect(netbox_url: str, token: str) -> Any:
    """A pynetbox API object (imported here so simhall itself does not depend on pynetbox)."""
    import pynetbox  # noqa: PLC0415

    return pynetbox.api(netbox_url, token=token)


def _version(nb: Any) -> tuple[int, int]:
    try:
        parts = str(nb.version).split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (AttributeError, ValueError, IndexError):
        return 4, 0


def _get_or_create(
    endpoint: Any, lookup: dict[str, Any], payload: dict[str, Any], created: list[str], what: str
) -> Any:
    obj = endpoint.get(**lookup)
    if obj is None:
        obj = endpoint.create(payload)
        created.append(what)
    return obj


def seed(
    intent: dict[str, Any],
    nb: Any,
    *,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Create the plan in NetBox through `nb` (pynetbox API or a stand-in). Returns a summary."""
    plan = plan_from_intent(intent)
    version = _version(nb)
    created: list[str] = []
    log(f"NetBox {version[0]}.{version[1]}: seeding site {plan.site['name']} ({plan.counts()})")

    cf_key = "object_types" if version >= (4, 0) else "content_types"
    for cf in plan.custom_fields:
        _get_or_create(
            nb.extras.custom_fields,
            {"name": cf["name"]},
            {**cf, cf_key: ["dcim.device"]},
            created,
            f"custom_field:{cf['name']}",
        )

    site = _get_or_create(
        nb.dcim.sites, {"slug": plan.site["slug"]}, plan.site, created, f"site:{plan.site['slug']}"
    )
    racks = {
        r["name"]: _get_or_create(
            nb.dcim.racks,
            {"site_id": site.id, "name": r["name"]},
            {**r, "site": site.id},
            created,
            f"rack:{r['name']}",
        )
        for r in plan.racks
    }
    manufacturers = {
        m["slug"]: _get_or_create(
            nb.dcim.manufacturers, {"slug": m["slug"]}, m, created, f"manufacturer:{m['slug']}"
        )
        for m in plan.manufacturers
    }
    roles = {
        r["slug"]: _get_or_create(nb.dcim.device_roles, {"slug": r["slug"]}, r, created, f"role:{r['slug']}")
        for r in plan.roles
    }
    device_types = {
        dt["slug"]: _get_or_create(
            nb.dcim.device_types,
            {"slug": dt["slug"]},
            {**dt, "manufacturer": manufacturers[dt["manufacturer"]].id},
            created,
            f"device_type:{dt['slug']}",
        )
        for dt in plan.device_types
    }

    role_key = "role" if version >= (4, 0) else "device_role"
    devices: dict[str, Any] = {}
    for d in plan.devices:
        payload = {
            "name": d["name"],
            "device_type": device_types[d["device_type"]].id,
            role_key: roles[d["role"]].id,
            "site": site.id,
            "rack": racks[d["rack"]].id if d.get("rack") in racks else None,
            "position": d.get("position"),
            "face": d["face"] if d.get("position") is not None else None,
            "serial": d["serial"],
            "status": d["status"],
            "custom_fields": d["custom_fields"],
            "local_context_data": d["local_context_data"],
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        devices[d["name"]] = _get_or_create(
            nb.dcim.devices, {"name": d["name"], "site_id": site.id}, payload, created, f"device:{d['name']}"
        )

    interfaces: dict[tuple[str, str], Any] = {}
    by_device: dict[str, list[dict[str, Any]]] = {}
    for i in plan.interfaces:
        by_device.setdefault(i["device"], []).append(i)
    mac_objects = version >= (4, 2)
    for dev_name, wanted in by_device.items():
        dev = devices[dev_name]
        existing = {i.name: i for i in nb.dcim.interfaces.filter(device_id=dev.id)}
        to_create = []
        for i in wanted:
            if i["name"] in existing:
                interfaces[(dev_name, i["name"])] = existing[i["name"]]
                continue
            payload: dict[str, Any] = {"device": dev.id, "name": i["name"], "type": i["type"]}
            if i.get("mac_address") and not mac_objects:
                payload["mac_address"] = i["mac_address"]
            to_create.append(payload)
        if to_create:
            made = nb.dcim.interfaces.create(to_create)
            if not isinstance(made, list):
                made = [made]
            for obj in made:
                interfaces[(dev_name, obj.name)] = obj
                created.append(f"interface:{dev_name}/{obj.name}")
        if mac_objects:
            for i in wanted:
                if not i.get("mac_address") or i["name"] in existing:
                    continue
                iface = interfaces[(dev_name, i["name"])]
                mac = nb.dcim.mac_addresses.create(
                    {
                        "mac_address": i["mac_address"],
                        "assigned_object_type": "dcim.interface",
                        "assigned_object_id": iface.id,
                    }
                )
                created.append(f"mac:{dev_name}/{i['name']}")
                try:
                    iface.primary_mac_address = mac.id
                    iface.save()
                except Exception as e:  # noqa: BLE001 - best effort, NetBox version dependent
                    log(f"could not set primary MAC on {dev_name}/{i['name']}: {e}")

    cables_made = 0
    for c in plan.cables:
        a = interfaces.get((c["a"]["device"], c["a"]["interface"]))
        b = interfaces.get((c["b"]["device"], c["b"]["interface"]))
        if a is None or b is None:
            log(f"skip cable {c['label']}: interface missing")
            continue
        if getattr(a, "cable", None):
            continue
        nb.dcim.cables.create(
            {
                "a_terminations": [{"object_type": "dcim.interface", "object_id": a.id}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": b.id}],
                "label": c["label"],
                "status": c["status"],
            }
        )
        cables_made += 1
        created.append(f"cable:{c['label']}")
    log(f"done: {len(created)} objects created ({cables_made} cables)")
    return {
        "netbox_version": f"{version[0]}.{version[1]}",
        "planned": plan.counts(),
        "created": len(created),
        "created_items": created,
    }
