# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""The `simhall` CLI (everything except long-running `serve`, which the HTTP tests cover in-process)."""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from simhall import __version__
from simhall.cli import main
from simhall.specfile import SpecFile


def test_version_and_help() -> None:
    runner = CliRunner()
    assert __version__ in runner.invoke(main, ["--version"]).output
    for cmd in (
        "serve",
        "intent",
        "golden-spec",
        "technician",
        "seed-netbox",
        "fault",
        "act",
        "faults",
        "reset",
    ):
        r = runner.invoke(main, [cmd, "--help"])
        assert r.exit_code == 0, cmd


def test_intent_command_writes_the_golden_intent(tmp_path) -> None:
    runner = CliRunner()
    out = tmp_path / "hall-b.yaml"
    r = runner.invoke(
        main, ["intent", "--golden", "hall-b", "-o", str(out), "--base-url", "http://127.0.0.1:8900"]
    )
    assert r.exit_code == 0, r.output
    doc = yaml.safe_load(out.read_text())
    assert doc["schema"] == "turnup.intent/v1" and doc["simulated"] is True and len(doc["cables"]) == 288
    node7 = next(d for d in doc["devices"] if d["name"] == "node7")
    assert node7["mgmt"]["bmc_url"] == "http://127.0.0.1:8900/dev/node7"
    printed = runner.invoke(main, ["intent"])
    assert printed.exit_code == 0 and yaml.safe_load(printed.output)["site"] == "hall-b"
    bad = runner.invoke(main, ["intent", "--golden", "hall-z"])
    assert bad.exit_code != 0 and "unknown golden" in bad.output


def test_golden_spec_export_and_serve_from_spec(tmp_path) -> None:
    runner = CliRunner()
    r = runner.invoke(main, ["golden-spec", "hall-b"])
    assert r.exit_code == 0, r.output
    spec_file = SpecFile.from_mapping(yaml.safe_load(r.output))
    assert spec_file.cables is not None and len(spec_file.cables) == 288
    assert [f.kind for f in spec_file.faults] == ["wrong_port"]
    hall = spec_file.build("http://127.0.0.1:1234")
    assert hall.intent_cables["CBL-12-07-03"].b.interface == "swp21"
    assert hall.intent()["devices"][0]["mgmt"]["bmc_url"].startswith("http://127.0.0.1:1234/")
    path = tmp_path / "spec.yaml"
    assert runner.invoke(main, ["golden-spec", "hall-b", "-o", str(path)]).exit_code == 0
    r2 = runner.invoke(main, ["intent", "--spec", str(path)])
    assert r2.exit_code == 0 and len(yaml.safe_load(r2.output)["cables"]) == 288
    both = runner.invoke(main, ["intent", "--spec", str(path), "--golden", "hall-b"])
    assert both.exit_code != 0


def test_seed_netbox_dry_run_needs_no_token_and_no_pynetbox() -> None:
    runner = CliRunner()
    r = runner.invoke(
        main, ["seed-netbox", "--netbox-url", "http://127.0.0.1:8000", "--golden", "hall-b", "--dry-run"]
    )
    assert r.exit_code == 0, r.output
    assert '"devices": 20' in r.output and '"cables": 288' in r.output and '"interfaces": 608' in r.output
    assert "CBL-12-" in r.output
    no_token = runner.invoke(
        main, ["seed-netbox", "--netbox-url", "http://127.0.0.1:8000"], env={"NETBOX_TOKEN": ""}
    )
    assert no_token.exit_code != 0 and "--token" in no_token.output
