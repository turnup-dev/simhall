# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The virtual hall: ground-truth intent plus a physical state that faults and actions change.

Model: the *base* physical state is what technicians have physically done (initially: everything
built exactly to intent). The *derived* state is the base with every active fault applied in
insertion order. `clear(id)` removes a fault and re-derives, so clearing restores truth exactly in
any order. Actions mutate the base and retire the faults they physically resolve (a cable seated by
hand is no longer "on the wrong port").
"""

from __future__ import annotations

import copy
import json
import random
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from simhall import naming
from simhall.actions import Action
from simhall.emulators import dcgm as dcgm_emu
from simhall.emulators import lldp as lldp_emu
from simhall.emulators import redfish as redfish_emu
from simhall.faults import Fault
from simhall.models import (
    DEFAULT_GPU_VBIOS,
    INTENT_SCHEMA,
    CableSpec,
    CableState,
    Endpoint,
    GpuState,
    HallSpec,
    LeafIntent,
    LeafState,
    LogEntry,
    NodeIntent,
    NodeState,
    PhysicalState,
    gpu_fb_total_mib,
)

NODE_ROLE = "gpu-node"
LEAF_ROLE = "leaf-switch"


class SimError(Exception):
    """Base for hall errors; `status` is the HTTP status the server maps it to."""

    status = 400


class UnknownObject(SimError):
    status = 404


class Conflict(SimError):
    status = 409


@dataclass(frozen=True)
class LeafPortView:
    """What one leaf port physically shows: link state and the LLDP neighbor, if any."""

    leaf: str
    port: str
    number: int
    link_up: bool
    speed_gbps: int | None
    label: str | None
    neighbor_system: str | None
    neighbor_chassis: str | None
    neighbor_port_id: str | None
    neighbor_port_is_mac: bool
    neighbor_iface: str | None


@dataclass(frozen=True)
class NodePortView:
    """What one node NIC port physically shows (Redfish `Ports/<id>`)."""

    node: str
    iface: str
    mac: str
    link_status: str
    speed_gbps: int
    label: str | None
    peer_system: str | None
    peer_port: str | None
    peer_chassis: str | None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redfish_time(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Hall:
    """One simulated site. Build with `Hall.generate(spec)`; never construct directly."""

    def __init__(
        self,
        spec: HallSpec,
        racks: list[str],
        nodes: dict[str, NodeIntent],
        leaves: dict[str, LeafIntent],
        cables: list[CableSpec],
        generated_at: str,
        base: PhysicalState,
        golden: str | None = None,
    ) -> None:
        self.spec = spec
        self.racks = racks
        self.nodes = nodes
        self.leaves = leaves
        self.intent_cables: dict[str, CableSpec] = {c.label: c for c in cables}
        self.generated_at = generated_at
        self.golden = golden
        self._base_url = spec.base_url
        self._base = base
        self._faults: list[Fault] = []
        self._fault_seq = 0
        self._history: list[dict[str, Any]] = []
        self._phys: PhysicalState = base
        self._derive()

    # ----------------------------------------------------------------------------------------------
    # Generation
    # ----------------------------------------------------------------------------------------------

    @classmethod
    def generate(
        cls,
        spec: HallSpec,
        cables: Iterable[CableSpec | dict[str, Any]] | None = None,
        golden: str | None = None,
    ) -> Hall:
        """Build the ground truth for `spec`. `cables` overrides the cabling rule with an explicit list."""
        rng = random.Random(spec.seed)
        racks = [str(spec.rack_start + r) for r in range(spec.racks)]
        gpu_count = spec.effective_gpu_count
        nodes: dict[str, NodeIntent] = {}
        leaves: dict[str, LeafIntent] = {}
        rack_nodes: dict[str, list[NodeIntent]] = {r: [] for r in racks}
        rack_leaves: dict[str, list[LeafIntent]] = {r: [] for r in racks}
        for r, rack in enumerate(racks):
            for i in range(1, spec.nodes_per_rack + 1):
                n = r * spec.nodes_per_rack + i
                name = spec.node_name_fmt.format(n=n, i=i, rack=rack)
                if name in nodes:
                    raise SimError(f"node_name_fmt {spec.node_name_fmt!r} does not produce unique names")
                serial = f"SN-N{n}-{rng.randrange(10**6):06d}"
                ifaces = tuple(naming.node_interface(p) for p in range(spec.ports_per_node))
                macs = {iface: naming.node_mac(n, p) for p, iface in enumerate(ifaces)}
                uuids = tuple(
                    f"GPU-{uuid.UUID(int=rng.getrandbits(128), version=4)}" for _ in range(gpu_count)
                )
                node = NodeIntent(
                    name=name,
                    rack=rack,
                    index_in_rack=i,
                    global_index=n,
                    position=i + 1,
                    serial=serial,
                    model=spec.node_model,
                    manufacturer=spec.node_manufacturer,
                    gpu_count=gpu_count,
                    interfaces=ifaces,
                    macs=macs,
                    chassis_mac=naming.node_mac(n, 0),
                    gpu_uuids=uuids,
                )
                nodes[name] = node
                rack_nodes[rack].append(node)
            for j in range(spec.leaves_per_rack):
                number = spec.leaf_start + r * spec.leaves_per_rack + j
                name = spec.leaf_name_fmt.format(n=number, i=j + 1, rack=rack)
                if name in leaves or name in nodes:
                    raise SimError(f"leaf_name_fmt {spec.leaf_name_fmt!r} does not produce unique names")
                ports = tuple(
                    naming.leaf_port_name(spec.switch_vendor, k) for k in range(1, spec.leaf_ports + 1)
                )
                leaf = LeafIntent(
                    name=name,
                    rack=rack,
                    index_in_rack=j + 1,
                    number=number,
                    position=spec.nodes_per_rack + 3 + j,
                    serial=f"SN-L{number}-{rng.randrange(10**6):06d}",
                    model=spec.effective_leaf_model,
                    manufacturer=spec.leaf_manufacturer,
                    vendor=spec.switch_vendor,
                    ports=ports,
                    chassis_mac=naming.leaf_mac(number),
                )
                leaves[name] = leaf
                rack_leaves[rack].append(leaf)

        if cables is None:
            cable_list = cls.default_cables(spec, racks, rack_nodes, rack_leaves)
        else:
            cable_list = [c if isinstance(c, CableSpec) else CableSpec.model_validate(c) for c in cables]
            cls._validate_cables(cable_list, nodes, leaves)

        generated_at = spec.generated_at or _now_iso()
        base = cls._initial_state(spec, nodes, leaves, cable_list, generated_at, rng)
        return cls(spec, racks, nodes, leaves, cable_list, generated_at, base, golden=golden)

    @staticmethod
    def default_cables(
        spec: HallSpec,
        racks: list[str],
        rack_nodes: dict[str, list[NodeIntent]],
        rack_leaves: dict[str, list[LeafIntent]],
    ) -> list[CableSpec]:
        """CONTRACT 6.1 cabling rule: node n, port p -> leaf leaf_start + p % L, port (n-1)*(P/L) + p//L + 1.

        `P/L` is rounded up, which equals the contract formula whenever L divides P.
        """
        out: list[CableSpec] = []
        per_leaf = spec.ports_per_node_per_leaf
        lcount = spec.leaves_per_rack
        for rack in racks:
            leaves = rack_leaves[rack]
            for node in rack_nodes[rack]:
                for p, iface in enumerate(node.interfaces):
                    leaf = leaves[p % lcount]
                    port = (node.index_in_rack - 1) * per_leaf + (p // lcount) + 1
                    out.append(
                        CableSpec(
                            label=naming.cable_label(rack, node.index_in_rack, p),
                            a=Endpoint(device=node.name, interface=iface),
                            b=Endpoint(device=leaf.name, interface=leaf.ports[port - 1]),
                            speed_gbps=spec.speed_gbps,
                        )
                    )
        return out

    @staticmethod
    def _validate_cables(
        cables: list[CableSpec], nodes: dict[str, NodeIntent], leaves: dict[str, LeafIntent]
    ) -> None:
        labels: set[str] = set()
        a_ends: set[tuple[str, str]] = set()
        b_ends: set[tuple[str, str]] = set()
        for c in cables:
            if c.label in labels:
                raise SimError(f"duplicate cable label {c.label}")
            labels.add(c.label)
            node = nodes.get(c.a.device)
            if node is None or c.a.interface not in node.interfaces:
                raise SimError(f"cable {c.label}: a end {c.a.device} {c.a.interface} is not a node interface")
            leaf = leaves.get(c.b.device)
            if leaf is None or c.b.interface not in leaf.ports:
                raise SimError(f"cable {c.label}: b end {c.b.device} {c.b.interface} is not a leaf port")
            a, b = (c.a.device, c.a.interface), (c.b.device, c.b.interface)
            if a in a_ends:
                raise SimError(f"cable {c.label}: node interface {a[0]} {a[1]} is cabled twice")
            if b in b_ends:
                raise SimError(f"cable {c.label}: leaf port {b[0]} {b[1]} is cabled twice")
            a_ends.add(a)
            b_ends.add(b)

    @staticmethod
    def _initial_state(
        spec: HallSpec,
        nodes: dict[str, NodeIntent],
        leaves: dict[str, LeafIntent],
        cables: list[CableSpec],
        generated_at: str,
        rng: random.Random,
    ) -> PhysicalState:
        t0 = _parse_iso(generated_at)
        baseline = dict(spec.firmware_baseline)
        if spec.redfish_gpu_firmware:
            baseline.setdefault("gpu_vbios", DEFAULT_GPU_VBIOS)
        fb = gpu_fb_total_mib(spec.node_model)
        node_states: dict[str, NodeState] = {}
        for node in nodes.values():
            gpus = [
                GpuState(
                    index=g,
                    uuid=node.gpu_uuids[g],
                    temp_c=rng.randint(33, 44),
                    power_w=round(rng.uniform(105.0, 145.0), 1),
                    sm_clock_mhz=rng.choice((1830, 1905, 1980)),
                    fb_total_mib=fb,
                    nvlink_bandwidth=rng.randrange(1_600_000, 1_800_000),
                )
                for g in range(node.gpu_count)
            ]
            log = [
                LogEntry(
                    _redfish_time(t0 - timedelta(minutes=25)),
                    "OK",
                    "System boot initiated.",
                    "Base.1.0.Success",
                ),
                LogEntry(
                    _redfish_time(t0 - timedelta(minutes=24)),
                    "OK",
                    f"BMC ready, firmware {baseline.get('bmc', 'unknown')}.",
                    "Base.1.0.Success",
                ),
                LogEntry(
                    _redfish_time(t0 - timedelta(minutes=20)),
                    "OK",
                    "System boot completed.",
                    "Base.1.0.Success",
                ),
            ]
            node_states[node.name] = NodeState(
                name=node.name, serial=node.serial, firmware=dict(baseline), gpus=gpus, log=log
            )
        leaf_states = {
            leaf.name: LeafState(name=leaf.name, lldp_port_id_mode=spec.lldp_port_id_mode, serial=leaf.serial)
            for leaf in leaves.values()
        }
        cable_states = {
            c.label: CableState(
                label=c.label,
                a_device=c.a.device,
                a_interface=c.a.interface,
                b_device=c.b.device,
                b_interface=c.b.interface,
                speed_gbps=c.speed_gbps,
            )
            for c in cables
        }
        return PhysicalState(cables=cable_states, nodes=node_states, leaves=leaf_states)

    # ----------------------------------------------------------------------------------------------
    # Basic accessors
    # ----------------------------------------------------------------------------------------------

    @property
    def site(self) -> str:
        return self.spec.site

    @property
    def base_url(self) -> str:
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value.rstrip("/")

    @property
    def physical(self) -> PhysicalState:
        """The derived physical state (read it, do not mutate it)."""
        return self._phys

    def device_role(self, name: str) -> str | None:
        if name in self.nodes:
            return NODE_ROLE
        if name in self.leaves:
            return LEAF_ROLE
        return None

    def nodes_in_rack(self, rack: str) -> list[NodeIntent]:
        return [n for n in self.nodes.values() if n.rack == rack]

    def leaves_in_rack(self, rack: str) -> list[LeafIntent]:
        return [lf for lf in self.leaves.values() if lf.rack == rack]

    def cables_of_node(self, node: str) -> list[CableSpec]:
        return [c for c in self.intent_cables.values() if c.a.device == node]

    def cable_record(self, label: str) -> dict[str, Any]:
        """The record a QR scan of `label` resolves to (intent data only)."""
        c = self.intent_cables.get(label)
        if c is None:
            st = self._base.cables.get(label)
            if st is None:
                raise UnknownObject(f"unknown cable label {label}")
            return {
                "label": label,
                "in_intent": False,
                "a": {"device": st.a_device, "interface": st.a_interface},
                "b": {"device": st.b_device, "interface": st.b_interface},
                "speed_gbps": st.speed_gbps,
            }
        parsed = naming.parse_cable_label(label)
        rec: dict[str, Any] = {
            "label": label,
            "in_intent": True,
            "a": c.a.model_dump(),
            "b": c.b.model_dump(),
            "a_display": naming.display(c.a.device, c.a.interface),
            "b_display": naming.display(c.b.device, c.b.interface),
            "speed_gbps": c.speed_gbps,
        }
        if parsed:
            rec.update({"rack": parsed[0], "node_index": parsed[1], "port_index": parsed[2]})
        return rec

    def spare_ports(self, leaf: str) -> list[str]:
        """Leaf ports with no intent cable, in port order."""
        lf = self._leaf(leaf)
        used = {c.b.interface for c in self.intent_cables.values() if c.b.device == leaf}
        return [p for p in lf.ports if p not in used]

    def _node(self, name: str) -> NodeIntent:
        try:
            return self.nodes[name]
        except KeyError:
            raise UnknownObject(f"unknown node {name}") from None

    def _leaf(self, name: str) -> LeafIntent:
        try:
            return self.leaves[name]
        except KeyError:
            raise UnknownObject(f"unknown leaf {name}") from None

    # ----------------------------------------------------------------------------------------------
    # Intent document
    # ----------------------------------------------------------------------------------------------

    def intent(self) -> dict[str, Any]:
        """`turnup.intent/v1` with `simulated: true` and mgmt URLs under `base_url`."""
        devices: list[dict[str, Any]] = []
        for rack in self.racks:
            for node in self.nodes_in_rack(rack):
                devices.append(
                    {
                        "name": node.name,
                        "role": NODE_ROLE,
                        "rack": rack,
                        "position": node.position,
                        "manufacturer": node.manufacturer,
                        "model": node.model,
                        "serial": node.serial,
                        "gpu_count": node.gpu_count,
                        "mgmt": {
                            "bmc_url": f"{self._base_url}/dev/{node.name}",
                            "dcgm_url": f"{self._base_url}/dev/{node.name}",
                        },
                        "interfaces": [
                            {"name": iface, "type": self.spec.interface_type, "mac": node.macs[iface]}
                            for iface in node.interfaces
                        ],
                    }
                )
            for leaf in self.leaves_in_rack(rack):
                devices.append(
                    {
                        "name": leaf.name,
                        "role": LEAF_ROLE,
                        "rack": rack,
                        "position": leaf.position,
                        "manufacturer": leaf.manufacturer,
                        "model": leaf.model,
                        "serial": leaf.serial,
                        "mgmt": {
                            "switch_url": f"{self._base_url}/dev/{leaf.name}",
                            "protocol": self.spec.lldp_protocol,
                        },
                        "interfaces": [{"name": p, "type": self.spec.interface_type} for p in leaf.ports],
                    }
                )
        return {
            "schema": INTENT_SCHEMA,
            "site": self.site,
            "simulated": True,
            "generated_at": self.generated_at,
            "racks": [{"name": r} for r in self.racks],
            "devices": devices,
            "cables": [c.model_dump() for c in self.intent_cables.values()],
            "firmware_baseline": {self.spec.node_model: dict(self.spec.firmware_baseline)},
        }

    def write_intent(self, path: str | Path) -> Path:
        """Write the intent as YAML (`.yaml`/`.yml`) or JSON (anything else)."""
        p = Path(path)
        doc = self.intent()
        if p.suffix.lower() in (".yaml", ".yml"):
            p.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), encoding="utf-8")
        else:
            p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return p

    # ----------------------------------------------------------------------------------------------
    # Faults
    # ----------------------------------------------------------------------------------------------

    def faults(self) -> list[Fault]:
        return list(self._faults)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def inject(self, fault: Fault | dict[str, Any]) -> Fault:
        """Validate against the current physical state, apply, and return the fault with its id."""
        f = fault if isinstance(fault, Fault) else Fault.model_validate(fault)
        self._validate_fault(f)
        self._fault_seq += 1
        stamp = f.created_at or datetime.now(UTC).replace(microsecond=0)
        f = f.model_copy(update={"id": f"flt-{self._fault_seq:04d}", "created_at": stamp})
        self._faults.append(f)
        self._derive()
        self._history.append({"event": "inject", "fault": f.summary()})
        return f

    def clear(self, fault_id: str) -> Fault:
        """Remove one fault; the physical state returns to truth for everything that fault touched."""
        for i, f in enumerate(self._faults):
            if f.id == fault_id:
                del self._faults[i]
                self._derive()
                self._history.append({"event": "clear", "fault": f.summary()})
                return f
        raise UnknownObject(f"unknown fault id {fault_id}")

    def clear_all(self) -> int:
        n = len(self._faults)
        self._faults.clear()
        self._derive()
        return n

    def _validate_fault(self, f: Fault) -> None:
        phys = self._phys
        if f.kind in ("wrong_port", "missing_link", "link_degraded"):
            if f.cable not in phys.cables:
                raise UnknownObject(f"unknown cable {f.cable}")
        if f.kind == "wrong_port":
            leaf = self._leaf(str(f.to_device))
            if f.to_interface not in leaf.ports:
                raise UnknownObject(f"{f.to_device} has no port {f.to_interface}")
            holder = phys.port_index.get((leaf.name, str(f.to_interface)))
            if holder is not None and holder != f.cable:
                raise Conflict(f"{naming.display(leaf.name, str(f.to_interface))} is occupied by {holder}")
        if f.kind == "swap_pair":
            for label in (f.cable_a, f.cable_b):
                if label not in phys.cables:
                    raise UnknownObject(f"unknown cable {label}")
        if f.kind == "unreachable_node":
            if self.device_role(str(f.node)) is None:
                raise UnknownObject(f"unknown device {f.node}")
        elif f.kind in (
            "partial_redfish",
            "firmware_off_baseline",
            "gpu_unhealthy",
            "gpu_missing",
            "nvlink_degraded",
            "node_health_warning",
            "pdu_outlet_off",
        ):
            node = self._node(str(f.node))
            if f.kind in ("gpu_unhealthy", "gpu_missing") and not (0 <= int(f.gpu or 0) < node.gpu_count):
                raise UnknownObject(f"{node.name} has no GPU {f.gpu} (gpu_count={node.gpu_count})")
        if f.kind == "coolant_leak" and not self._leak_targets(f):
            raise UnknownObject(f"coolant_leak target {f.node or f.rack} is neither a node nor a rack")
        if f.kind == "lldp_port_id_mac":
            self._leaf(str(f.leaf))

    def _leak_targets(self, f: Fault) -> list[str]:
        target = f.node or f.rack or ""
        if target in self.nodes:
            return [target]
        if target in self.racks:
            return [n.name for n in self.nodes_in_rack(target)]
        return []

    def _derive(self) -> None:
        phys = copy.deepcopy(self._base)
        for f in self._faults:
            self._apply_fault(phys, f)
        self._reindex(phys)
        self._phys = phys

    def _apply_fault(self, phys: PhysicalState, f: Fault) -> None:
        when = _redfish_time(f.created_at or datetime.now(UTC))
        if f.kind == "wrong_port":
            c = phys.cables[str(f.cable)]
            c.b_device, c.b_interface, c.seated = str(f.to_device), str(f.to_interface), True
        elif f.kind == "missing_link":
            phys.cables[str(f.cable)].seated = False
        elif f.kind == "swap_pair":
            a, b = phys.cables[str(f.cable_a)], phys.cables[str(f.cable_b)]
            a.b_device, b.b_device = b.b_device, a.b_device
            a.b_interface, b.b_interface = b.b_interface, a.b_interface
        elif f.kind == "link_degraded":
            c = phys.cables[str(f.cable)]
            if f.speed_gbps == "down":
                c.link_down = True
            else:
                c.link_speed_gbps = int(f.speed_gbps)  # type: ignore[arg-type]
        elif f.kind == "unreachable_node":
            if f.node in phys.nodes:
                phys.nodes[str(f.node)].reach_mode = f.mode
                phys.nodes[str(f.node)].reach_scope = f.scope
            elif f.node in phys.leaves:
                phys.leaves[str(f.node)].reach_mode = f.mode
        elif f.kind == "partial_redfish":
            phys.nodes[str(f.node)].partial_redfish = True
        elif f.kind == "firmware_off_baseline":
            phys.nodes[str(f.node)].firmware[str(f.component)] = str(f.version)
        elif f.kind == "gpu_unhealthy":
            n = phys.nodes[str(f.node)]
            g = n.gpus[int(f.gpu or 0)]
            g.xid_errors = f.xid
            n.log.append(
                LogEntry(
                    when,
                    "Critical",
                    f"GPU{g.index} ({g.uuid}): XID {f.xid} error reported.",
                    "Oem.Sim.GpuXid",
                )
            )
        elif f.kind == "gpu_missing":
            n = phys.nodes[str(f.node)]
            g = n.gpus[int(f.gpu or 0)]
            g.present = False
            n.log.append(
                LogEntry(
                    when, "Critical", f"GPU{g.index} not detected during enumeration.", "Oem.Sim.GpuMissing"
                )
            )
        elif f.kind == "nvlink_degraded":
            phys.nodes[str(f.node)].nvlink_degraded = True
        elif f.kind == "node_health_warning":
            n = phys.nodes[str(f.node)]
            n.health = "Warning"
            n.log.append(
                LogEntry(
                    when,
                    "Warning",
                    "Sensor threshold crossed: PSU1 input voltage below warning threshold.",
                    "Base.1.0.Warning",
                )
            )
        elif f.kind == "coolant_leak":
            for name in self._leak_targets(f):
                n = phys.nodes[name]
                n.leak = True
                n.log.append(
                    LogEntry(when, "Critical", "Leak detected by tray leak detector.", "Oem.Sim.LeakDetected")
                )
        elif f.kind == "pdu_outlet_off":
            phys.nodes[str(f.node)].pdu_on = False
        elif f.kind == "lldp_port_id_mac":
            phys.leaves[str(f.leaf)].lldp_port_id_mode = "mac"

    @staticmethod
    def _reindex(phys: PhysicalState) -> None:
        """Rebuild the port occupancy maps; a port can hold one cable, the first in base order wins."""
        phys.port_index = {}
        phys.node_port_index = {}
        phys.dangling = set()
        for label, c in phys.cables.items():
            if not c.seated:
                continue
            key = (c.b_device, c.b_interface)
            if key in phys.port_index:
                phys.dangling.add(label)
                continue
            phys.port_index[key] = label
            phys.node_port_index[(c.a_device, c.a_interface)] = label

    # ----------------------------------------------------------------------------------------------
    # Actions
    # ----------------------------------------------------------------------------------------------

    def act(self, action: Action | dict[str, Any]) -> dict[str, Any]:
        """Perform a physical action; returns a result dict including the faults it retired."""
        a = action if isinstance(action, Action) else Action.model_validate(action)
        handler: Callable[[Action], dict[str, Any]] = getattr(self, f"_act_{a.kind}")
        result = handler(a)
        result.setdefault("ok", True)
        result["kind"] = a.kind
        self._history.append({"event": "action", "action": a.model_dump(exclude_none=True), "result": result})
        return result

    def _retire(self, predicate: Callable[[Fault], bool], by: str) -> list[str]:
        retired = [f for f in self._faults if predicate(f)]
        if retired:
            self._faults = [f for f in self._faults if f not in retired]
            for f in retired:
                self._history.append({"event": "retire", "fault": f.summary(), "by": by})
        return [str(f.id) for f in retired]

    def _materialize_cable(self, label: str, by: str) -> list[str]:
        """Copy a cable's derived position into the base and retire the faults that put it there."""
        derived = self._phys.cables.get(label)
        if derived is not None and label in self._base.cables:
            self._base.cables[label] = copy.deepcopy(derived)
        labels = {label}
        for f in self._faults:
            if f.kind == "swap_pair" and label in (f.cable_a, f.cable_b):
                labels.update({str(f.cable_a), str(f.cable_b)})
        for other in labels - {label}:
            d = self._phys.cables.get(other)
            if d is not None and other in self._base.cables:
                self._base.cables[other] = copy.deepcopy(d)
        retired = self._retire(
            lambda f: (
                (f.kind in ("wrong_port", "missing_link", "link_degraded") and f.cable in labels)
                or (f.kind == "swap_pair" and (f.cable_a in labels or f.cable_b in labels))
            ),
            by,
        )
        self._derive()
        return retired

    def _act_seat_cable(self, a: Action) -> dict[str, Any]:
        label = str(a.label)
        intent = self.intent_cables.get(label)
        a_dev = a.a_device or (intent.a.device if intent else None)
        a_if = a.a_interface or (intent.a.interface if intent else None)
        b_dev = a.b_device or (intent.b.device if intent else None)
        b_if = a.b_interface or (intent.b.interface if intent else None)
        if not (a_dev and a_if and b_dev and b_if):
            raise SimError(
                f"seat_cable {label}: label is not in intent, so a_device, a_interface, b_device "
                "and b_interface are required"
            )
        node = self._node(a_dev)
        if a_if not in node.interfaces:
            raise UnknownObject(f"{a_dev} has no interface {a_if}")
        leaf = self._leaf(b_dev)
        if b_if not in leaf.ports:
            raise UnknownObject(f"{b_dev} has no port {b_if}")
        retired = self._materialize_cable(label, "seat_cable")
        holder = self._phys.port_index.get((leaf.name, b_if))
        if holder is not None and holder != label:
            raise Conflict(f"{naming.display(leaf.name, b_if)} is occupied by {holder}")
        holder = self._phys.node_port_index.get((node.name, a_if))
        if holder is not None and holder != label:
            raise Conflict(f"{naming.display(node.name, a_if)} already has cable {holder}")
        st = self._base.cables.get(label)
        if st is None:
            st = CableState(
                label,
                a_dev,
                a_if,
                b_dev,
                b_if,
                speed_gbps=intent.speed_gbps if intent else self.spec.speed_gbps,
            )
            st.in_intent = False
            self._base.cables[label] = st
        st.a_device, st.a_interface, st.b_device, st.b_interface = a_dev, a_if, b_dev, b_if
        st.seated, st.link_down, st.link_speed_gbps = True, False, None
        self._derive()
        return {
            "label": label,
            "a": naming.display(a_dev, a_if),
            "b": naming.display(b_dev, b_if),
            "matches_intent": bool(intent and (b_dev, b_if) == (intent.b.device, intent.b.interface)),
            "retired_faults": retired,
        }

    def _act_unseat_cable(self, a: Action) -> dict[str, Any]:
        label = str(a.label)
        if label not in self._base.cables:
            raise UnknownObject(f"unknown cable {label}")
        retired = self._materialize_cable(label, "unseat_cable")
        st = self._base.cables[label]
        st.seated, st.link_down, st.link_speed_gbps = False, False, None
        self._derive()
        return {"label": label, "retired_faults": retired}

    def _log(self, node: str, severity: str, message: str, message_id: str = "Base.1.0.Success") -> None:
        self._base.nodes[node].log.append(
            LogEntry(_redfish_time(datetime.now(UTC)), severity, message, message_id)
        )

    def _act_power_on(self, a: Action) -> dict[str, Any]:
        dev = str(a.device)
        if dev in self.leaves:
            return {"device": dev, "power": "On", "retired_faults": []}
        self._node(dev)
        retired = self._retire(lambda f: f.kind == "pdu_outlet_off" and f.node == dev, "power_on")
        n = self._base.nodes[dev]
        n.pdu_on, n.power = True, "On"
        self._log(dev, "OK", "Host power on requested.")
        self._derive()
        return {"device": dev, "power": "On", "retired_faults": retired}

    def _act_power_off(self, a: Action) -> dict[str, Any]:
        dev = str(a.device)
        if dev in self.leaves:
            return {"device": dev, "power": "Off", "retired_faults": []}
        self._node(dev)
        self._base.nodes[dev].power = "Off"
        self._log(dev, "OK", "Host power off requested.")
        self._derive()
        return {"device": dev, "power": "Off", "retired_faults": []}

    def _act_replace_node(self, a: Action) -> dict[str, Any]:
        dev = str(a.device)
        node = self._node(dev)
        new_serial = str(a.new_serial)
        node_kinds = (
            "unreachable_node",
            "partial_redfish",
            "firmware_off_baseline",
            "gpu_unhealthy",
            "gpu_missing",
            "nvlink_degraded",
            "node_health_warning",
            "pdu_outlet_off",
        )
        retired = self._retire(
            lambda f: (
                (f.kind in node_kinds and f.node == dev) or (f.kind == "coolant_leak" and f.node == dev)
            ),
            "replace_node",
        )
        old = self._base.nodes[dev]
        rng = random.Random(f"{self.spec.seed}:{new_serial}")
        baseline = dict(self.spec.firmware_baseline)
        if self.spec.redfish_gpu_firmware:
            baseline.setdefault("gpu_vbios", DEFAULT_GPU_VBIOS)
        gpus = [
            GpuState(
                index=g,
                uuid=f"GPU-{uuid.UUID(int=rng.getrandbits(128), version=4)}",
                temp_c=rng.randint(33, 44),
                power_w=round(rng.uniform(105.0, 145.0), 1),
                sm_clock_mhz=rng.choice((1830, 1905, 1980)),
                fb_total_mib=gpu_fb_total_mib(self.spec.node_model),
                nvlink_bandwidth=rng.randrange(1_600_000, 1_800_000),
            )
            for g in range(node.gpu_count)
        ]
        self._base.nodes[dev] = NodeState(
            name=dev,
            serial=new_serial,
            firmware=baseline,
            gpus=gpus,
            log=[*old.log],
        )
        self._log(dev, "OK", f"Compute tray replaced: serial {old.serial} -> {new_serial}.")
        self._derive()
        return {"device": dev, "old_serial": old.serial, "new_serial": new_serial, "retired_faults": retired}

    def _act_update_firmware(self, a: Action) -> dict[str, Any]:
        dev = str(a.device)
        self._node(dev)
        comp, ver = str(a.component), str(a.version)
        retired = self._retire(
            lambda f: f.kind == "firmware_off_baseline" and f.node == dev and f.component == comp,
            "update_firmware",
        )
        old = self._phys.nodes[dev].firmware.get(comp)
        self._base.nodes[dev].firmware[comp] = ver
        self._log(dev, "OK", f"Firmware update applied: {comp} {old} -> {ver}.")
        self._derive()
        return {
            "device": dev,
            "component": comp,
            "old_version": old,
            "version": ver,
            "retired_faults": retired,
        }

    def _act_scan_qr(self, a: Action) -> dict[str, Any]:
        return {"record": self.cable_record(str(a.label)), "retired_faults": []}

    # ----------------------------------------------------------------------------------------------
    # Physical views used by the emulators
    # ----------------------------------------------------------------------------------------------

    @staticmethod
    def _links_active(n: NodeState) -> bool:
        return n.power == "On" and n.pdu_on

    def reach(self, device: str, proto: str = "redfish") -> str:
        """Reachability for `proto` in {redfish, dcgm, switch}: ok | timeout | http_500 | refused."""
        phys = self._phys
        if device in phys.nodes:
            n = phys.nodes[device]
            if not n.pdu_on:
                return "refused"
            if proto == "dcgm" and n.power == "Off":
                return "refused"
            if n.reach_mode != "ok" and n.reach_scope in ("all", proto):
                return n.reach_mode
            return "ok"
        if device in phys.leaves:
            return phys.leaves[device].reach_mode
        raise UnknownObject(f"unknown device {device}")

    def partial_redfish(self, node: str) -> bool:
        return self._phys.nodes[self._node(node).name].partial_redfish

    def leaf_port_views(self, leaf: str) -> list[LeafPortView]:
        lf = self._leaf(leaf)
        phys = self._phys
        mode = phys.leaves[leaf].lldp_port_id_mode
        out: list[LeafPortView] = []
        for k, port in enumerate(lf.ports, start=1):
            label = phys.port_index.get((leaf, port))
            c = phys.cables.get(label) if label else None
            node_state = phys.nodes.get(c.a_device) if c else None
            node_intent = self.nodes.get(c.a_device) if c else None
            if c is None or node_state is None or node_intent is None or not self._links_active(node_state):
                out.append(LeafPortView(leaf, port, k, False, None, label, None, None, None, False, None))
                continue
            link_up = not c.link_down
            speed = (c.link_speed_gbps or c.speed_gbps) if link_up else None
            is_mac = mode == "mac"
            port_id = node_intent.macs[c.a_interface] if is_mac else c.a_interface
            out.append(
                LeafPortView(
                    leaf=leaf,
                    port=port,
                    number=k,
                    link_up=link_up,
                    speed_gbps=speed,
                    label=label,
                    neighbor_system=node_intent.name,
                    neighbor_chassis=node_intent.chassis_mac,
                    neighbor_port_id=port_id,
                    neighbor_port_is_mac=is_mac,
                    neighbor_iface=c.a_interface,
                )
            )
        return out

    def node_port_views(self, node: str) -> list[NodePortView]:
        nd = self._node(node)
        phys = self._phys
        n = phys.nodes[node]
        out: list[NodePortView] = []
        for iface in nd.interfaces:
            label = phys.node_port_index.get((node, iface))
            c = phys.cables.get(label) if label else None
            mac = nd.macs[iface]
            if c is None:
                out.append(NodePortView(node, iface, mac, "NoLink", 0, None, None, None, None))
                continue
            if not self._links_active(n):
                out.append(NodePortView(node, iface, mac, "LinkDown", 0, label, None, None, None))
                continue
            leaf = self.leaves.get(c.b_device)
            status = "LinkDown" if c.link_down else "LinkUp"
            speed = (c.link_speed_gbps or c.speed_gbps) if status == "LinkUp" else 0
            out.append(
                NodePortView(
                    node=node,
                    iface=iface,
                    mac=mac,
                    link_status=status,
                    speed_gbps=speed,
                    label=label,
                    peer_system=c.b_device,
                    peer_port=c.b_interface,
                    peer_chassis=leaf.chassis_mac if leaf else None,
                )
            )
        return out

    # ----------------------------------------------------------------------------------------------
    # Emulated views
    # ----------------------------------------------------------------------------------------------

    def redfish_tree(self, node: str) -> dict[str, dict[str, Any]]:
        """Resource path -> JSON, as `/dev/<node>/redfish/v1/...` serves it (paths are device-relative)."""
        nd = self._node(node)
        return redfish_emu.render_tree(self.spec, nd, self._phys.nodes[node], self.node_port_views(node))

    def lldp_table(self, leaf: str) -> dict[str, Any]:
        """eAPI (`{"lldpNeighbors", "interfaceStatuses"}`) or NVUE (`{"swp1": ...}`) view of one leaf."""
        lf = self._leaf(leaf)
        views = self.leaf_port_views(leaf)
        if lf.vendor == "arista":
            return lldp_emu.render_eapi(lf, views)
        return lldp_emu.render_nvue(lf, views)

    def dcgm_metrics(self, node: str) -> str:
        """DCGM-exporter Prometheus text for one node."""
        nd = self._node(node)
        return dcgm_emu.render(self.spec, nd, self._phys.nodes[node])

    # ----------------------------------------------------------------------------------------------
    # State dump
    # ----------------------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Full physical state: cabling, power, firmware, GPU health, reachability and active faults."""
        phys = self._phys
        cables: dict[str, Any] = {}
        deviating = 0
        for label, c in phys.cables.items():
            intent = self.intent_cables.get(label)
            intended_b = {"device": intent.b.device, "interface": intent.b.interface} if intent else None
            deviates = (
                not c.seated
                or label in phys.dangling
                or c.link_down
                or c.link_speed_gbps is not None
                or intent is None
                or (c.b_device, c.b_interface) != (intent.b.device, intent.b.interface)
            )
            deviating += int(deviates)
            cables[label] = {
                "a_device": c.a_device,
                "a_interface": c.a_interface,
                "b_device": c.b_device,
                "b_interface": c.b_interface,
                "seated": c.seated and label not in phys.dangling,
                "speed_gbps": c.speed_gbps,
                "link_speed_gbps": c.link_speed_gbps,
                "link_down": c.link_down,
                "in_intent": c.in_intent,
                "intended_b": intended_b,
                "deviates": deviates,
            }
        nodes = {
            name: {
                "serial": n.serial,
                "power": "On" if self._links_active(n) else "Off",
                "health": n.health,
                "firmware": dict(n.firmware),
                "reach": {"redfish": self.reach(name, "redfish"), "dcgm": self.reach(name, "dcgm")},
                "partial_redfish": n.partial_redfish,
                "leak": n.leak,
                "pdu_on": n.pdu_on,
                "nvlink_degraded": n.nvlink_degraded,
                "gpus": [asdict(g) for g in n.gpus],
                "log_entries": len(n.log),
            }
            for name, n in phys.nodes.items()
        }
        leaves = {
            name: {
                "lldp_port_id_mode": lf.lldp_port_id_mode,
                "reach": lf.reach_mode,
                "ports_with_neighbors": sum(1 for v in self.leaf_port_views(name) if v.neighbor_system),
            }
            for name, lf in phys.leaves.items()
        }
        return {
            "schema": "simhall.state/v1",
            "site": self.site,
            "golden": self.golden,
            "base_url": self._base_url,
            "generated_at": self.generated_at,
            "summary": {
                "nodes": len(nodes),
                "leaves": len(leaves),
                "cables": len(cables),
                "deviating_cables": deviating,
                "faults": len(self._faults),
            },
            "cables": cables,
            "nodes": nodes,
            "leaves": leaves,
            "faults": [f.summary() for f in self._faults],
        }
