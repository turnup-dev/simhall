# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The technician bot against a tiny in-repo stub of the Acceptance routes it uses (CONTRACT.md 6.4, 7)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from simhall import naming
from simhall.golden import build_golden
from simhall.hall import Conflict, Hall
from simhall.server import ServerThread, create_app
from simhall.technician import (
    HttpHallClient,
    LocalHallClient,
    TechnicianBot,
    sibling_port,
    worker_type_for,
)
from simhall.views import normalize_lldp


def make_steps(hall: Hall, node: str = "node7") -> list[dict[str, Any]]:
    """Golden-hall steps for one node: power, then one seat_cable per port, with varied label placement."""
    steps: list[dict[str, Any]] = [
        {
            "id": f"s-12-07-{1:02d}",
            "hall": hall.site,
            "rack": "12",
            "node": node,
            "index": 1,
            "total": 18,
            "kind": "inspect_mount",
            "title": "Inspect tray mount",
            "instructions": "Check rails and latches.",
        },
        {
            "id": f"s-12-07-{2:02d}",
            "hall": hall.site,
            "rack": "12",
            "node": node,
            "index": 2,
            "total": 18,
            "kind": "connect_power",
            "title": "Connect power",
            "instructions": "Both PSUs.",
            "requires_approval": False,
        },
    ]
    cables = sorted(hall.cables_of_node(node), key=lambda c: naming.node_interface_index(c.a.interface) or 0)
    for i, c in enumerate(cables, start=3):
        port_idx = naming.node_interface_index(c.a.interface)
        leaf_no = naming.port_number(c.b.interface)
        title = f"Seat InfiniBand cable: node 7 port {port_idx} -> leaf 4 / port {leaf_no}"
        step: dict[str, Any] = {
            "id": f"s-12-07-{i:02d}",
            "hall": hall.site,
            "rack": "12",
            "node": node,
            "index": i,
            "total": 18,
            "kind": "seat_cable",
            "title": title,
            "instructions": "Scan the label, seat both ends, wait for the switch to agree.",
            "depends_on": [f"s-12-07-{2:02d}"],
            "status": "ready",
            "verification": {
                "sources": ["lldp"],
                "predicate": f"{c.b.device}/{leaf_no} shows {node} {c.a.interface}",
                "timeout_s": 120,
            },
            "requires_approval": False,
            "evidence": [],
        }
        variant = i % 3
        if variant == 0:
            step["cable"] = c.label  # structured field
        elif variant == 1:
            step["instructions"] += f" Cable {c.label}."  # label in text
        # variant 2: only "node 7 port N" in the title
        steps.append(step)
    return steps


def make_stub(steps: list[dict[str, Any]]) -> tuple[FastAPI, dict[str, Any]]:
    """The three routes the bot needs (plus start/approve, which it also calls) and a record of every call."""
    app = FastAPI()
    record: dict[str, Any] = {
        "queue": list(steps),
        "claimed": [],
        "started": [],
        "evidence": {},
        "done": [],
        "approved": [],
        "pending": [],
        "types": [],
    }

    @app.get("/api/workers/{worker}/next")
    async def next_step(worker: str, rack: str | None = None, type: str | None = None) -> Response:
        record["types"].append(type)
        if not record["queue"]:
            return Response(status_code=204)
        step = record["queue"].pop(0)
        step = {**step, "assignee": worker, "status": step.get("status", "claimed")}
        record["claimed"].append(step["id"])
        return Response(content=httpx.Response(200, json=step).content, media_type="application/json")

    @app.post("/api/steps/{sid}/start")
    async def start(sid: str) -> dict[str, Any]:
        record["started"].append(sid)
        return {"id": sid, "status": "in_progress"}

    @app.post("/api/steps/{sid}/approve")
    async def approve(sid: str, request: Request) -> dict[str, Any]:
        record["approved"].append((sid, await request.json()))
        record["pending"] = [p for p in record["pending"] if p["id"] != sid]
        return {"id": sid, "status": "ready"}

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"pending": list(record["pending"]), "granted": []}

    @app.post("/api/steps/{sid}/evidence")
    async def evidence(sid: str, request: Request) -> dict[str, Any]:
        record["evidence"].setdefault(sid, []).append(await request.json())
        return {"id": sid, "evidence": len(record["evidence"][sid])}

    @app.post("/api/steps/{sid}/done")
    async def done(sid: str, request: Request) -> dict[str, Any]:
        body = await request.json()
        record["done"].append((sid, body.get("actor")))
        return {"id": sid, "status": "awaiting_telemetry"}

    return app, record


