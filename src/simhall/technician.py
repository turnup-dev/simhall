# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The technician bot (CONTRACT.md section 6.4).

Pulls steps from the Acceptance API (`GET /api/workers/{worker}/next`), performs them as actions on
the hall (in-process or over `/sim/actions`), submits evidence (`POST /api/steps/{id}/evidence`, a QR
scan of the cable label) and marks the step done (`POST /api/steps/{id}/done`). With probability
`error_rate` a cable step is done wrong: on an adjacent port that is free right now (a spare, or a
neighbour's port whose cable is not seated yet), or left unseated. A step handed back as `failed`
is a fix: the bot reads the verifier's diagnosis, pulls the cable and seats it where the plan says.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from simhall import naming
from simhall.hall import Hall, SimError

log = logging.getLogger("simhall.technician")

NOMINAL_SECONDS: dict[str, float] = {
    "inspect_mount": 120,
    "connect_power": 90,
    "verify_bmc": 30,
    "seat_cable": 60,
    "firmware_check": 120,
    "gpu_check": 60,
    "nvlink_check": 60,
    "coolant_check": 120,
    "burn_in": 600,
    "retest": 60,
    "rma_swap": 900,
}

_NODE_PORT_RE = re.compile(r"node\s*(\d+)\D+port\s*(\d+)", re.IGNORECASE)
_PORT_RE = re.compile(r"\bport\s*(\d+)", re.IGNORECASE)
_NODE_RE = re.compile(r"\bnode\s*(\d+)", re.IGNORECASE)
# the verifier's firmware fix sentence: "update bmc on node1 from 1.11.9 to 1.12.0"
_FW_FIX_RE = re.compile(r"update\s+(\w+)\s+on\s+(\S+)\s+from\s+\S+\s+to\s+([\w.\-]+)", re.IGNORECASE)


def worker_type_for(worker: str) -> str:
    """Worker type the Acceptance dispatcher queues for: `oem-*` -> oem_engineer, `robot-*` -> robot."""
    w = worker.lower()
    if w.startswith("oem"):
        return "oem_engineer"
    if w.startswith("robot"):
        return "robot"
    return "technician"


class HallActionError(Exception):
    """An action the hall refused (unknown object, occupied port, ...)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class HallClient(Protocol):
    async def intent(self) -> dict[str, Any]: ...

    async def act(self, action: dict[str, Any]) -> dict[str, Any]: ...


class LocalHallClient:
    """Drive a `Hall` in the same process."""

    def __init__(self, hall: Hall) -> None:
        self.hall = hall

    async def intent(self) -> dict[str, Any]:
        return self.hall.intent()

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.hall.act(action)
        except SimError as e:
            raise HallActionError(e.status, str(e)) from e
        except ValueError as e:
            raise HallActionError(400, str(e)) from e


class HttpHallClient:
    """Drive a running simhall server through `/sim/intent` and `/sim/actions`."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")

    async def intent(self) -> dict[str, Any]:
        r = await self.client.get(f"{self.base_url}/sim/intent")
        r.raise_for_status()
        return r.json()

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        r = await self.client.post(f"{self.base_url}/sim/actions", json=action)
        if r.status_code >= 400:
            try:
                message = r.json().get("error", r.text)
            except ValueError:
                message = r.text
            raise HallActionError(r.status_code, str(message))
        return r.json()


@dataclass
class BotStats:
    steps: int = 0
    done: int = 0
    mistakes: int = 0
    wrong_port: int = 0
    unseated: int = 0
    fixes: int = 0
    blocked: int = 0
    approvals: int = 0
    errors: int = 0
    polls: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "done": self.done,
            "mistakes": self.mistakes,
            "wrong_port": self.wrong_port,
            "unseated": self.unseated,
            "fixes": self.fixes,
            "blocked": self.blocked,
            "approvals": self.approvals,
            "errors": self.errors,
            "polls": self.polls,
            "by_kind": dict(self.by_kind),
            # every deliberate mistake, so a harness can check each became a failed step with the right fix
            "mistake_log": [
                {k: e.get(k) for k in ("step", "label", "outcome")}
                for e in self.log
                if str(e.get("outcome", "")).startswith(("wrong_port", "unseated"))
            ],
        }


def _find_label_in(obj: Any) -> str | None:
    """A cable label in any `cable`/`label` field of a (nested) step object."""
    if isinstance(obj, dict):
        for key in ("cable", "label"):
            v = obj.get(key)
            if isinstance(v, str) and naming.parse_cable_label(v):
                return naming.find_cable_labels(v)[0]
        for v in obj.values():
            if isinstance(v, dict | list):
                found = _find_label_in(v)
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_label_in(v)
            if found:
                return found
    return None


