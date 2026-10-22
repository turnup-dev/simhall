# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Spec files: a `HallSpec` plus optional explicit `cables` and start-up `faults`, as YAML or JSON.

```yaml
site: hall-b
racks: 1
nodes_per_rack: 18
cables:                      # optional; replaces the cabling rule
  - {label: CBL-12-07-03, a: {device: node7, interface: mlx5_3}, b: {device: leaf4, interface: swp21}}
faults:                      # optional; injected when the hall is built
  - {kind: wrong_port, cable: CBL-12-07-03, to_device: leaf4, to_interface: swp22}
```
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from simhall.faults import Fault
from simhall.hall import Hall
from simhall.models import CableSpec, HallSpec


class SpecFile(BaseModel):
    """Everything needed to rebuild one hall."""

    model_config = ConfigDict(extra="forbid")

    spec: HallSpec = Field(default_factory=HallSpec)
    cables: list[CableSpec] | None = None
    faults: list[Fault] = Field(default_factory=list)

    @classmethod
    def from_mapping(cls, doc: dict[str, Any]) -> SpecFile:
        data = dict(doc)
        cables = data.pop("cables", None)
        faults = data.pop("faults", None) or []
        spec_fields = data.pop("spec", None)
        if spec_fields is not None:
            data = {**spec_fields, **data}
        return cls(spec=HallSpec.model_validate(data), cables=cables, faults=faults)

    def to_mapping(self) -> dict[str, Any]:
        doc: dict[str, Any] = self.spec.model_dump(mode="json")
        if self.cables is not None:
            doc["cables"] = [c.model_dump(mode="json") for c in self.cables]
        if self.faults:
            doc["faults"] = [
                {k: v for k, v in f.summary().items() if k not in ("id", "created_at")} for f in self.faults
            ]
        return doc

    def build(self, base_url: str | None = None) -> Hall:
        spec = self.spec if base_url is None else self.spec.model_copy(update={"base_url": base_url})
        hall = Hall.generate(spec, cables=self.cables)
        for f in self.faults:
            hall.inject(f)
        return hall


def load_spec_file(path: str | Path) -> SpecFile:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    doc = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{p}: spec file must be a mapping")
    return SpecFile.from_mapping(doc)


def dump_spec_file(spec_file: SpecFile, path: str | Path | None = None) -> str:
    text = yaml.safe_dump(spec_file.to_mapping(), sort_keys=False, default_flow_style=None, width=120)
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text
