# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The real server on a free 127.0.0.1 port: a resource per protocol, every fault mode, the control plane."""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import pytest

from simhall.golden import GOLDEN_CABLE, build_golden, golden_cables, golden_spec
from simhall.hall import Hall
from simhall.server import ServerThread, create_app

TIMEOUT_SLEEP = 0.3


@pytest.fixture(scope="module")
def server() -> Iterator[ServerThread]:
    app = create_app(build_golden, timeout_sleep_s=TIMEOUT_SLEEP)
    with ServerThread(app) as srv:
        yield srv


@pytest.fixture(scope="module")
def client(server: ServerThread) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=server.base_url, timeout=5.0, auth=("admin", "password")) as c:
        yield c


def test_index_and_health(client: httpx.Client, server: ServerThread) -> None:
    index = client.get("/").json()
    assert index["site"] == "hall-b" and index["golden"] == "hall-b" and index["base_url"] == server.base_url
    assert client.get("/sim/health").json()["ok"] is True
    spec = client.get("/sim/spec").json()
    assert spec["timeout_sleep_s"] == TIMEOUT_SLEEP and spec["spec"]["nodes_per_rack"] == 18


def test_redfish_over_http(client: httpx.Client) -> None:
    root = client.get("/dev/node7/redfish/v1/")
    assert root.status_code == 200 and root.headers["odata-version"] == "4.0"
    systems = client.get("/dev/node7" + root.json()["Systems"]["@odata.id"]).json()
    system = client.get("/dev/node7" + systems["Members"][0]["@odata.id"]).json()
    assert (
        system["HostName"] == "node7" and system["PowerState"] == "On" and system["Status"]["Health"] == "OK"
    )
    inventory = client.get("/dev/node7/redfish/v1/UpdateService/FirmwareInventory").json()
    versions = {client.get("/dev/node7" + m["@odata.id"]).json()["Name"]: None for m in inventory["Members"]}
    assert set(versions) == {"BMC Firmware", "System BIOS", "ConnectX-8 NIC Firmware"}
    port = client.get(
        "/dev/node7/redfish/v1/Chassis/Chassis.Embedded.1/NetworkAdapters/NetworkAdapter_1/Ports/mlx5_3"
    ).json()
    assert port["Ethernet"]["LLDPReceive"]["PortId"] == "swp22"  # the golden fault
    assert client.get("/dev/node7/redfish/v1/Nope").status_code == 404
    assert client.post("/dev/node7/redfish/v1/Systems").status_code == 405
    assert client.get("/dev/node7/redfish").json() == {"v1": "/redfish/v1/"}
    assert client.get("/dev/node99/redfish/v1/").status_code == 404


def test_nvue_and_dcgm_over_http(client: httpx.Client) -> None:
    table = client.get("/dev/leaf4/nvue_v1/interface", params={"rev": "applied"}).json()
    assert table["swp22"]["lldp"]["neighbor"]["node7"]["port"]["id"] == "mlx5_3"
    assert "lldp" not in table["swp21"] and table["swp21"]["link"]["state"] == {"down": {}}
    one = client.get("/dev/leaf4/nvue_v1/interface/swp22?rev=applied").json()
    assert one == table["swp22"]
    assert client.get("/dev/leaf4/nvue_v1/system").json()["hostname"] == "leaf4"
    assert client.post("/dev/leaf4/command-api", json={}).status_code == 404  # not an Arista
    metrics = client.get("/dev/node7/metrics")
    assert metrics.status_code == 200 and metrics.headers["content-type"].startswith(
        "text/plain; version=0.0.4"
    )
    assert metrics.text.count("DCGM_FI_DEV_GPU_TEMP{") == 4


def test_eapi_over_http() -> None:
    spec = golden_spec().model_copy(update={"switch_vendor": "arista"})
    hall = Hall.generate(spec, cables=golden_cables(spec))
    app = create_app(hall)
    assert app.state.sim.hall is hall  # the passed hall is served, not a copy
    with ServerThread(app) as srv, httpx.Client(base_url=srv.base_url, timeout=5.0) as c:
        assert hall.base_url == srv.base_url
        hall.inject({"kind": "missing_link", "cable": "CBL-12-07-05"})  # in-process change ...
        assert (
            c.get("/sim/state").json()["cables"]["CBL-12-07-05"]["seated"] is False
        )  # ... visible over HTTP
        hall.clear_all()
        body = {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {
                "version": 1,
                "cmds": ["show lldp neighbors detail", "show interfaces status"],
                "format": "json",
            },
            "id": "turnup",
        }
        r = c.post("/dev/leaf4/command-api", json=body, auth=("admin", "x"))
        assert r.status_code == 200
        result = r.json()["result"]
        info = result[0]["lldpNeighbors"]["Ethernet21"]["lldpNeighborInfo"][0]
        assert info["systemName"] == "node7" and info["neighborInterfaceInfo"]["interfaceId"] == "mlx5_3"
        assert result[1]["interfaceStatuses"]["Ethernet21"] == {
            **result[1]["interfaceStatuses"]["Ethernet21"],
            "linkStatus": "connected",
            "bandwidth": 400000000000,
        }
        assert c.get("/dev/leaf4/nvue_v1/interface").status_code == 404
        assert c.get("/sim/intent").json()["devices"][-1]["mgmt"]["protocol"] == "eapi"


