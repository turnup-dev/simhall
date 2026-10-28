# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Shared fixtures: fresh golden halls, a small Arista hall, and an in-process HTTP helper."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from simhall.golden import build_golden, golden_cables, golden_spec
from simhall.hall import Hall
from simhall.models import HallSpec

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_TIME = "2026-10-16T18:00:00Z"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def subset(expected: Any, actual: Any, path: str = "$") -> None:
    """Assert every key/value in `expected` appears in `actual` (recursively; lists element-wise)."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {type(actual).__name__}"
        for k, v in expected.items():
            assert k in actual, f"{path}: missing key {k!r} (have {sorted(actual)})"
            subset(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list"
        assert len(actual) >= len(expected), f"{path}: list too short"
        for i, v in enumerate(expected):
            subset(v, actual[i], f"{path}[{i}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


@pytest.fixture
def golden() -> Hall:
    """hall-b with its five start-up faults (wrong_port first), as `simhall serve --golden hall-b` builds it."""
    return build_golden()


@pytest.fixture
def clean_golden() -> Hall:
    """hall-b built exactly to intent (no fault)."""
    return build_golden(with_faults=False)


@pytest.fixture
def arista_golden() -> Hall:
    """The golden cabling on Arista leaves (node7 mlx5_3 -> leaf4/Ethernet21), built to intent."""
    spec = golden_spec().model_copy(update={"switch_vendor": "arista", "generated_at": FIXED_TIME})
    return Hall.generate(spec, cables=golden_cables(spec))


@pytest.fixture
def small_spec() -> HallSpec:
    return HallSpec(
        site="hall-s",
        racks=1,
        nodes_per_rack=3,
        ports_per_node=4,
        leaves_per_rack=2,
        spare_ports=4,
        generated_at=FIXED_TIME,
    )


def asgi_request(app: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One request against an ASGI app without a socket."""

    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sim") as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(go())


@pytest.fixture
def asgi() -> Callable[..., httpx.Response]:
    return asgi_request
