# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""One FastAPI server for the whole hall (CONTRACT.md section 6.3).

- `/dev/<device>/...`: Redfish and DCGM for nodes, eAPI (`/command-api`) and NVUE (`/nvue_v1/...`) for leaves,
  rendered straight from `Hall.redfish_tree`, `Hall.dcgm_metrics` and `Hall.lldp_table`.
- `/netbox/api/...`: the minimal NetBox API, so `NETBOX_URL=http://127.0.0.1:8900/netbox` works.
- `/sim/...`: the control plane (intent, state, faults, actions, reset).

Every handler is `async def` and the only `await` is the deliberate `timeout` sleep, so control-plane
mutations never race device reads.
"""

from __future__ import annotations

import asyncio
import copy
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError

from simhall import __version__
from simhall.emulators import dcgm as dcgm_emu
from simhall.emulators import lldp as lldp_emu
from simhall.emulators import netbox as netbox_emu
from simhall.emulators import redfish as redfish_emu
from simhall.hall import NODE_ROLE, Hall, SimError

HallBuilder = Callable[[], Hall]


class SimState:
    """What the app holds: the live hall, how to rebuild it, and the NetBox view cache."""

    def __init__(self, builder: HallBuilder, timeout_sleep_s: float, initial: Hall | None = None) -> None:
        self.builder = builder
        self.timeout_sleep_s = timeout_sleep_s
        self.hall: Hall = initial if initial is not None else builder()
        self._netbox: netbox_emu.NetBoxView | None = None
        self.started_at = time.time()
        self.resets = 0

    def set_base_url(self, base_url: str) -> None:
        self.hall.base_url = base_url
        self._netbox = None

    def netbox(self) -> netbox_emu.NetBoxView:
        if self._netbox is None or self._netbox.base_url != self.hall.base_url:
            self._netbox = netbox_emu.NetBoxView(self.hall)
        return self._netbox

    def reset(self) -> Hall:
        base_url = self.hall.base_url
        self.hall = self.builder()
        self.hall.base_url = base_url
        self._netbox = None
        self.resets += 1
        return self.hall


def builder_for(hall: Hall) -> HallBuilder:
    """A builder that rebuilds `hall` as it is right now (intent cabling plus its current faults)."""
    spec = hall.spec
    cables = [c.model_copy(deep=True) for c in hall.intent_cables.values()]
    faults = [f.model_copy(update={"id": None, "created_at": None}) for f in hall.faults()]
    golden = hall.golden

    def build() -> Hall:
        h = Hall.generate(spec, cables=copy.deepcopy(cables), golden=golden)
        for f in faults:
            h.inject(f)
        return h

    return build


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


def _query_params(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in request.query_params.multi_items():
        out.setdefault(k, []).append(v)
    return out


def create_app(builder: HallBuilder | Hall, *, timeout_sleep_s: float = 10.0) -> FastAPI:
    """Build the FastAPI app.

    With a builder, it is called once now and again on every `POST /sim/reset`. With a `Hall`, that
    object itself is served (so a test can drive it in-process while clients poll over HTTP) and reset
    rebuilds an equivalent hall from its intent cabling and current faults.
    """
    initial: Hall | None = None
    if isinstance(builder, Hall):
        initial = builder  # serve this very object; in-process actions show up over HTTP
        builder = builder_for(builder)
    app = FastAPI(title="simhall", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = SimState(builder, timeout_sleep_s, initial=initial)

    def sim() -> SimState:
        return app.state.sim

    @app.exception_handler(SimError)
    async def _sim_error(_request: Request, exc: SimError) -> JSONResponse:
        return _error(exc.status, str(exc))

    @app.exception_handler(ValidationError)
    async def _validation_error(_request: Request, exc: ValidationError) -> JSONResponse:
        return _error(
            400, "invalid body", details=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        )

    # ------------------------------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------------------------------

    @app.get("/")
    async def index() -> dict[str, Any]:
        hall = sim().hall
        return {
            "simhall": __version__,
            "site": hall.site,
            "golden": hall.golden,
            "base_url": hall.base_url,
            "devices": {"nodes": len(hall.nodes), "leaves": len(hall.leaves)},
            "routes": {
                "redfish": "/dev/<node>/redfish/v1/",
                "dcgm": "/dev/<node>/metrics",
                "eapi": "POST /dev/<leaf>/command-api",
                "nvue": "/dev/<leaf>/nvue_v1/interface?rev=applied",
                "netbox": "/netbox/api/dcim/{sites,racks,devices,interfaces,cables}/",
                "control": "/sim/{intent,state,spec,faults,actions,reset,history,health}",
            },
        }

    # ------------------------------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------------------------------

    @app.api_route("/dev/{device}/{path:path}", methods=["GET", "POST", "HEAD"])
    async def device_route(device: str, path: str, request: Request) -> Response:
        st = sim()
        hall = st.hall
        role = hall.device_role(device)
        if role is None:
            return _error(404, f"device {device} is not registered")
        norm = path.strip("/")
        proto = ("dcgm" if norm == "metrics" else "redfish") if role == NODE_ROLE else "switch"
        mode = hall.reach(device, proto)
        if mode == "timeout":
            await asyncio.sleep(st.timeout_sleep_s)
            return _error(504, f"device {device} timed out")
        if mode == "http_500":
            return _error(500, "internal server error")
        if mode == "refused":
            return _error(404, f"device {device} is not reachable")

        if role == NODE_ROLE:
            if proto == "dcgm":
                return PlainTextResponse(hall.dcgm_metrics(device), media_type=dcgm_emu.CONTENT_TYPE)
            if norm == "redfish":
                return JSONResponse({"v1": "/redfish/v1/"})
            if not norm.startswith("redfish/v1"):
                return _error(404, f"{device}: unknown path /{norm}")
            if request.method == "POST":
                return JSONResponse(
                    status_code=405,
                    content=redfish_emu.error_body(
                        "Base.1.0.OperationNotAllowed", "simhall BMCs are read-only"
                    ),
                    headers={"Allow": "GET"},
                )
            key = redfish_emu.normalize_path(norm)
            resource = hall.redfish_tree(device).get(key)
            if resource is None:
                return JSONResponse(
                    status_code=404,
                    content=redfish_emu.error_body(
                        "Base.1.0.ResourceMissingAtURI", f"The resource at {key} was not found."
                    ),
                )
            return JSONResponse(resource, headers={"OData-Version": "4.0", "Allow": "GET"})

        leaf = hall.leaves[device]
        if norm == "command-api":
            if leaf.vendor != "arista":
                return _error(404, f"{device} does not speak eAPI")
            if request.method != "POST":
                return _error(405, "eAPI expects POST", allow="POST")
            try:
                body = await request.json()
            except ValueError:
                body = None
            return JSONResponse(lldp_emu.eapi_response(body, leaf, hall.lldp_table(device)))
        if norm == "nvue_v1" or norm.startswith("nvue_v1/"):
            if leaf.vendor != "nvidia":
                return _error(404, f"{device} does not speak NVUE")
            if request.method != "GET":
                return _error(405, "simhall NVUE is read-only", allow="GET")
            rest = norm[len("nvue_v1") :].strip("/")
            if rest == "":
                return JSONResponse({"interface": {}, "system": {}})
            if rest == "interface":
                return JSONResponse(hall.lldp_table(device))
            if rest.startswith("interface/"):
                name = rest.split("/", 1)[1]
                table = hall.lldp_table(device)
                if name not in table:
                    return _error(404, f"interface {name} not found")
                return JSONResponse(table[name])
            if rest == "system":
                return JSONResponse(lldp_emu.nvue_system(leaf))
            return _error(404, f"{device}: unknown NVUE path /{norm}")
        return _error(404, f"{device}: unknown path /{norm}")

    # ------------------------------------------------------------------------------------------
    # NetBox
    # ------------------------------------------------------------------------------------------

    @app.get("/netbox/api")
    @app.get("/netbox/api/")
    async def netbox_root() -> dict[str, Any]:
        return netbox_emu.api_root(sim().hall.base_url)

    @app.get("/netbox/api/status")
    @app.get("/netbox/api/status/")
    async def netbox_status() -> dict[str, Any]:
        return netbox_emu.status_body()

    @app.get("/netbox/api/dcim")
    @app.get("/netbox/api/dcim/")
    async def netbox_dcim() -> dict[str, Any]:
        return netbox_emu.dcim_root(sim().hall.base_url)

    @app.get("/netbox/api/dcim/{rest:path}")
    async def netbox_dcim_route(rest: str, request: Request) -> Response:
        parts = [p for p in rest.split("/") if p]
        if not parts or parts[0] not in netbox_emu.COLLECTIONS or len(parts) > 2:
            return JSONResponse(status_code=404, content={"detail": "Not found."})
        view = sim().netbox()
        collection = parts[0]
        if len(parts) == 2:
            try:
                obj_id = int(parts[1])
            except ValueError:
                return JSONResponse(status_code=404, content={"detail": "Not found."})
            obj = view.get(collection, obj_id)
            if obj is None:
                return JSONResponse(status_code=404, content={"detail": "Not found."})
            return JSONResponse(obj)

        def url_builder(offset: int | None, limit: int) -> str:
            url = request.url.include_query_params(limit=limit)
            url = (
                url.include_query_params(offset=offset)
                if offset is not None
                else url.remove_query_params("offset")
            )
            return str(url)

        return JSONResponse(view.list(collection, _query_params(request), url_builder))

    # ------------------------------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------------------------------

    @app.get("/sim/health")
    async def sim_health() -> dict[str, Any]:
        st = sim()
        return {
            "ok": True,
            "site": st.hall.site,
            "uptime_s": round(time.time() - st.started_at, 1),
            "resets": st.resets,
        }

    @app.get("/sim/spec")
    async def sim_spec() -> dict[str, Any]:
        st = sim()
        return {
            "spec": st.hall.spec.model_dump(mode="json"),
            "golden": st.hall.golden,
            "base_url": st.hall.base_url,
            "timeout_sleep_s": st.timeout_sleep_s,
            "version": __version__,
            "explicit_cables": len(st.hall.intent_cables),
        }

    @app.get("/sim/intent")
    async def sim_intent() -> dict[str, Any]:
        return sim().hall.intent()

    @app.get("/sim/state")
    async def sim_state() -> dict[str, Any]:
        return sim().hall.state()

    @app.get("/sim/history")
    async def sim_history() -> list[dict[str, Any]]:
        return sim().hall.history()

    @app.get("/sim/faults")
    async def sim_faults() -> list[dict[str, Any]]:
        return [f.summary() for f in sim().hall.faults()]

    @app.post("/sim/faults", status_code=201)
    async def sim_inject(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object with a 'kind'")
        fault = sim().hall.inject(body)
        return JSONResponse(status_code=201, content=fault.summary())

    @app.delete("/sim/faults/{fault_id}")
    async def sim_clear(fault_id: str) -> dict[str, Any]:
        fault = sim().hall.clear(fault_id)
        return {"cleared": fault.summary()}

    @app.post("/sim/actions")
    async def sim_act(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object with a 'kind'")
        return JSONResponse(sim().hall.act(body))

    @app.post("/sim/reset")
    async def sim_reset() -> dict[str, Any]:
        hall = sim().reset()
        return {
            "ok": True,
            "site": hall.site,
            "golden": hall.golden,
            "faults": [f.summary() for f in hall.faults()],
        }

    return app


# --------------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def serve(
    builder: HallBuilder | Hall,
    host: str = "127.0.0.1",
    port: int = 8900,
    *,
    timeout_sleep_s: float = 10.0,
    log_level: str = "info",
) -> None:
    """Run the server in the foreground; the hall's `base_url` follows `host:port`."""
    app = create_app(builder, timeout_sleep_s=timeout_sleep_s)
    app.state.sim.set_base_url(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


class ServerThread:
    """A uvicorn server on a background thread, for tests and the technician end-to-end runs.

    ```python
    with ServerThread(create_app(build_golden)) as srv:
        httpx.get(f"{srv.base_url}/sim/intent")
    ```
    """

    def __init__(
        self, app: FastAPI, host: str = "127.0.0.1", port: int = 0, graceful_shutdown_s: float = 1.0
    ) -> None:
        self.host = host
        self.port = port or free_port(host)
        self.base_url = f"http://{self.host}:{self.port}"
        self.app = app
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=graceful_shutdown_s,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name=f"simhall-{self.port}", daemon=True)

    def start(self, wait_s: float = 10.0) -> ServerThread:
        sim = getattr(self.app.state, "sim", None)
        if sim is not None:
            sim.set_base_url(self.base_url)
        self.thread.start()
        deadline = time.time() + wait_s
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("simhall server did not start in time")
            if not self.thread.is_alive():
                raise RuntimeError("simhall server thread exited during startup")
            time.sleep(0.01)
        return self

    def stop(self, wait_s: float = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(wait_s)

    def __enter__(self) -> ServerThread:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