def test_netbox_over_http_points_back_at_the_server(client: httpx.Client, server: ServerThread) -> None:
    r = client.get(
        "/netbox/api/dcim/devices/",
        params={"site": "hall-b", "limit": 5},
        headers={"Authorization": "Token t"},
    )
    body = r.json()
    assert body["count"] == 20 and body["next"].startswith(server.base_url + "/netbox/api/dcim/devices/?")
    assert body["results"][0]["custom_fields"]["turnup_bmc_url"] == f"{server.base_url}/dev/node1"
    assert body["results"][0]["url"] == f"{server.base_url}/netbox/api/dcim/devices/1/"
    intent = client.get("/sim/intent").json()
    assert intent["devices"][0]["mgmt"]["bmc_url"] == f"{server.base_url}/dev/node1"
    assert client.get("/netbox/api/dcim/cables/?site=hall-b&limit=1000").json()["count"] == 288


def test_fault_modes_over_http(client: httpx.Client) -> None:
    before = len(client.get("/sim/faults").json())
    r = client.post("/sim/faults", json={"kind": "unreachable_node", "node": "node2", "mode": "timeout"})
    assert r.status_code == 201 and r.json()["id"].startswith("flt-")
    timeout_id = r.json()["id"]
    t0 = time.time()
    with pytest.raises(httpx.TimeoutException):
        client.get("/dev/node2/redfish/v1/", timeout=0.05)
    assert time.time() - t0 < TIMEOUT_SLEEP
    client.post("/sim/faults", json={"kind": "unreachable_node", "node": "node3", "mode": "http_500"})
    assert client.get("/dev/node3/redfish/v1/Systems").status_code == 500
    assert client.get("/dev/node3/metrics").status_code == 500
    client.post("/sim/faults", json={"kind": "unreachable_node", "node": "node4", "mode": "refused"})
    assert client.get("/dev/node4/redfish/v1/").status_code == 404
    client.post("/sim/faults", json={"kind": "partial_redfish", "node": "node5"})
    assert client.get("/dev/node5/redfish/v1/UpdateService/FirmwareInventory").status_code == 404
    assert client.get("/dev/node5/redfish/v1/UpdateService/FirmwareInventory/BMC").status_code == 404
    assert client.get("/dev/node5/redfish/v1/Systems/System.Embedded.1").status_code == 200
    client.post(
        "/sim/faults", json={"kind": "unreachable_node", "node": "node6", "mode": "http_500", "scope": "dcgm"}
    )
    assert (
        client.get("/dev/node6/metrics").status_code == 500
        and client.get("/dev/node6/redfish/v1/").status_code == 200
    )
    assert len(client.get("/sim/faults").json()) == before + 5
    assert client.delete(f"/sim/faults/{timeout_id}").status_code == 200
    assert client.get("/dev/node2/redfish/v1/").status_code == 200
    assert client.delete("/sim/faults/flt-9999").status_code == 404
    # Validation and conflicts never 500.
    assert client.post("/sim/faults", json={"kind": "nope"}).status_code == 400
    assert client.post("/sim/faults", json={"kind": "wrong_port", "cable": "CBL-12-07-03"}).status_code == 400
    assert (
        client.post("/sim/faults", json={"kind": "missing_link", "cable": "CBL-00-00-00"}).status_code == 404
    )
    r = client.post(
        "/sim/faults",
        json={"kind": "wrong_port", "cable": "CBL-12-01-00", "to_device": "leaf4", "to_interface": "swp22"},
    )
    assert r.status_code == 409 and "occupied" in r.json()["error"]
    assert (
        client.post(
            "/sim/faults", content=b"not json", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )
    time.sleep(TIMEOUT_SLEEP)  # let the stalled request finish before the next test


def test_actions_state_and_reset_over_http(client: httpx.Client) -> None:
    r = client.post(
        "/sim/actions", json={"kind": "seat_cable", "label": GOLDEN_CABLE, "b_interface": "swp21"}
    )
    assert (
        r.status_code == 200
        and r.json()["matches_intent"] is True
        and "flt-0001" in r.json()["retired_faults"]
    )
    assert (
        client.get("/dev/leaf4/nvue_v1/interface").json()["swp21"]["lldp"]["neighbor"]["node7"]["port"]["id"]
        == "mlx5_3"
    )
    state = client.get("/sim/state").json()
    assert (
        state["cables"][GOLDEN_CABLE]["b_interface"] == "swp21"
        and state["cables"][GOLDEN_CABLE]["deviates"] is False
    )
    assert (
        client.post("/sim/actions", json={"kind": "scan_qr", "label": GOLDEN_CABLE}).json()["record"][
            "b_display"
        ]
        == "leaf4/21"
    )
    assert client.post("/sim/actions", json={"kind": "seat_cable"}).status_code == 400
    assert client.post("/sim/actions", json={"kind": "power_on", "device": "ghost"}).status_code == 404
    occupied = client.post(
        "/sim/actions", json={"kind": "seat_cable", "label": "CBL-12-07-04", "b_interface": "swp21"}
    )
    assert occupied.status_code == 409
    assert isinstance(client.get("/sim/history").json(), list)
    reset = client.post("/sim/reset").json()
    assert reset["ok"] and [f["kind"] for f in reset["faults"]] == [
        "wrong_port", "link_degraded", "firmware_off_baseline", "gpu_unhealthy", "unreachable_node"
    ]
    assert (
        client.get("/dev/leaf4/nvue_v1/interface").json()["swp22"]["lldp"]["neighbor"]["node7"]["port"]["id"]
        == "mlx5_3"
    )
    assert client.get("/sim/health").json()["resets"] == 1