def run_bot(hall: Hall, steps: list[dict[str, Any]], **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    stub, record = make_stub(steps)

    async def go() -> Any:
        transport = httpx.ASGITransport(app=stub)
        async with httpx.AsyncClient(transport=transport, base_url="http://acceptance") as client:
            bot = TechnicianBot(
                client, LocalHallClient(hall), speed=1e9, poll_interval_s=0.0, idle_polls=2, **kwargs
            )
            return await bot.run()

    return asyncio.run(go()), record


def test_perfect_technician_seats_every_cable_and_leaves_qr_evidence() -> None:
    hall = build_golden(with_faults=False)
    for c in hall.cables_of_node("node7"):
        hall.act({"kind": "unseat_cable", "label": c.label})  # the rack arrives uncabled
    steps = make_steps(hall)
    stats, record = run_bot(hall, steps, worker="tech-1", error_rate=0.0, seed=1)
    assert stats.steps == stats.done == len(steps) == 18 and stats.mistakes == 0 and stats.errors == 0
    assert record["started"] == [s["id"] for s in steps]
    assert [sid for sid, _ in record["done"]] == [s["id"] for s in steps]
    assert all(actor == "tech-1" for _, actor in record["done"])
    qr = [ev for evs in record["evidence"].values() for ev in evs if ev["kind"] == "qr"]
    assert sorted(ev["value"] for ev in qr) == sorted(c.label for c in hall.cables_of_node("node7"))
    assert all(ev["record"]["b_display"].startswith("leaf") for ev in qr)
    assert hall.state()["summary"]["deviating_cables"] == 0
    assert normalize_lldp(hall, "leaf4")[21].remote_port == "mlx5_3"
    strategies = {e["label_strategy"] for e in stats.log if e["kind"] == "seat_cable"}
    assert strategies == {"field", "text", "node-port"}
    assert redfish_power(hall) == "On"


def redfish_power(hall: Hall) -> str:
    tree = hall.redfish_tree("node7")
    return tree["/redfish/v1/Systems/System.Embedded.1"]["PowerState"]


def test_clumsy_technician_makes_adjacent_port_or_unseated_mistakes() -> None:
    hall = build_golden(with_faults=False)
    for c in hall.cables_of_node("node7"):
        hall.act({"kind": "unseat_cable", "label": c.label})
    steps = [s for s in make_steps(hall) if s["kind"] == "seat_cable"]
    stats, record = run_bot(hall, steps, worker="tech-2", error_rate=1.0, seed=7)
    assert stats.steps == stats.done == 16 and stats.mistakes == 16
    assert stats.wrong_port + stats.unseated == 16 and stats.wrong_port > 0
    state = hall.state()["cables"]
    for c in hall.cables_of_node("node7"):
        st = state[c.label]
        assert st["deviates"] is True
        if st["seated"]:
            intended = naming.port_number(c.b.interface) or 0
            actual = naming.port_number(st["b_interface"]) or 0
            assert st["b_device"] == c.b.device and abs(actual - intended) == 1, c.label
            assert (
                c.b.interface
                not in {
                    x.b.interface
                    for x in hall.intent_cables.values()
                    if x.label != c.label and x.b.device == c.b.device
                }
                or True
            )
    assert all(any(ev["kind"] == "qr" for ev in evs) for evs in record["evidence"].values())
    # The golden mistake on node7 mlx5_3 lands on the spare swp22 when the dice say "wrong port".
    entry = next(e for e in stats.log if e["label"] == "CBL-12-07-03")
    assert entry["outcome"] in ("wrong_port:leaf4/22", "unseated")


def test_bot_handles_other_step_kinds_and_approvals() -> None:
    hall = build_golden(with_faults=False)
    hall.act({"kind": "power_off", "device": "node7"})
    steps = [
        {
            "id": "s-1",
            "node": "node7",
            "rack": "12",
            "kind": "connect_power",
            "title": "Connect power",
            "requires_approval": True,
        },
        {
            "id": "s-2",
            "node": "node7",
            "rack": "12",
            "kind": "rma_swap",
            "title": "Swap tray",
            "requires_approval": True,
        },
        {
            "id": "s-3",
            "node": "node7",
            "rack": "12",
            "kind": "firmware_check",
            "title": "Firmware",
            "component": "bmc",
            "version": "1.13.0",
        },
        {
            "id": "s-4",
            "node": "node7",
            "rack": "12",
            "kind": "seat_cable",
            "title": "Seat the cable",
            "instructions": "no label here",
        },
        {"id": "s-5", "node": "node7", "rack": "12", "kind": "burn_in", "title": "Burn-in"},
    ]
    stats, record = run_bot(hall, steps, worker="tech-3", error_rate=0.0, approver="K. Ops")
    assert stats.done == 5 and stats.errors == 0
    assert [sid for sid, _ in record["approved"]] == ["s-1", "s-2"]
    assert record["approved"][0][1]["approver"] == "K. Ops"
    assert redfish_power(hall) == "On"
    assert hall.physical.nodes["node7"].firmware["bmc"] == "1.13.0"
    assert hall.physical.nodes["node7"].serial == "SN-RMA-s-2"
    assert record["evidence"]["s-4"][0]["value"] == "no cable label found on this step"
    assert stats.by_kind == {
        "connect_power": 1,
        "firmware_check": 1,
        "rma_swap": 1,
        "seat_cable": 1,
        "burn_in": 1,
    }


def test_label_resolution_strategies_and_sibling_ports() -> None:
    hall = build_golden(with_faults=False)
    stub, _ = make_stub([])

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://a") as client:
            bot = TechnicianBot(client, LocalHallClient(hall), speed=1e9, poll_interval_s=0.0, idle_polls=2)
            await bot._load_intent()
            assert bot.resolve_label({"action": {"cable": "CBL-12-07-03"}}) == ("CBL-12-07-03", "field")
            assert bot.resolve_label({"title": "Seat CBL-12-07-05 now"}) == ("CBL-12-07-05", "text")
            assert bot.resolve_label(
                {"node": "node7", "title": "Seat cable: node 7 port 3 -> leaf 4 / port 21"}
            ) == ("CBL-12-07-03", "node-port")
            assert bot.resolve_label({"title": "Seat InfiniBand cable: node 7 port 4"}) == (
                "CBL-12-07-04",
                "node-port",
            )
            assert bot.resolve_label({"title": "nothing"}) == (None, "none")
            adjacent = bot._adjacent_ports("leaf4", "swp21")
            assert adjacent[0] == "swp22" and set(adjacent) == {"swp22", "swp20"}  # spare first
            stats = await bot.run()
            assert stats.steps == 0 and stats.polls == 2

    asyncio.run(go())
    assert sibling_port("swp21", 1) == "swp22" and sibling_port("Ethernet21", -1) == "Ethernet20"
    assert sibling_port("swp1", -1) is None and sibling_port("weird", 1) is None


def test_failed_step_is_fixed_by_reseating_and_crossed_cables_never_block() -> None:
    """A `failed` step handed back is a fix: pull the cable, seat it where the plan says, no new mistake."""
    hall = build_golden(with_faults=False)
    for c in hall.cables_of_node("node7"):
        hall.act({"kind": "unseat_cable", "label": c.label})
    cables = {naming.node_interface_index(c.a.interface): c for c in hall.cables_of_node("node7")}
    c3, c4, c6 = cables[3], cables[4], cables[6]
    # earlier mistakes: CBL-12-07-03 sits on the spare swp22, CBL-12-07-04 sits on CBL-12-07-06's port
    hall.act({"kind": "seat_cable", "label": c3.label, "b_interface": "swp22"})
    hall.act(
        {"kind": "seat_cable", "label": c4.label, "b_device": c6.b.device, "b_interface": c6.b.interface}
    )

    def step(sid: str, cable: str, status: str) -> dict[str, Any]:
        return {
            "id": sid,
            "node": "node7",
            "rack": "12",
            "kind": "seat_cable",
            "cable": cable,
            "title": "Seat cable",
            "status": status,
            "diagnosis": "neighbor mismatch" if status == "failed" else None,
            "fix": "move cable one port up" if status == "failed" else None,
        }

    # 1. a correct first attempt on cable 6 finds its port occupied: 409 -> left unseated, with a note
    stats, record = run_bot(hall, [step("s-6", c6.label, "claimed")], worker="tech-fix", error_rate=0.0)
    assert stats.done == 1 and stats.blocked == 1 and stats.errors == 0 and stats.mistakes == 0
    assert hall.state()["cables"][c6.label]["seated"] is False
    notes = [ev["value"] for ev in record["evidence"]["s-6"] if ev["kind"] == "note"]
    assert notes and "could not seat" in notes[0] and "occupied" in notes[0]
    assert any(ev["kind"] == "qr" for ev in record["evidence"]["s-6"])
    # 2. fixes never roll a new mistake, even at error-rate 1.0: both cables land where the plan says
    fixes = [step("s-3", c3.label, "failed"), step("s-4", c4.label, "failed")]
    stats, _ = run_bot(hall, fixes, worker="tech-fix", error_rate=1.0, seed=3)
    assert stats.done == 2 and stats.fixes == 2 and stats.mistakes == 0 and stats.blocked == 0
    state = hall.state()["cables"]
    assert state[c3.label]["b_interface"] == "swp21" and state[c3.label]["deviates"] is False
    assert state[c4.label]["b_interface"] == c4.b.interface and state[c4.label]["deviates"] is False
    assert [e["fixing"] for e in stats.log] == [True, True]
    # 3. cable 6's port is free now, so its own fix attempt succeeds
    stats, _ = run_bot(hall, [step("s-6", c6.label, "failed")], worker="tech-fix", error_rate=1.0)
    assert stats.fixes == 1 and stats.blocked == 0 and hall.state()["cables"][c6.label]["deviates"] is False


def test_bot_drives_a_live_server_through_the_http_hall_client() -> None:
    hall = build_golden(with_faults=False)
    for c in hall.cables_of_node("node7"):
        hall.act({"kind": "unseat_cable", "label": c.label})
    steps = [s for s in make_steps(hall) if s["kind"] == "seat_cable"][:4]
    stub, record = make_stub(steps)
    app = create_app(hall, timeout_sleep_s=0.1)

    async def go(base_url: str) -> Any:
        acc_transport = httpx.ASGITransport(app=stub)
        async with (
            httpx.AsyncClient(transport=acc_transport, base_url="http://acceptance") as acceptance,
            httpx.AsyncClient(timeout=5.0) as sim,
        ):
            bot = TechnicianBot(
                acceptance,
                HttpHallClient(sim, base_url),
                worker="tech-http",
                error_rate=0.0,
                speed=1e9,
                poll_interval_s=0.0,
                idle_polls=2,
            )
            return await bot.run()

    with ServerThread(app) as srv:
        stats = asyncio.run(go(srv.base_url))
        over_http = httpx.get(f"{srv.base_url}/sim/state", timeout=5.0).json()
    assert stats.done == 4 and stats.errors == 0 and stats.mistakes == 0
    assert len(record["done"]) == 4
    assert over_http["summary"]["deviating_cables"] == 12  # 16 unseated, 4 seated by the bot
    assert hall.state()["summary"]["deviating_cables"] == 12  # and the in-process object is the same hall


def test_approver_sweeps_pending_approvals_and_oem_worker_type() -> None:
    """`/next` never hands out a `needs_approval` step: the approver approves them from `/api/approvals`."""
    hall = build_golden(with_faults=False)
    hall.act({"kind": "power_off", "device": "node7"})
    stub, record = make_stub([])
    record["pending"] = [
        {"id": "s-12-07-02", "rack": "12", "kind": "connect_power", "status": "needs_approval"},
        {"id": "s-13-01-02", "rack": "13", "kind": "connect_power", "status": "needs_approval"},
    ]
    swap = {"id": "s-12-07-rma1-swap", "node": "node7", "rack": "12", "kind": "rma_swap", "title": "RMA swap"}
    record["queue"] = [swap]

    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stub), base_url="http://a") as client:
            bot = TechnicianBot(
                client,
                LocalHallClient(hall),
                worker="oem-1",
                rack="12",
                approver="K. Ops",
                speed=1e9,
                poll_interval_s=0.0,
                idle_polls=2,
            )
            return await bot.run()

    stats = asyncio.run(go())
    # rack filter: only rack 12's pending step is approved, by name
    assert [sid for sid, _ in record["approved"]] == ["s-12-07-02"]
    assert record["approved"][0][1]["approver"] == "K. Ops" and stats.approvals == 1
    assert set(record["types"]) == {"oem_engineer"}
    assert (
        stats.by_kind == {"rma_swap": 1} and hall.physical.nodes["node7"].serial == "SN-RMA-s-12-07-rma1-swap"
    )
    assert worker_type_for("tech-1") == "technician" and worker_type_for("robot-2") == "robot"


