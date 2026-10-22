# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Actions: what a technician (or a bot) physically does to the hall (CONTRACT.md section 6.2).

Actions change the ground truth and retire any fault they physically resolve; emulators reflect the
result on the next poll.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

ActionKind = Literal[
    "seat_cable",
    "unseat_cable",
    "power_on",
    "power_off",
    "replace_node",
    "update_firmware",
    "scan_qr",
]

ACTION_KINDS: tuple[str, ...] = ActionKind.__args__  # type: ignore[attr-defined]

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "seat_cable": ("label",),
    "unseat_cable": ("label",),
    "power_on": ("device",),
    "power_off": ("device",),
    "replace_node": ("device", "new_serial"),
    "update_firmware": ("device", "component", "version"),
    "scan_qr": ("label",),
}


class Action(BaseModel):
    """One physical action. For `seat_cable`, ends omitted default to the intent ends of `label`."""

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    label: str | None = None
    a_device: str | None = None
    a_interface: str | None = None
    b_device: str | None = None
    b_interface: str | None = None
    device: str | None = None
    new_serial: str | None = None
    component: str | None = None
    version: str | None = None
    actor: str | None = None

    @model_validator(mode="after")
    def _required(self) -> Action:
        missing = [f for f in REQUIRED_FIELDS[self.kind] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"action {self.kind} requires {', '.join(missing)}")
        return self


def seat_cable(
    label: str,
    a_device: str | None = None,
    a_interface: str | None = None,
    b_device: str | None = None,
    b_interface: str | None = None,
    actor: str | None = None,
) -> Action:
    return Action(
        kind="seat_cable",
        label=label,
        a_device=a_device,
        a_interface=a_interface,
        b_device=b_device,
        b_interface=b_interface,
        actor=actor,
    )


def unseat_cable(label: str, actor: str | None = None) -> Action:
    return Action(kind="unseat_cable", label=label, actor=actor)


def power_on(device: str, actor: str | None = None) -> Action:
    return Action(kind="power_on", device=device, actor=actor)


def power_off(device: str, actor: str | None = None) -> Action:
    return Action(kind="power_off", device=device, actor=actor)


def replace_node(device: str, new_serial: str, actor: str | None = None) -> Action:
    return Action(kind="replace_node", device=device, new_serial=new_serial, actor=actor)


def update_firmware(device: str, component: str, version: str, actor: str | None = None) -> Action:
    return Action(kind="update_firmware", device=device, component=component, version=version, actor=actor)


def scan_qr(label: str, actor: str | None = None) -> Action:
    return Action(kind="scan_qr", label=label, actor=actor)
