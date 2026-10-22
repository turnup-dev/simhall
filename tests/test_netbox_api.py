# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The minimal NetBox API: shapes the turnup-check adapter reads, pagination, filters, token auth."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from simhall.golden import build_golden
from simhall.server import create_app


@pytest.fixture(scope="module")
def app():
    application = create_app(build_golden)
    application.state.sim.set_base_url("http://sim")
    return application


def test_status_and_roots(app, asgi) -> None:
    assert asgi(app, "GET", "/netbox/api/status/").json()["netbox-version"].startswith("4.")
    root = asgi(app, "GET", "/netbox/api/").json()
    assert root["dcim"] == "http://sim/netbox/api/dcim/"
    dcim = asgi(app, "GET", "/netbox/api/dcim/").json()
    assert dcim["devices"] == "http://sim/netbox/api/dcim/devices/"
    assert asgi(app, "GET", "/netbox/api/dcim/nonsense/").status_code == 404


def test_devices_shape_per_contract_2_1(app, asgi) -> None:
    r = asgi(
        app, "GET", "/netbox/api/dcim/devices/?site=hall-b&limit=1000", headers={"Authorization": "Token any"}
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"count", "next", "previous", "results"}
    assert body["count"] == 20 and body["next"] is None and body["previous"] is None
    node7 = next(d for d in body["results"] if d["name"] == "node7")
    assert node7["role"]["slug"] == "gpu-node" and node7["device_role"]["slug"] == "gpu-node"
    assert node7["device_type"]["model"] == "GB300 NVL72 compute tray"
    assert node7["device_type"]["manufacturer"]["name"] == "NVIDIA"
    assert node7["site"]["slug"] == "hall-b" and node7["rack"]["name"] == "12" and node7["position"] == 8.0
    assert node7["serial"].startswith("SN-N7-") and node7["primary_ip4"] is None
    assert node7["custom_fields"] == {
        "turnup_bmc_url": "http://sim/dev/node7",
        "turnup_dcgm_url": "http://sim/dev/node7",
        "turnup_switch_url": None,
    }
    assert node7["config_context"]["turnup"]["firmware_baseline"] == {
        "bmc": "1.12.0",
        "bios": "2.3.1",
        "nic": "28.43.1014",
    }
    leaf4 = next(d for d in body["results"] if d["name"] == "leaf4")
    assert (
        leaf4["role"]["slug"] == "leaf-switch"
        and leaf4["custom_fields"]["turnup_switch_url"] == "http://sim/dev/leaf4"
    )
    assert (
        leaf4["device_type"]["manufacturer"]["name"] == "NVIDIA" and leaf4["device_type"]["model"] == "SN5600"
    )
    assert asgi(app, "GET", f"/netbox/api/dcim/devices/{node7['id']}/").json()["name"] == "node7"
    assert asgi(app, "GET", "/netbox/api/dcim/devices/9999/").status_code == 404


def test_interfaces_and_cables_shapes(app, asgi) -> None:
    node7 = next(d for d in asgi(app, "GET", "/netbox/api/dcim/devices/?name=node7").json()["results"])
    r = asgi(app, "GET", f"/netbox/api/dcim/interfaces/?device_id={node7['id']}&limit=1000").json()
    assert r["count"] == 16
    mlx5_3 = next(i for i in r["results"] if i["name"] == "mlx5_3")
    assert mlx5_3["device"]["name"] == "node7" and mlx5_3["type"]["value"] == "400gbase-x-osfp"
    assert mlx5_3["mac_address"] == "B8:3F:D2:00:07:03" and mlx5_3["cable"]["label"] == "CBL-12-07-03"
    assert mlx5_3["link_peers"][0]["device"]["name"] == "leaf4" and mlx5_3["link_peers"][0]["name"] == "swp21"
    cables = asgi(app, "GET", "/netbox/api/dcim/cables/?site=hall-b&limit=1000").json()
    assert cables["count"] == 288
    c = next(x for x in cables["results"] if x["label"] == "CBL-12-07-03")
    assert c["a_terminations"][0]["object"]["device"]["name"] == "node7"
    assert c["a_terminations"][0]["object"]["name"] == "mlx5_3"
    assert c["b_terminations"][0]["object"]["device"]["name"] == "leaf4"
    assert c["b_terminations"][0]["object"]["name"] == "swp21"
    assert c["a_terminations"][0]["object_type"] == "dcim.interface"
    assert asgi(app, "GET", f"/netbox/api/dcim/cables/?device_id={node7['id']}").json()["count"] == 16
    assert asgi(app, "GET", "/netbox/api/dcim/interfaces/?site=hall-b&limit=1000").json()["count"] == 608
    assert asgi(app, "GET", "/netbox/api/dcim/interfaces/?site=hall-z").json()["count"] == 0
    assert asgi(app, "GET", "/netbox/api/dcim/racks/?site=hall-b").json()["results"][0]["name"] == "12"
    assert asgi(app, "GET", "/netbox/api/dcim/sites/?slug=hall-b").json()["count"] == 1


def test_pagination_walks_every_page(app, asgi) -> None:
    url: str | None = "/netbox/api/dcim/interfaces/?site=hall-b&limit=50"
    seen: list[int] = []
    pages = 0
    first = None
    while url:
        body = asgi(app, "GET", url).json()
        if first is None:
            first = body
        assert len(body["results"]) <= 50
        seen.extend(i["id"] for i in body["results"])
        pages += 1
        url = body["next"]
        if url:
            parsed = urlparse(url)
            assert parsed.scheme == "http" and parsed.netloc == "sim"
            assert parse_qs(parsed.query)["site"] == ["hall-b"] and parse_qs(parsed.query)["limit"] == ["50"]
            url = parsed.path + "?" + parsed.query
    assert pages == 13 and len(seen) == len(set(seen)) == 608
    assert first is not None and first["previous"] is None
    page2 = asgi(app, "GET", "/netbox/api/dcim/interfaces/?site=hall-b&limit=50&offset=50").json()
    assert "offset" not in parse_qs(urlparse(page2["previous"]).query)
    page3 = asgi(app, "GET", "/netbox/api/dcim/interfaces/?site=hall-b&limit=50&offset=100").json()
    assert parse_qs(urlparse(page3["previous"]).query)["offset"] == ["50"]
    everything = asgi(app, "GET", "/netbox/api/dcim/interfaces/?limit=0").json()
    assert len(everything["results"]) == 608 and everything["next"] is None
    default = asgi(app, "GET", "/netbox/api/dcim/interfaces/").json()
    assert len(default["results"]) == 50