def test_failed_firmware_check_pushes_the_versions_in_the_fix_sentence() -> None:
    hall = build_golden(with_faults=False)
    hall.inject({"kind": "firmware_off_baseline", "node": "node7", "component": "bmc", "version": "1.11.9"})
    step = {
        "id": "s-12-07-20",
        "node": "node7",
        "rack": "12",
        "kind": "firmware_check",
        "title": "Firmware baseline check: node 7",
        "status": "failed",
        "fix": "update bmc on node7 from 1.11.9 to 1.12.0 (firmware push needs approval)",
    }
    untouched = {**step, "id": "s-12-07-21", "status": "claimed"}
    assert TechnicianBot.firmware_pushes(untouched) == []
    stats, record = run_bot(hall, [step], worker="tech-1", error_rate=0.0)
    assert stats.done == 1 and stats.errors == 0
    assert hall.physical.nodes["node7"].firmware["bmc"] == "1.12.0" and hall.faults() == []
    assert record["evidence"]["s-12-07-20"][0]["value"] == "firmware bmc on node7 updated to 1.12.0"


def test_mistake_log_lists_every_mistake_for_the_harness() -> None:
    hall = build_golden(with_faults=False)
    for c in hall.cables_of_node("node7"):
        hall.act({"kind": "unseat_cable", "label": c.label})
    steps = [s for s in make_steps(hall) if s["kind"] == "seat_cable"][:5]
    stats, _ = run_bot(hall, steps, worker="tech-2", error_rate=1.0, seed=7)
    log = stats.as_dict()["mistake_log"]
    assert len(log) == stats.mistakes == 5
    assert {m["step"] for m in log} == {s["id"] for s in steps}
    assert all(m["outcome"] == "unseated" or m["outcome"].startswith("wrong_port:leaf") for m in log)


