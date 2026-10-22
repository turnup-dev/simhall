# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Minimal NetBox REST API (`/api/dcim/...`) rendered from the hall's intent (CONTRACT.md 2.1, 6.3).

Serves `sites`, `racks`, `devices`, `interfaces`, `cables` (plus `manufacturers`, `device-roles`,
`device-types`) with NetBox pagination (`{count, next, previous, results}`) and the filters the
turnup-check adapter uses (`site=`, `device_id=`, `limit=`, `offset=`). Any token is accepted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from simhall.hall import Hall

NETBOX_VERSION = "4.3.6"
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000

COLLECTIONS = (
    "sites",
    "racks",
    "manufacturers",
    "device-roles",
    "device-types",
    "devices",
    "interfaces",
    "cables",
)

INTERFACE_TYPE_LABELS = {
    "400gbase-x-osfp": "OSFP (400GE)",
    "400gbase-x-qsfpdd": "QSFP-DD (400GE)",
    "800gbase-x-osfp": "OSFP (800GE)",
    "200gbase-x-qsfp56": "QSFP56 (200GE)",
    "100gbase-x-qsfp28": "QSFP28 (100GE)",
}

UrlBuilder = Callable[[int | None, int], str]


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "x"


class NetBoxView:
    """All NetBox objects for one hall, built once per `base_url`."""

    def __init__(self, hall: Hall) -> None:
        self.hall = hall
        self.base_url = hall.base_url
        self.objects: dict[str, list[dict[str, Any]]] = {c: [] for c in COLLECTIONS}
        self.filters: dict[str, list[dict[str, Any]]] = {c: [] for c in COLLECTIONS}
        self._build()

    # ----------------------------------------------------------------------------------------------

    def _url(self, collection: str, obj_id: int) -> str:
        return f"{self.base_url}/netbox/api/dcim/{collection}/{obj_id}/"

    def _ref(self, collection: str, obj_id: int, name: str, **extra: Any) -> dict[str, Any]:
        ref: dict[str, Any] = {
            "id": obj_id,
            "url": self._url(collection, obj_id),
            "display": name,
            "name": name,
        }
        ref.update(extra)
        return ref

    def _build(self) -> None:
        hall = self.hall
        ts = hall.generated_at
        site_id = 1
        site = {
            "id": site_id,
            "url": self._url("sites", site_id),
            "display": hall.site,
            "name": hall.site,
            "slug": _slug(hall.site),
            "status": {"value": "active", "label": "Active"},
            "region": None,
            "group": None,
            "tenant": None,
            "facility": "",
            "time_zone": None,
            "description": "Simulated by simhall",
            "physical_address": "",
            "shipping_address": "",
            "latitude": None,
            "longitude": None,
            "comments": "",
            "asns": [],
            "tags": [],
            "custom_fields": {},
            "created": ts,
            "last_updated": ts,
            "rack_count": len(hall.racks),
            "device_count": len(hall.nodes) + len(hall.leaves),
        }
        site_ref = {
            "id": site_id,
            "url": self._url("sites", site_id),
            "display": hall.site,
            "name": hall.site,
            "slug": site["slug"],
        }
        self._add("sites", site, {"id": site_id, "name": hall.site, "slug": site["slug"]})

        rack_refs: dict[str, dict[str, Any]] = {}
        for i, rack in enumerate(hall.racks, start=1):
            devices_in_rack = len(hall.nodes_in_rack(rack)) + len(hall.leaves_in_rack(rack))
            obj = {
                "id": i,
                "url": self._url("racks", i),
                "display": rack,
                "name": rack,
                "facility_id": None,
                "site": site_ref,
                "location": None,
                "tenant": None,
                "status": {"value": "active", "label": "Active"},
                "role": None,
                "serial": "",
                "asset_tag": None,
                "rack_type": None,
                "form_factor": {"value": "4-post-cabinet", "label": "4-post cabinet"},
                "width": {"value": 19, "label": "19 inches"},
                "u_height": 48,
                "starting_unit": 1,
                "weight": None,
                "max_weight": None,
                "weight_unit": None,
                "desc_units": False,
                "outer_width": None,
                "outer_depth": None,
                "outer_unit": None,
                "mounting_depth": None,
                "airflow": None,
                "description": "",
                "comments": "",
                "tags": [],
                "custom_fields": {},
                "created": ts,
                "last_updated": ts,
                "device_count": devices_in_rack,
                "powerfeed_count": 0,
            }
            rack_refs[rack] = {"id": i, "url": self._url("racks", i), "display": rack, "name": rack}
            self._add("racks", obj, {"id": i, "name": rack, "site": site["slug"], "site_id": site_id})

        manufacturers: dict[str, dict[str, Any]] = {}

        def manufacturer(name: str) -> dict[str, Any]:
            if name not in manufacturers:
                mid = len(manufacturers) + 1
                obj = {
                    "id": mid,
                    "url": self._url("manufacturers", mid),
                    "display": name,
                    "name": name,
                    "slug": _slug(name),
                    "description": "",
                    "tags": [],
                    "custom_fields": {},
                    "created": ts,
                    "last_updated": ts,
                }
                self._add("manufacturers", obj, {"id": mid, "name": name, "slug": obj["slug"]})
                manufacturers[name] = {
                    "id": mid,
                    "url": obj["url"],
                    "display": name,
                    "name": name,
                    "slug": obj["slug"],
                }
            return manufacturers[name]

        roles: dict[str, dict[str, Any]] = {}
        for rid, (slug, name, color) in enumerate(
            (("gpu-node", "GPU node", "4caf50"), ("leaf-switch", "Leaf switch", "2196f3")), start=1
        ):
            obj = {
                "id": rid,
                "url": self._url("device-roles", rid),
                "display": name,
                "name": name,
                "slug": slug,
                "color": color,
                "vm_role": False,
                "description": "",
                "tags": [],
                "custom_fields": {},
                "created": ts,
                "last_updated": ts,
            }
            self._add("device-roles", obj, {"id": rid, "name": name, "slug": slug})
            roles[slug] = {"id": rid, "url": obj["url"], "display": name, "name": name, "slug": slug}

        device_types: dict[tuple[str, str], dict[str, Any]] = {}

        def device_type(manufacturer_name: str, model: str, u_height: int) -> dict[str, Any]:
            key = (manufacturer_name, model)
            if key not in device_types:
                tid = len(device_types) + 1
                m = manufacturer(manufacturer_name)
                obj = {
                    "id": tid,
                    "url": self._url("device-types", tid),
                    "display": f"{manufacturer_name} {model}",
                    "manufacturer": m,
                    "default_platform": None,
                    "model": model,
                    "slug": _slug(model),
                    "part_number": "",
                    "u_height": float(u_height),
                    "is_full_depth": True,
                    "subdevice_role": None,
                    "airflow": None,
                    "weight": None,
                    "weight_unit": None,
                    "front_image": None,
                    "rear_image": None,
                    "description": "",
                    "comments": "",
                    "tags": [],
                    "custom_fields": {},
                    "created": ts,
                    "last_updated": ts,
                }
                self._add(
                    "device-types",
                    obj,
                    {"id": tid, "model": model, "slug": obj["slug"], "manufacturer": m["slug"]},
                )
                device_types[key] = {
                    "id": tid,
                    "url": obj["url"],
                    "display": obj["display"],
                    "manufacturer": m,
                    "model": model,
                    "slug": obj["slug"],
                }
            return device_types[key]

        device_refs: dict[str, dict[str, Any]] = {}
        iface_ids: dict[tuple[str, str], int] = {}
        iface_types: dict[tuple[str, str], str] = {}
        iface_macs: dict[tuple[str, str], str | None] = {}
        dev_id = 0
        iface_id = 0
        for rack in hall.racks:
            for node in hall.nodes_in_rack(rack):
                dev_id += 1
                dt = device_type(node.manufacturer, node.model, 1)
                baseline = dict(hall.spec.firmware_baseline)
                obj = self._device(
                    dev_id,
                    node.name,
                    dt,
                    roles["gpu-node"],
                    site_ref,
                    rack_refs[rack],
                    node.position,
                    node.serial,
                    custom_fields={
                        "turnup_bmc_url": f"{self.base_url}/dev/{node.name}",
                        "turnup_dcgm_url": f"{self.base_url}/dev/{node.name}",
                        "turnup_switch_url": None,
                    },
                    config_context={"turnup": {"firmware_baseline": baseline, "gpu_count": node.gpu_count}},
                    interface_count=len(node.interfaces),
                    ts=ts,
                )
                self._add(
                    "devices",
                    obj,
                    {
                        "id": dev_id,
                        "name": node.name,
                        "site": site["slug"],
                        "site_id": site_id,
                        "rack": rack,
                        "rack_id": rack_refs[rack]["id"],
                        "role": "gpu-node",
                        "role_id": roles["gpu-node"]["id"],
                        "device_role": "gpu-node",
                        "manufacturer": dt["manufacturer"]["slug"],
                        "device_type": dt["slug"],
                        "device_type_id": dt["id"],
                        "serial": node.serial,
                        "status": "active",
                    },
                )
                device_refs[node.name] = {
                    "id": dev_id,
                    "url": self._url("devices", dev_id),
                    "display": node.name,
                    "name": node.name,
                }
                for iface in node.interfaces:
                    iface_id += 1
                    iface_ids[(node.name, iface)] = iface_id
                    iface_types[(node.name, iface)] = hall.spec.interface_type
                    iface_macs[(node.name, iface)] = node.macs[iface].upper()
            for leaf in hall.leaves_in_rack(rack):
                dev_id += 1
                dt = device_type(leaf.manufacturer, leaf.model, 2)
                obj = self._device(
                    dev_id,
                    leaf.name,
                    dt,
                    roles["leaf-switch"],
                    site_ref,
                    rack_refs[rack],
                    leaf.position,
                    leaf.serial,
                    custom_fields={
                        "turnup_bmc_url": None,
                        "turnup_dcgm_url": None,
                        "turnup_switch_url": f"{self.base_url}/dev/{leaf.name}",
                    },
                    config_context={"turnup": {"lldp_protocol": hall.spec.lldp_protocol}},
                    interface_count=len(leaf.ports),
                    ts=ts,
                )
                self._add(
                    "devices",
                    obj,
                    {
                        "id": dev_id,
                        "name": leaf.name,
                        "site": site["slug"],
                        "site_id": site_id,
                        "rack": rack,
                        "rack_id": rack_refs[rack]["id"],
                        "role": "leaf-switch",
                        "role_id": roles["leaf-switch"]["id"],
                        "device_role": "leaf-switch",
                        "manufacturer": dt["manufacturer"]["slug"],
                        "device_type": dt["slug"],
                        "device_type_id": dt["id"],
                        "serial": leaf.serial,
                        "status": "active",
                    },
                )
                device_refs[leaf.name] = {
                    "id": dev_id,
                    "url": self._url("devices", dev_id),
                    "display": leaf.name,
                    "name": leaf.name,
                }
                for port in leaf.ports:
                    iface_id += 1
                    iface_ids[(leaf.name, port)] = iface_id
                    iface_types[(leaf.name, port)] = hall.spec.interface_type
                    iface_macs[(leaf.name, port)] = None

        # Cables first, so interfaces can reference them.
        cable_by_iface: dict[tuple[str, str], tuple[int, str, tuple[str, str]]] = {}
        cables: list[tuple[int, Any]] = []
        label_by_cid: dict[int, str] = {}
        for cid, cable in enumerate(hall.intent_cables.values(), start=1):
            a = (cable.a.device, cable.a.interface)
            b = (cable.b.device, cable.b.interface)
            cable_by_iface[a] = (cid, "A", b)
            cable_by_iface[b] = (cid, "B", a)
            cables.append((cid, cable))
            label_by_cid[cid] = cable.label

        def iface_ref(dev: str, name: str, cid: int | None) -> dict[str, Any]:
            iid = iface_ids[(dev, name)]
            return {
                "id": iid,
                "url": self._url("interfaces", iid),
                "display": name,
                "device": device_refs[dev],
                "name": name,
                "cable": cid,
                "_occupied": cid is not None,
            }

        for cid, cable in cables:
            a = (cable.a.device, cable.a.interface)
            b = (cable.b.device, cable.b.interface)
            obj = {
                "id": cid,
                "url": self._url("cables", cid),
                "display": cable.label,
                "type": "",
                "a_terminations": [
                    {
                        "object_type": "dcim.interface",
                        "object_id": iface_ids[a],
                        "object": iface_ref(a[0], a[1], cid),
                    }
                ],
                "b_terminations": [
                    {
                        "object_type": "dcim.interface",
                        "object_id": iface_ids[b],
                        "object": iface_ref(b[0], b[1], cid),
                    }
                ],
                "status": {"value": "connected", "label": "Connected"},
                "tenant": None,
                "label": cable.label,
                "color": "",
                "length": None,
                "length_unit": None,
                "description": "",
                "comments": "",
                "tags": [],
                "custom_fields": {"turnup_speed_gbps": cable.speed_gbps},
                "created": ts,
                "last_updated": ts,
            }
            self._add(
                "cables",
                obj,
                {
                    "id": cid,
                    "label": cable.label,
                    "site": site["slug"],
                    "site_id": site_id,
                    "device": [a[0], b[0]],
                    "device_id": [device_refs[a[0]]["id"], device_refs[b[0]]["id"]],
                    "rack": sorted({hall.nodes[a[0]].rack, hall.leaves[b[0]].rack}),
                },
            )

        for (dev, name), iid in iface_ids.items():
            link = cable_by_iface.get((dev, name))
            cid = link[0] if link else None
            peer = link[2] if link else None
            cable_ref = None
            if link and cid is not None:
                cable_ref = {
                    "id": cid,
                    "url": self._url("cables", cid),
                    "display": label_by_cid[cid],
                    "label": label_by_cid[cid],
                }
            peers = [iface_ref(peer[0], peer[1], cid)] if peer else []
            type_slug = iface_types[(dev, name)]
            mac = iface_macs[(dev, name)]
            is_node = dev in hall.nodes
            rack_name = hall.nodes[dev].rack if is_node else hall.leaves[dev].rack
            obj = {
                "id": iid,
                "url": self._url("interfaces", iid),
                "display": name,
                "device": device_refs[dev],
                "vdcs": [],
                "module": None,
                "name": name,
                "label": "",
                "type": {"value": type_slug, "label": INTERFACE_TYPE_LABELS.get(type_slug, type_slug)},
                "enabled": True,
                "parent": None,
                "bridge": None,
                "lag": None,
                "mtu": 9000,
                "mac_address": mac,
                "primary_mac_address": (
                    {
                        "id": iid,
                        "url": f"{self.base_url}/netbox/api/dcim/mac-addresses/{iid}/",
                        "display": mac,
                        "mac_address": mac,
                    }
                    if mac
                    else None
                ),
                "speed": hall.spec.speed_gbps * 1_000_000,
                "duplex": None,
                "wwn": None,
                "mgmt_only": False,
                "description": "",
                "mode": None,
                "rf_role": None,
                "rf_channel": None,
                "poe_mode": None,
                "poe_type": None,
                "rf_channel_frequency": None,
                "rf_channel_width": None,
                "tx_power": None,
                "untagged_vlan": None,
                "tagged_vlans": [],
                "qinq_svlan": None,
                "vlan_translation_policy": None,
                "mark_connected": False,
                "cable": cable_ref,
                "cable_end": link[1] if link else "",
                "wireless_link": None,
                "link_peers": peers,
                "link_peers_type": "dcim.interface" if peers else None,
                "wireless_lans": [],
                "vrf": None,
                "l2vpn_termination": None,
                "connected_endpoints": peers,
                "connected_endpoints_type": "dcim.interface" if peers else None,
                "connected_endpoints_reachable": True if peers else None,
                "tags": [],
                "custom_fields": {},
                "created": ts,
                "last_updated": ts,
                "count_ipaddresses": 0,
                "count_fhrp_groups": 0,
                "_occupied": bool(peers),
            }
            self._add(
                "interfaces",
                obj,
                {
                    "id": iid,
                    "name": name,
                    "device": dev,
                    "device_id": device_refs[dev]["id"],
                    "site": site["slug"],
                    "site_id": site_id,
                    "rack": rack_name,
                    "type": type_slug,
                    "cabled": bool(peers),
                    "mac_address": mac,
                    "role": "gpu-node" if is_node else "leaf-switch",
                },
            )

    def _device(
        self,
        dev_id: int,
        name: str,
        dt: dict[str, Any],
        role: dict[str, Any],
        site_ref: dict[str, Any],
        rack_ref: dict[str, Any],
        position: int,
        serial: str,
        custom_fields: dict[str, Any],
        config_context: dict[str, Any],
        interface_count: int,
        ts: str,
    ) -> dict[str, Any]:
        return {
            "id": dev_id,
            "url": self._url("devices", dev_id),
            "display": name,
            "name": name,
            "device_type": dt,
            "role": role,
            "device_role": role,
            "tenant": None,
            "platform": None,
            "serial": serial,
            "asset_tag": None,
            "site": site_ref,
            "location": None,
            "rack": rack_ref,
            "position": float(position),
            "face": {"value": "front", "label": "Front"},
            "latitude": None,
            "longitude": None,
            "parent_device": None,
            "status": {"value": "active", "label": "Active"},
            "airflow": None,
            "primary_ip": None,
            "primary_ip4": None,
            "primary_ip6": None,
            "oob_ip": None,
            "cluster": None,
            "virtual_chassis": None,
            "vc_position": None,
            "vc_priority": None,
            "description": "",
            "comments": "",
            "config_template": None,
            "config_context": config_context,
            "local_context_data": config_context,
            "tags": [],
            "custom_fields": custom_fields,
            "created": ts,
            "last_updated": ts,
            "console_port_count": 0,
            "console_server_port_count": 0,
            "power_port_count": 0,
            "power_outlet_count": 0,
            "interface_count": interface_count,
            "front_port_count": 0,
            "rear_port_count": 0,
            "device_bay_count": 0,
            "module_bay_count": 0,
            "inventory_item_count": 0,
        }

    def _add(self, collection: str, obj: dict[str, Any], filt: dict[str, Any]) -> None:
        self.objects[collection].append(obj)
        self.filters[collection].append(filt)

    # ----------------------------------------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------------------------------------

    def get(self, collection: str, obj_id: int) -> dict[str, Any] | None:
        for obj in self.objects.get(collection, []):
            if obj["id"] == obj_id:
                return obj
        return None

    def list(
        self,
        collection: str,
        params: Mapping[str, Sequence[str]],
        url_builder: UrlBuilder,
    ) -> dict[str, Any]:
        """Filtered, paginated collection in NetBox's `{count, next, previous, results}` envelope."""
        objs = self.objects[collection]
        filts = self.filters[collection]
        selected = [obj for obj, f in zip(objs, filts, strict=True) if _matches(f, params)]
        limit = _int_param(params, "limit", DEFAULT_LIMIT)
        offset = max(0, _int_param(params, "offset", 0))
        if limit <= 0 or limit > MAX_LIMIT:
            limit = MAX_LIMIT if limit <= 0 else MAX_LIMIT
        count = len(selected)
        page = selected[offset : offset + limit]
        next_url = url_builder(offset + limit, limit) if offset + limit < count else None
        prev_url = None
        if offset > 0:
            prev_offset = offset - limit
            prev_url = url_builder(prev_offset if prev_offset > 0 else None, limit)
        return {"count": count, "next": next_url, "previous": prev_url, "results": page}