def sibling_port(ifname: str, delta: int) -> str | None:
    """`swp21`, +1 -> `swp22`; `Ethernet21`, -1 -> `Ethernet20`; None below port 1."""
    m = re.match(r"^(.*?)(\d+)$", ifname)
    if not m:
        return None
    n = int(m.group(2)) + delta
    if n < 1:
        return None
    return f"{m.group(1)}{n}"


class TechnicianBot:
    """One simulated technician. Create, then `await bot.run()`; returns `BotStats`."""

    def __init__(
        self,
        acceptance: httpx.AsyncClient,
        hall: HallClient,
        *,
        worker: str = "tech-1",
        error_rate: float = 0.05,
        speed: float = 20.0,
        rack: str | None = None,
        seed: int | None = None,
        approver: str | None = None,
        max_steps: int | None = None,
        idle_polls: int = 5,
        poll_interval_s: float = 1.0,
        acceptance_base: str = "",
        worker_type: str | None = None,
    ) -> None:
        self.acceptance = acceptance
        self.worker_type = worker_type or worker_type_for(worker)
        self.hall = hall
        self.worker = worker
        self.error_rate = max(0.0, min(1.0, error_rate))
        self.speed = max(speed, 1e-6)
        self.rack = rack
        self.rng = random.Random(seed)
        self.approver = approver
        self.max_steps = max_steps
        self.idle_polls = max(1, idle_polls)
        self.poll_interval_s = poll_interval_s
        self.base = acceptance_base.rstrip("/")
        self.stats = BotStats()
        self._cables: dict[str, dict[str, Any]] = {}
        self._by_node_iface: dict[tuple[str, str], str] = {}
        self._used_leaf_ports: set[tuple[str, str]] = set()
        self._leaf_ports: dict[str, set[str]] = {}
        self._node_by_index: dict[int, str] = {}

    # ------------------------------------------------------------------------------------------

    async def _load_intent(self) -> None:
        intent = await self.hall.intent()
        for c in intent.get("cables", []):
            label = c["label"]
            self._cables[label] = c
            self._by_node_iface[(c["a"]["device"], c["a"]["interface"])] = label
            self._used_leaf_ports.add((c["b"]["device"], c["b"]["interface"]))
        for d in intent.get("devices", []):
            if d.get("role") == "leaf-switch":
                self._leaf_ports[d["name"]] = {i["name"] for i in d.get("interfaces", [])}
            elif d.get("role") == "gpu-node":
                m = re.search(r"(\d+)$", d["name"])
                if m:
                    self._node_by_index[int(m.group(1))] = d["name"]

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    async def run(self) -> BotStats:
        await self._load_intent()
        idle = 0
        while True:
            if self.max_steps is not None and self.stats.steps >= self.max_steps:
                break
            self.stats.polls += 1
            await self.approve_pending()
            params: dict[str, str] = {"rack": self.rack} if self.rack else {}
            if self.worker_type != "technician":
                params["type"] = self.worker_type
            try:
                r = await self.acceptance.get(
                    self._url(f"/api/workers/{self.worker}/next"), params=params or None
                )
            except httpx.HTTPError as e:
                self.stats.errors += 1
                log.warning("acceptance unreachable: %s", e)
                idle += 1
                if idle >= self.idle_polls:
                    break
                await asyncio.sleep(self.poll_interval_s)
                continue
            if r.status_code == 204 or (r.status_code == 200 and not r.content.strip()):
                idle += 1
                if idle >= self.idle_polls:
                    break
                await asyncio.sleep(self.poll_interval_s)
                continue
            if r.status_code != 200:
                self.stats.errors += 1
                log.warning("next returned %s: %s", r.status_code, r.text[:200])
                idle += 1
                if idle >= self.idle_polls:
                    break
                await asyncio.sleep(self.poll_interval_s)
                continue
            idle = 0
            step = r.json()
            if not isinstance(step, dict) or "id" not in step:
                self.stats.errors += 1
                continue
            await self.perform(step)
        return self.stats

    # ------------------------------------------------------------------------------------------

    async def approve_pending(self) -> int:
        """With `approver` set, approve every step waiting on a named approval (power, firmware, coolant,
        RMA swap) as that person. `/next` never hands out a `needs_approval` step, so without this sweep
        gated steps would wait for the owner's click in `/owner`."""
        if not self.approver:
            return 0
        try:
            r = await self.acceptance.get(self._url("/api/approvals"))
        except httpx.HTTPError:
            return 0
        if r.status_code != 200:
            return 0
        approved = 0
        for st in r.json().get("pending", []):
            if self.rack and str(st.get("rack")) != self.rack:
                continue
            res = await self._post(
                f"/api/steps/{st['id']}/approve",
                {"approver": self.approver, "note": "approved for simulation"},
            )
            if res is not None and res.status_code == 200:
                approved += 1
        self.stats.approvals += approved
        return approved

    async def perform(self, step: dict[str, Any]) -> None:
        sid = step["id"]
        kind = str(step.get("kind", "unknown"))
        self.stats.steps += 1
        self.stats.by_kind[kind] = self.stats.by_kind.get(kind, 0) + 1
        if step.get("requires_approval") and self.approver and not step.get("approval"):
            await self._post(
                f"/api/steps/{sid}/approve", {"approver": self.approver, "note": "approved for simulation"}
            )
        await self._post(f"/api/steps/{sid}/start", {"actor": self.worker})
        await asyncio.sleep(NOMINAL_SECONDS.get(kind, 60) / self.speed)
        evidence: list[dict[str, Any]] = []
        entry: dict[str, Any] = {"step": sid, "kind": kind, "node": step.get("node")}
        try:
            check_kind = str(((step.get("verification") or {}).get("check") or {}).get("kind", ""))
            if kind == "seat_cable" or (kind == "retest" and check_kind in ("", "lldp_link")):
                await self._do_cable(step, evidence, entry)
            elif kind == "retest":
                # a retest of a node-health step after a swap: nothing to touch, the verifier re-reads
                what = step.get("retest_of") or check_kind
                evidence.append(self._note(f"retest of {what} on {step.get('node')}"))
            elif kind == "connect_power":
                await self.hall.act({"kind": "power_on", "device": step["node"], "actor": self.worker})
                evidence.append(self._note(f"power connected and host powered on: {step['node']}"))
            elif kind == "rma_swap":
                res = await self.hall.act(
                    {
                        "kind": "replace_node",
                        "device": step["node"],
                        "new_serial": f"SN-RMA-{sid}",
                        "actor": self.worker,
                    }
                )
                evidence.append(
                    self._note(f"tray replaced: {res.get('old_serial')} -> {res.get('new_serial')}")
                )
            elif kind == "firmware_check" and self.firmware_pushes(step):
                for device, component, version in self.firmware_pushes(step):
                    await self.hall.act(
                        {
                            "kind": "update_firmware",
                            "device": device,
                            "component": component,
                            "version": version,
                            "actor": self.worker,
                        }
                    )
                    evidence.append(self._note(f"firmware {component} on {device} updated to {version}"))
            else:
                evidence.append(self._note(f"{kind} performed on {step.get('node') or step.get('rack')}"))
        except HallActionError as e:
            self.stats.errors += 1
            entry["error"] = str(e)
            evidence.append(self._note(f"could not complete: {e}"))
        for ev in evidence:
            await self._post(f"/api/steps/{sid}/evidence", ev)
        await self._post(f"/api/steps/{sid}/done", {"actor": self.worker})
        self.stats.done += 1
        self.stats.log.append(entry)

    @staticmethod
    def firmware_pushes(step: dict[str, Any]) -> list[tuple[str, str, str]]:
        """(device, component, version) to push: explicit `component`/`version` on the step, else the
        verifier's fix sentence on a failed firmware check (`update bmc on node1 from 1.11.9 to 1.12.0`)."""
        if step.get("component") and step.get("version") and step.get("node"):
            return [(str(step["node"]), str(step["component"]), str(step["version"]))]
        if str(step.get("status", "")) != "failed":
            return []
        return [
            (m.group(2), m.group(1).lower(), m.group(3))
            for m in _FW_FIX_RE.finditer(str(step.get("fix") or ""))
        ]

    def _note(self, text: str) -> dict[str, Any]:
        return {"kind": "note", "value": text, "actor": self.worker}

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response | None:
        try:
            r = await self.acceptance.post(self._url(path), json=body)
        except httpx.HTTPError as e:
            self.stats.errors += 1
            log.warning("POST %s failed: %s", path, e)
            return None
        if r.status_code >= 400:
            log.debug("POST %s -> %s", path, r.status_code)
        return r

    # ------------------------------------------------------------------------------------------

    def resolve_label(self, step: dict[str, Any]) -> tuple[str | None, str]:
        """The cable label a step is about and which strategy found it."""
        found = _find_label_in(step)
        if found:
            return found, "field"
        text = " ".join(str(step.get(k, "")) for k in ("title", "instructions", "description"))
        labels = naming.find_cable_labels(text)
        if labels:
            return labels[0], "text"
        node = step.get("node")
        m = _NODE_PORT_RE.search(text)
        port_idx: int | None = None
        if m:
            port_idx = int(m.group(2))
            if not node:
                node = self._node_by_index.get(int(m.group(1)))
        else:
            pm = _PORT_RE.search(text)
            if pm:
                port_idx = int(pm.group(1))
            if not node:
                nm = _NODE_RE.search(text)
                if nm:
                    node = self._node_by_index.get(int(nm.group(1)))
        if node and port_idx is not None:
            label = self._by_node_iface.get((str(node), naming.node_interface(port_idx)))
            if label:
                return label, "node-port"
        return None, "none"

    def _adjacent_ports(self, leaf: str, port: str) -> list[str]:
        """Neighbouring ports (+1, -1) that exist on `leaf`, spare ports first.

        A spare port is free in the intent; a neighbour's port is free only until that cable is seated
        (the hall answers 409 once it is), so the verifier sees a real `wrong_port` either way.
        """
        ports = self._leaf_ports.get(leaf, set())
        spare: list[str] = []
        taken: list[str] = []
        for delta in (1, -1):
            cand = sibling_port(port, delta)
            if cand and cand in ports:
                (taken if (leaf, cand) in self._used_leaf_ports else spare).append(cand)
        self.rng.shuffle(spare)
        self.rng.shuffle(taken)
        return spare + taken

    async def _do_cable(
        self, step: dict[str, Any], evidence: list[dict[str, Any]], entry: dict[str, Any]
    ) -> None:
        label, strategy = self.resolve_label(step)
        entry["label"], entry["label_strategy"] = label, strategy
        if label is None:
            evidence.append(self._note("no cable label found on this step"))
            return
        cable = self._cables.get(label)
        if cable is None:
            evidence.append(self._note(f"label {label} is not in the cable schedule"))
            return
        a_dev, a_if = cable["a"]["device"], cable["a"]["interface"]
        b_dev, b_if = cable["b"]["device"], cable["b"]["interface"]
        fixing = str(step.get("status", "")) == "failed"
        entry["fixing"] = fixing
        mistake = (not fixing) and self.rng.random() < self.error_rate
        outcome = "correct"
        if mistake:
            self.stats.mistakes += 1
            if self.rng.random() < 0.7:
                for cand in self._adjacent_ports(b_dev, b_if):
                    try:
                        await self.hall.act(
                            {
                                "kind": "seat_cable",
                                "label": label,
                                "a_device": a_dev,
                                "a_interface": a_if,
                                "b_device": b_dev,
                                "b_interface": cand,
                                "actor": self.worker,
                            }
                        )
                    except HallActionError:
                        continue
                    outcome = f"wrong_port:{naming.display(b_dev, cand)}"
                    self.stats.wrong_port += 1
                    break
            if outcome == "correct":
                try:
                    await self.hall.act({"kind": "unseat_cable", "label": label, "actor": self.worker})
                except HallActionError:
                    pass
                outcome = "unseated"
                self.stats.unseated += 1
        else:
            if fixing:
                self.stats.fixes += 1
                # Pull the cable first: a cable that landed on a neighbour's port frees that port even when
                # its own port is still occupied, so two crossed cables can never block each other.
                try:
                    await self.hall.act({"kind": "unseat_cable", "label": label, "actor": self.worker})
                except HallActionError:
                    pass
            try:
                await self.hall.act(
                    {
                        "kind": "seat_cable",
                        "label": label,
                        "a_device": a_dev,
                        "a_interface": a_if,
                        "b_device": b_dev,
                        "b_interface": b_if,
                        "actor": self.worker,
                    }
                )
            except HallActionError as e:
                if e.status != 409:
                    raise
                # the intended port holds another cable (someone else's mistake): leave ours unseated, say so
                outcome = "blocked"
                self.stats.blocked += 1
                evidence.append(self._note(f"could not seat {label} at {naming.display(b_dev, b_if)}: {e}"))
        entry["outcome"] = outcome
        try:
            scan = await self.hall.act({"kind": "scan_qr", "label": label, "actor": self.worker})
            record = scan.get("record", {})
        except HallActionError:
            record = {}
        evidence.append({"kind": "qr", "value": label, "actor": self.worker, "record": record})


async def run_bot(
    acceptance_url: str,
    hall: HallClient | None = None,
    *,
    sim_url: str | None = None,
    worker: str = "tech-1",
    error_rate: float = 0.05,
    speed: float = 20.0,
    rack: str | None = None,
    seed: int | None = None,
    approver: str | None = None,
    max_steps: int | None = None,
    idle_polls: int = 5,
    poll_interval_s: float = 1.0,
    timeout_s: float = 10.0,
) -> BotStats:
    """CLI entry: run one bot against a live Acceptance API and a live (or in-process) hall."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        hall_client: HallClient = hall or HttpHallClient(client, sim_url or "http://127.0.0.1:8900")
        bot = TechnicianBot(
            client,
            hall_client,
            worker=worker,
            error_rate=error_rate,
            speed=speed,
            rack=rack,
            seed=seed,
            approver=approver,
            max_steps=max_steps,
            idle_polls=idle_polls,
            poll_interval_s=poll_interval_s,
            acceptance_base=acceptance_url,
        )
        return await bot.run()
