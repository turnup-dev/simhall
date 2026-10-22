# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""`simhall` command line: serve, intent, golden-spec, technician, seed-netbox, fault, act, faults, reset."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

import click
import httpx
import yaml

from simhall import __version__
from simhall.golden import GOLDEN_NAMES, build_golden, golden_cables, golden_faults, golden_spec
from simhall.hall import Hall
from simhall.specfile import SpecFile, dump_spec_file, load_spec_file


def _builder(
    golden: str | None, spec_path: str | None, base_url: str | None = None
) -> tuple[Callable[[], Hall], str]:
    """A hall builder from `--golden` or `--spec`; the golden `hall-b` when neither is given."""
    if golden and spec_path:
        raise click.UsageError("use either --golden or --spec, not both")
    if spec_path:
        spec_file = load_spec_file(spec_path)
        return (lambda: spec_file.build(base_url)), f"spec {spec_path}"
    name = golden or "hall-b"
    if name not in GOLDEN_NAMES:
        raise click.UsageError(f"unknown golden scenario {name!r}; known: {', '.join(GOLDEN_NAMES)}")
    return (lambda: build_golden(name, base_url)), f"golden {name}"


@click.group()
@click.version_option(__version__, prog_name="simhall")
def main() -> None:
    """simhall: a virtual GPU hall (Redfish, LLDP switches, DCGM, NetBox) with fault injection."""


@main.command()
@click.option(
    "--golden",
    default=None,
    help=f"Built-in scenario ({', '.join(GOLDEN_NAMES)}). Default when --spec is absent.",
)
@click.option(
    "--spec", "spec_path", type=click.Path(exists=True, dir_okay=False), default=None, help="Spec YAML/JSON."
)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8900, show_default=True, type=int)
@click.option(
    "--timeout-sleep", default=10.0, show_default=True, type=float, help="Seconds a 'timeout' device stalls."
)
@click.option("--log-level", default="info", show_default=True)
def serve(
    golden: str | None, spec_path: str | None, host: str, port: int, timeout_sleep: float, log_level: str
) -> None:
    """Serve the hall on 127.0.0.1 (vendor protocols, NetBox API and the /sim control plane)."""
    from simhall.server import serve as _serve  # noqa: PLC0415 - keeps `simhall --help` fast

    builder, label = _builder(golden, spec_path, f"http://{host}:{port}")
    click.echo(f"simhall {__version__}: serving {label} on http://{host}:{port}", err=True)
    click.echo(f"  NetBox API    NETBOX_URL=http://{host}:{port}/netbox", err=True)
    click.echo(f"  intent        http://{host}:{port}/sim/intent", err=True)
    click.echo(f"  control plane http://{host}:{port}/sim/faults  /sim/actions  /sim/reset", err=True)
    _serve(builder, host=host, port=port, timeout_sleep_s=timeout_sleep, log_level=log_level)


