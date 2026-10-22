# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Faults: the only way the physical state deviates from intent (CONTRACT.md section 6.2).

A `Fault` is a pydantic record with a `kind` and the parameters that kind needs. Construct one with
`Fault(kind="wrong_port", cable=..., to_device=..., to_interface=...)` or with the helpers below,
e.g. `wrong_port("CBL-12-07-03", "leaf4", "swp22")`. `Hall.inject()` assigns the `id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

FaultKind = Literal[
    "wrong_port",
    "missing_link",
    "swap_pair",
    "unreachable_node",
    "partial_redfish",
    "firmware_off_baseline",
    "gpu_unhealthy",
    "gpu_missing",
    "nvlink_degraded",
    "link_degraded",
    "node_health_warning",
    "coolant_leak",
    "pdu_outlet_off",
    "lldp_port_id_mac",
]

FAULT_KINDS: tuple[str, ...] = FaultKind.__args__  # type: ignore[attr-defined]

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "wrong_port": ("cable", "to_device", "to_interface"),
    "missing_link": ("cable",),
    "swap_pair": ("cable_a", "cable_b"),
    "unreachable_node": ("node",),
    "partial_redfish": ("node",),
    "firmware_off_baseline": ("node", "component", "version"),
    "gpu_unhealthy": ("node", "gpu"),
    "gpu_missing": ("node", "gpu"),
    "nvlink_degraded": ("node",),
    "link_degraded": ("cable", "speed_gbps"),
    "node_health_warning": ("node",),
    "coolant_leak": (),
    "pdu_outlet_off": ("node",),
    "lldp_port_id_mac": ("leaf",),
}


class Fault(BaseModel):
    """One injected deviation. Fields not used by `kind` stay None."""

    model_config = ConfigDict(extra="forbid")

    kind: FaultKind
    id: str | None = None
    cable: str | None = None
    cable_a: str | None = None
    cable_b: str | None = None
    to_device: str | None = None
    to_interface: str | None = None
    node: str | None = None
    leaf: str | None = None
    rack: str | None = None
    mode: Literal["timeout", "http_500", "refused"] = "timeout"
    scope: Literal["all", "redfish", "dcgm"] = "all"
    component: str | None = None
    version: str | None = None
    gpu: int | None = None
    xid: int = 79
    speed_gbps: int | Literal["down"] | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _required(self) -> Fault:
        missing = [f for f in REQUIRED_FIELDS[self.kind] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"fault {self.kind} requires {', '.join(missing)}")
        if self.kind == "coolant_leak" and not (self.node or self.rack):
            raise ValueError("fault coolant_leak requires node or rack")
        if self.kind == "swap_pair" and self.cable_a == self.cable_b:
            raise ValueError("fault swap_pair needs two different cables")
        if self.kind == "link_degraded" and isinstance(self.speed_gbps, int) and self.speed_gbps <= 0:
            raise ValueError("link_degraded speed_gbps must be positive or 'down'")
        return self

    @property
    def target(self) -> str:
        """The device, cable or leaf the fault is about (for display)."""
        for f in ("cable", "cable_a", "node", "leaf", "rack"):
            v = getattr(self, f)
            if v:
                return str(v)
        return ""

    def summary(self) -> dict[str, Any]:
        """Compact dict without None fields, suitable for `/sim/faults`."""
        d = self.model_dump(mode="json", exclude_none=True)
        if self.kind not in ("unreachable_node",):
            d.pop("mode", None)
            d.pop("scope", None)
        if self.kind != "gpu_unhealthy":
            d.pop("xid", None)
        return d


def wrong_port(cable: str, to_device: str, to_interface: str) -> Fault:
    return Fault(kind="wrong_port", cable=cable, to_device=to_device, to_interface=to_interface)


def missing_link(cable: str) -> Fault:
    return Fault(kind="missing_link", cable=cable)


def swap_pair(cable_a: str, cable_b: str) -> Fault:
    return Fault(kind="swap_pair", cable_a=cable_a, cable_b=cable_b)


def unreachable_node(
    node: str,
    mode: Literal["timeout", "http_500", "refused"] = "timeout",
    scope: Literal["all", "redfish", "dcgm"] = "all",
) -> Fault:
    return Fault(kind="unreachable_node", node=node, mode=mode, scope=scope)


def partial_redfish(node: str) -> Fault:
    return Fault(kind="partial_redfish", node=node)


def firmware_off_baseline(node: str, component: str, version: str) -> Fault:
    return Fault(kind="firmware_off_baseline", node=node, component=component, version=version)


def gpu_unhealthy(node: str, gpu: int, xid: int = 79) -> Fault:
    return Fault(kind="gpu_unhealthy", node=node, gpu=gpu, xid=xid)


def gpu_missing(node: str, gpu: int) -> Fault:
    return Fault(kind="gpu_missing", node=node, gpu=gpu)


def nvlink_degraded(node: str) -> Fault:
    return Fault(kind="nvlink_degraded", node=node)


def link_degraded(cable: str, speed_gbps: int | Literal["down"] = 200) -> Fault:
    return Fault(kind="link_degraded", cable=cable, speed_gbps=speed_gbps)


def node_health_warning(node: str) -> Fault:
    return Fault(kind="node_health_warning", node=node)


def coolant_leak(target: str) -> Fault:
    """`target` is a node name or a rack name; the hall resolves which."""
    return Fault(kind="coolant_leak", node=target)


def pdu_outlet_off(node: str) -> Fault:
    return Fault(kind="pdu_outlet_off", node=node)


def lldp_port_id_mac(leaf: str) -> Fault:
    return Fault(kind="lldp_port_id_mac", leaf=leaf)