def status_body() -> dict[str, Any]:
    """`GET /api/status/`; pynetbox reads `netbox-version` from here."""
    return {
        "django-version": "5.2.6",
        "installed-apps": {
            "django_filters": "25.1",
            "django_rq": "3.0.1",
            "drf_spectacular": "0.28.0",
            "rest_framework": "3.16.0",
        },
        "netbox-version": NETBOX_VERSION,
        "netbox-full-version": NETBOX_VERSION,
        "plugins": {},
        "python-version": "3.12.3",
        "rq-workers-running": 1,
    }


# The apps a NetBox 4.x API root (`GET /api/`) lists, in its order.
API_APPS = (
    "circuits",
    "core",
    "dcim",
    "extras",
    "ipam",
    "status",
    "tenancy",
    "users",
    "virtualization",
    "vpn",
    "wireless",
)


def api_root(base_url: str) -> dict[str, Any]:
    return {app: f"{base_url}/netbox/api/{app}/" for app in API_APPS}


def dcim_root(base_url: str) -> dict[str, Any]:
    return {c: f"{base_url}/netbox/api/dcim/{c}/" for c in COLLECTIONS}


IGNORED_PARAMS = {"limit", "offset", "brief", "ordering", "exclude", "format", "fields", "omit", "depth", "q"}


def _int_param(params: Mapping[str, Sequence[str]], key: str, default: int) -> int:
    values = params.get(key)
    if not values:
        return default
    try:
        return int(values[-1])
    except (TypeError, ValueError):
        return default


def _matches(filt: dict[str, Any], params: Mapping[str, Sequence[str]]) -> bool:
    for key, values in params.items():
        if key in IGNORED_PARAMS or not values:
            continue
        base = key[:-3] if key.endswith("__n") else key
        negate = key.endswith("__n")
        if base not in filt:
            # Unknown filter: NetBox would 400; being permissive keeps odd clients working.
            continue
        actual = filt[base]
        actual_set = {str(v).lower() for v in actual} if isinstance(actual, list) else {str(actual).lower()}
        wanted = {str(v).lower() for v in values}
        hit = bool(actual_set & wanted)
        if hit == negate:
            return False
    return True