@main.command()
@click.option("--golden", default=None, help="Built-in scenario.")
@click.option("--spec", "spec_path", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option(
    "--base-url", default=None, help="mgmt URL prefix (default: the spec's base_url, http://127.0.0.1:8900)."
)
@click.option(
    "-o",
    "--out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write here (.yaml/.yml or .json) instead of stdout.",
)
def intent(golden: str | None, spec_path: str | None, base_url: str | None, out: str | None) -> None:
    """Print or write the turnup.intent/v1 document (simulated: true)."""
    builder, _ = _builder(golden, spec_path, base_url)
    hall = builder()
    if out:
        path = hall.write_intent(out)
        click.echo(
            f"wrote {path} ({len(hall.intent_cables)} cables, {len(hall.nodes)} nodes, "
            f"{len(hall.leaves)} leaves)",
            err=True,
        )
    else:
        click.echo(yaml.safe_dump(hall.intent(), sort_keys=False, default_flow_style=False), nl=False)


@main.command("golden-spec")
@click.argument("name", default="hall-b")
@click.option("-o", "--out", type=click.Path(dir_okay=False), default=None)
def golden_spec_cmd(name: str, out: str | None) -> None:
    """Export a golden scenario as a spec file (explicit cables and faults included)."""
    if name not in GOLDEN_NAMES:
        raise click.UsageError(f"unknown golden scenario {name!r}; known: {', '.join(GOLDEN_NAMES)}")
    spec = golden_spec(name)
    spec_file = SpecFile(spec=spec, cables=golden_cables(spec), faults=golden_faults())
    text = dump_spec_file(spec_file, out)
    if out:
        click.echo(f"wrote {out}", err=True)
    else:
        click.echo(text, nl=False)


@main.command()
@click.option("--acceptance-url", default="http://127.0.0.1:8800", show_default=True)
@click.option(
    "--sim-url", default="http://127.0.0.1:8900", show_default=True, help="simhall server to act on."
)
@click.option("--worker", default="tech-1", show_default=True)
@click.option("--error-rate", default=0.05, show_default=True, type=float)
@click.option(
    "--speed",
    default=20.0,
    show_default=True,
    type=float,
    help="Simulation speed factor (20 = a 60 s step takes 3 s).",
)
@click.option("--rack", default=None, help="Only pull steps for this rack.")
@click.option("--seed", default=None, type=int, help="Seed for reproducible mistakes.")
@click.option(
    "--approver", default=None, help="If set, approve requires_approval steps as this named person."
)
@click.option("--max-steps", default=None, type=int)
@click.option(
    "--idle-polls", default=5, show_default=True, type=int, help="Consecutive empty polls before exiting."
)
@click.option(
    "--poll-interval", default=1.0, show_default=True, type=float, help="Seconds between empty polls."
)
@click.option("--json", "as_json", is_flag=True, help="Print the stats as JSON.")
def technician(
    acceptance_url: str,
    sim_url: str,
    worker: str,
    error_rate: float,
    speed: float,
    rack: str | None,
    seed: int | None,
    approver: str | None,
    max_steps: int | None,
    idle_polls: int,
    poll_interval: float,
    as_json: bool,
) -> None:
    """Run a simulated technician against the Acceptance API, acting on the simhall server."""
    from simhall.technician import run_bot  # noqa: PLC0415

    stats = asyncio.run(
        run_bot(
            acceptance_url,
            sim_url=sim_url,
            worker=worker,
            error_rate=error_rate,
            speed=speed,
            rack=rack,
            seed=seed,
            approver=approver,
            max_steps=max_steps,
            idle_polls=idle_polls,
            poll_interval_s=poll_interval,
        )
    )
    if as_json:
        click.echo(json.dumps(stats.as_dict(), indent=2))
    else:
        d = stats.as_dict()
        click.echo(
            f"{worker}: {d['done']} steps done, {d['mistakes']} mistakes "
            f"({d['wrong_port']} wrong port, {d['unseated']} unseated), {d['errors']} errors"
        )


@main.command("seed-netbox")
@click.option("--netbox-url", required=True, envvar="NETBOX_URL", help="e.g. http://127.0.0.1:8000")
@click.option("--token", required=False, envvar="NETBOX_TOKEN", default=None)
@click.option("--golden", default=None)
@click.option("--spec", "spec_path", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option(
    "--intent",
    "intent_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Seed from an intent file instead.",
)
@click.option(
    "--base-url",
    default="http://127.0.0.1:8900",
    show_default=True,
    help="mgmt URL prefix written into custom fields.",
)
@click.option("--dry-run", is_flag=True, help="Print the plan; do not touch NetBox (no pynetbox import).")
def seed_netbox_cmd(
    netbox_url: str,
    token: str | None,
    golden: str | None,
    spec_path: str | None,
    intent_path: str | None,
    base_url: str,
    dry_run: bool,
) -> None:
    """Seed a real NetBox (netbox-docker) with the hall via pynetbox."""
    from simhall.seed_netbox import connect, plan_from_intent, seed  # noqa: PLC0415

    if intent_path:
        text = Path(intent_path).read_text(encoding="utf-8")
        doc = json.loads(text) if intent_path.endswith(".json") else yaml.safe_load(text)
    else:
        builder, _ = _builder(golden, spec_path, base_url)
        doc = builder().intent()
    if dry_run:
        plan = plan_from_intent(doc)
        click.echo(f"dry run against {netbox_url}: would create {json.dumps(plan.counts())}")
        for d in plan.devices[:3]:
            click.echo(
                f"  device {d['name']} ({d['role']}, {d['device_type']}) "
                f"custom_fields={json.dumps(d['custom_fields'])}"
            )
        for c in plan.cables[:3]:
            click.echo(
                f"  cable {c['label']}: {c['a']['device']} {c['a']['interface']} "
                f"-> {c['b']['device']} {c['b']['interface']}"
            )
        return
    if not token:
        raise click.UsageError("--token (or NETBOX_TOKEN) is required unless --dry-run")
    nb = connect(netbox_url, token)
    summary = seed(doc, nb, log=lambda m: click.echo(m, err=True))
    click.echo(json.dumps({k: v for k, v in summary.items() if k != "created_items"}))


def _control(url: str, method: str, path: str, body: str | None = None) -> None:
    payload = json.loads(body) if body else None
    with httpx.Client(timeout=10.0) as client:
        r = client.request(method, f"{url.rstrip('/')}{path}", json=payload)
    try:
        click.echo(json.dumps(r.json(), indent=2))
    except ValueError:
        click.echo(r.text)
    if r.status_code >= 400:
        sys.exit(1)


@main.command()
@click.option("--url", default="http://127.0.0.1:8900", show_default=True)
@click.argument("fault_json")
def fault(url: str, fault_json: str) -> None:
    """Inject a fault from JSON, e.g. '{"kind":"missing_link","cable":"CBL-12-07-03"}'."""
    _control(url, "POST", "/sim/faults", fault_json)


@main.command()
@click.option("--url", default="http://127.0.0.1:8900", show_default=True)
@click.argument("action_json")
def act(url: str, action_json: str) -> None:
    """Perform an action: simhall act '{"kind":"seat_cable","label":"CBL-12-07-03","b_interface":"swp21"}'."""
    _control(url, "POST", "/sim/actions", action_json)


@main.command()
@click.option("--url", default="http://127.0.0.1:8900", show_default=True)
@click.option("--clear", "clear_id", default=None, help="Clear this fault id instead of listing.")
def faults(url: str, clear_id: str | None) -> None:
    """List active faults, or clear one with --clear ID."""
    if clear_id:
        _control(url, "DELETE", f"/sim/faults/{clear_id}")
    else:
        _control(url, "GET", "/sim/faults")


@main.command()
@click.option("--url", default="http://127.0.0.1:8900", show_default=True)
def reset(url: str) -> None:
    """Rebuild the hall as served at start-up (the golden fault comes back)."""
    _control(url, "POST", "/sim/reset")


if __name__ == "__main__":  # pragma: no cover
    main()
