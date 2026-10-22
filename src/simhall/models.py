# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Specs (what to generate) and physical state (what the emulators render).

`HallSpec` is the generator input from CONTRACT.md section 6.1. `CableSpec` is one intent cable in
the same shape as the `cables` entries of `turnup.intent/v1`. The dataclasses below are the
mutable physical state: the ground truth that faults and technician actions change and that the
Redfish, LLDP, DCGM and NetBox emulators read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SwitchVendor = Literal["nvidia", "arista"]
LldpIdMode = Literal["name", "mac"]
ReachMode = Literal["ok", "timeout", "http_500", "refused"]
ReachScope = Literal["all", "redfish", "dcgm"]

DEFAULT_FIRMWARE_BASELINE: dict[str, str] = {"bmc": "1.12.0", "bios": "2.3.1", "nic": "28.43.1014"}
DEFAULT_GPU_VBIOS = "97.00.5A.00.01"
INTENT_SCHEMA = "turnup.intent/v1"


def gpu_count_for_model(model: str) -> int:
    """CONTRACT 2.1: `NVL72` -> 4; `HGX` -> 8; `MI300`/`MI325`/`MI355` -> 8; else 8."""
    if "NVL72" in model:
        return 4
    return 8


def gpu_model_name(node_model: str) -> str:
    """The `modelName` label DCGM-exporter prints, derived from the node model string."""
    upper = node_model.upper()
    for token in ("GB300", "GB200", "B300", "B200", "H200", "H100", "MI355", "MI325", "MI300"):
        if token in upper:
            return f"NVIDIA {token}" if not token.startswith("MI") else f"AMD Instinct {token}"
    return "NVIDIA GPU"


def gpu_fb_total_mib(node_model: str) -> int:
    """Framebuffer size DCGM reports (MiB) for the GPU class implied by the node model."""
    upper = node_model.upper()
    if "GB300" in upper or "B300" in upper:
        return 288 * 1024
    if "GB200" in upper or "B200" in upper:
        return 192 * 1024
    if "H200" in upper:
        return 143771
    return 81559


class HallSpec(BaseModel):
    """Generator input; defaults reproduce the golden hall-b of CONTRACT.md section 8 (minus its cabling)."""

    model_config = ConfigDict(extra="forbid")

    site: str = "hall-b"
    racks: int = Field(1, ge=1, le=64)
    nodes_per_rack: int = Field(18, ge=1, le=99)
    ports_per_node: int = Field(16, ge=1, le=99)
    leaves_per_rack: int = Field(2, ge=1, le=16)
    node_name_fmt: str = "node{n}"
    leaf_name_fmt: str = "leaf{n}"
    leaf_start: int = 3
    rack_start: int = 12
    switch_vendor: SwitchVendor = "nvidia"
    lldp_port_id_mode: LldpIdMode = "name"
    node_model: str = "GB300 NVL72 compute tray"
    node_manufacturer: str = "NVIDIA"
    leaf_model: str | None = None
    gpu_count: int | None = Field(None, ge=1, le=16)
    speed_gbps: int = 400
    interface_type: str = "400gbase-x-osfp"
    spare_ports: int = Field(16, ge=0, le=512)
    firmware_baseline: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FIRMWARE_BASELINE))
    base_url: str = "http://127.0.0.1:8900"
    redfish_system_id: str = "System.Embedded.1"
    redfish_manager_id: str = "BMC.Embedded.1"
    redfish_chassis_id: str = "Chassis.Embedded.1"
    redfish_gpu_firmware: bool = False
    seed: int = 1
    generated_at: str | None = None

    @model_validator(mode="after")
    def _check(self) -> HallSpec:
        if "{n}" not in self.node_name_fmt and "{i}" not in self.node_name_fmt:
            raise ValueError("node_name_fmt must contain {n} (global index) or {i} (index in rack)")
        if "{n}" not in self.leaf_name_fmt:
            raise ValueError("leaf_name_fmt must contain {n}")
        return self

    @property
    def effective_gpu_count(self) -> int:
        return self.gpu_count if self.gpu_count is not None else gpu_count_for_model(self.node_model)

    @property
    def effective_leaf_model(self) -> str:
        if self.leaf_model:
            return self.leaf_model
        return "7060DX5-64S" if self.switch_vendor == "arista" else "SN5600"

    @property
    def leaf_manufacturer(self) -> str:
        return "Arista" if self.switch_vendor == "arista" else "NVIDIA"

    @property
    def lldp_protocol(self) -> str:
        return "eapi" if self.switch_vendor == "arista" else "nvue"

    @property
    def ports_per_node_per_leaf(self) -> int:
        """Leaf ports each node consumes on each leaf: `ports_per_node / leaves_per_rack`, rounded up.

        Equal to the CONTRACT 6.1 formula whenever `ports_per_node` is divisible by `leaves_per_rack`;
        the rounding only avoids port collisions for specs where it is not.
        """
        return math.ceil(self.ports_per_node / self.leaves_per_rack)

    @property
    def used_ports_per_leaf(self) -> int:
        return self.nodes_per_rack * self.ports_per_node_per_leaf

    @property
    def leaf_ports(self) -> int:
        """Ports per leaf: the used range plus `spare_ports` above it."""
        return self.used_ports_per_leaf + self.spare_ports