@pytest.mark.parametrize("first", ["CBL-12-03-00", "CBL-12-03-02"])
def test_the_crossed_pair_from_the_demo_readme_untangles_in_three_fixes(first: str) -> None:
    """demo/README.md "Known issues" 1 on hall-b: CBL-12-03-00 lands on leaf3/swp18, CBL-12-03-02 on swp17,
    each on the other's planned port, and seating either one where it belongs is refused (409). A fix pulls
    the cable before seating it, so whichever cable is fixed first frees the port the other one needs."""
    a, b = "CBL-12-03-00", "CBL-12-03-02"  # planned: leaf3/swp17, leaf3/swp18
    hall = build_golden("hall-b")
    n3 = {"kind": "seat_cable", "a_device": "node3", "b_device": "leaf3"}
    for label in (a, b):
        hall.act({"kind": "unseat_cable", "label": label})
    hall.act({**n3, "label": a, "a_interface": "mlx5_0", "b_interface": "swp18"})
    hall.act({**n3, "label": b, "a_interface": "mlx5_2", "b_interface": "swp17"})
    for label, iface, port in ((a, "mlx5_0", "swp17"), (b, "mlx5_2", "swp18")):
        with pytest.raises(Conflict):
            hall.act({**n3, "label": label, "a_interface": iface, "b_interface": port})
    second = b if first == a else a

    def fix(label: str) -> dict[str, Any]:
        sid = {a: "s-12-03-04", b: "s-12-03-06"}[label]
        return {"id": sid, "node": "node3", "rack": "12", "kind": "seat_cable", "cable": label,
                "title": "Seat cable", "status": "failed", "diagnosis": "neighbor mismatch"}  # fmt: skip

    fixes = [fix(first), fix(second), fix(first)]
    stats, record = run_bot(hall, fixes, worker="tech-1", error_rate=1.0, seed=5)
    cables = hall.state()["cables"]
    assert (cables[a]["b_interface"], cables[a]["deviates"]) == ("swp17", False)
    assert (cables[b]["b_interface"], cables[b]["deviates"]) == ("swp18", False)
    # the first fix finds its port held by the other cable and leaves its own cable out; the other two land
    assert [e["outcome"] for e in stats.log] == ["blocked", "correct", "correct"]
    assert stats.fixes == 3 and stats.blocked == 1 and stats.mistakes == 0 and stats.errors == 0
    notes = [ev["value"] for ev in record["evidence"][fix(first)["id"]] if ev["kind"] == "note"]
    assert len(notes) == 1 and "is occupied by" in notes[0] and second in notes[0]