class Endpoint(BaseModel):
    """One end of a cable: `{device, interface}`."""

    model_config = ConfigDict(extra="forbid")

    device: str
    interface: str


class CableSpec(BaseModel):
    """One intent cable, in the `turnup.intent/v1` shape."""

    model_config = ConfigDict(extra="forbid")

    label: str
    a: Endpoint
    b: Endpoint
    speed_gbps: int = 400


# --------------------------------------------------------------------------------------------------
# Intent-side records (fixed once generated)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeIntent:
    name: str
    rack: str
    index_in_rack: int
    global_index: int
    position: int
    serial: str
    model: str
    manufacturer: str
    gpu_count: int
    interfaces: tuple[str, ...]
    macs: dict[str, str]
    chassis_mac: str
    gpu_uuids: tuple[str, ...]


@dataclass(frozen=True)
class LeafIntent:
    name: str
    rack: str
    index_in_rack: int
    number: int
    position: int
    serial: str
    model: str
    manufacturer: str
    vendor: SwitchVendor
    ports: tuple[str, ...]
    chassis_mac: str


# --------------------------------------------------------------------------------------------------
# Physical state (mutable ground truth)
# --------------------------------------------------------------------------------------------------


@dataclass
class CableState:
    label: str
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str
    speed_gbps: int
    seated: bool = True
    link_speed_gbps: int | None = None
    link_down: bool = False
    in_intent: bool = True


@dataclass
class GpuState:
    index: int
    uuid: str
    present: bool = True
    xid_errors: int = 0
    ecc_dbe: int = 0
    row_remap_failure: int = 0
    uncorrectable_remapped_rows: int = 0
    pcie_replay: int = 0
    nvlink_crc_errors: int = 0
    nvlink_bandwidth: int = 0
    temp_c: int = 38
    power_w: float = 120.0
    sm_clock_mhz: int = 1980
    fb_total_mib: int = 81559


@dataclass
class LogEntry:
    created: str
    severity: str
    message: str
    message_id: str


@dataclass
class NodeState:
    name: str
    serial: str
    firmware: dict[str, str]
    gpus: list[GpuState]
    log: list[LogEntry] = field(default_factory=list)
    power: str = "On"
    health: str = "OK"
    nvlink_degraded: bool = False
    reach_mode: str = "ok"
    reach_scope: str = "all"
    partial_redfish: bool = False
    leak: bool = False
    pdu_on: bool = True
    inlet_c: float = 24.0


@dataclass
class LeafState:
    name: str
    lldp_port_id_mode: str
    serial: str
    reach_mode: str = "ok"


@dataclass
class PhysicalState:
    cables: dict[str, CableState]
    nodes: dict[str, NodeState]
    leaves: dict[str, LeafState]
    port_index: dict[tuple[str, str], str] = field(default_factory=dict)
    node_port_index: dict[tuple[str, str], str] = field(default_factory=dict)
    dangling: set[str] = field(default_factory=set)
