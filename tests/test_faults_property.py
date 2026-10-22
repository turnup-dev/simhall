# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""Property tests: the emulated LLDP / Redfish / DCGM views reflect injected faults exactly, nothing
else changes, and clearing every fault (in any order) restores the pristine views byte for byte."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from simhall import naming
from simhall.faults import Fault
from simhall.hall import Hall
from simhall.models import HallSpec
from simhall.views import (
    PortRecord,
    dcgm_table,
    normalize_lldp,
    redfish_firmware,
    redfish_leak_states,
    redfish_ports,
    redfish_system,
)

NODE_KINDS = (
    "unreachable_node",
    "partial_redfish",
    "firmware_off_baseline",
    "gpu_unhealthy",
    "gpu_missing",
    "nvlink_degraded",
    "node_health_warning",
    "coolant_leak",
    "pdu_outlet_off",
)
CABLE_KINDS = ("wrong_port", "missing_link", "swap_pair", "link_degraded")
LEAF_KINDS = ("lldp_port_id_mac",)
ALL_KINDS = NODE_KINDS + CABLE_KINDS + LEAF_KINDS

specs = st.builds(
    HallSpec,
    site=st.just("hall-p"),
    racks=st.integers(1, 3),
    nodes_per_rack=st.integers(2, 18),
    ports_per_node=st.integers(4, 16),
    leaves_per_rack=st.integers(1, 4),
    switch_vendor=st.sampled_from(["nvidia", "arista"]),
    lldp_port_id_mode=st.sampled_from(["name", "mac"]),
    spare_ports=st.integers(1, 6),
    seed=st.integers(0, 2**16),
    generated_at=st.just("2026-10-16T18:00:00Z"),
)


def snapshot(hall: Hall) -> dict[str, Any]:
    return {
        "redfish": {n: hall.redfish_tree(n) for n in hall.nodes},
        "dcgm": {n: hall.dcgm_metrics(n) for n in hall.nodes},
        "lldp": {lf: hall.lldp_table(lf) for lf in hall.leaves},
        "reach": {d: (hall.reach(d, "redfish"), hall.reach(d, "dcgm")) for d in hall.nodes},
    }


def draw_faults(data: st.DataObject, hall: Hall) -> list[Fault]:
    """One to six faults with disjoint targets, so each has an independent oracle."""
    kinds = data.draw(st.lists(st.sampled_from(ALL_KINDS), min_size=1, max_size=6), label="kinds")
    node_pool = sorted(hall.nodes)
    leaf_pool = sorted(hall.leaves)
    out: list[Fault] = []
    for kind in (k for k in kinds if k in NODE_KINDS):
        if not node_pool:
            break
        node = data.draw(st.sampled_from(node_pool), label=f"node for {kind}")
        node_pool.remove(node)
        gpu_count = hall.nodes[node].gpu_count
        if kind == "unreachable_node":
            mode = data.draw(st.sampled_from(["timeout", "http_500", "refused"]), label="mode")
            scope = data.draw(st.sampled_from(["all", "redfish", "dcgm"]), label="scope")
            out.append(Fault(kind=kind, node=node, mode=mode, scope=scope))
        elif kind == "firmware_off_baseline":
            comp = data.draw(st.sampled_from(["bmc", "bios", "nic"]), label="component")
            out.append(Fault(kind=kind, node=node, component=comp, version="0.0.9"))
        elif kind in ("gpu_unhealthy", "gpu_missing"):
            gpu = data.draw(st.integers(0, gpu_count - 1), label="gpu")
            out.append(
                Fault(
                    kind=kind, node=node, gpu=gpu, xid=data.draw(st.sampled_from([13, 31, 79]), label="xid")
                )
            )
        elif kind == "coolant_leak":
            rack = hall.nodes[node].rack
            rack_nodes = [n.name for n in hall.nodes_in_rack(rack)]
            whole_rack = all(n in node_pool or n == node for n in rack_nodes) and data.draw(
                st.booleans(), label="rack leak"
            )
            if whole_rack:
                for n in rack_nodes:
                    if n in node_pool:
                        node_pool.remove(n)
                out.append(Fault(kind=kind, rack=rack))
            else:
                out.append(Fault(kind=kind, node=node))
        else:
            out.append(Fault(kind=kind, node=node))
    for kind in (k for k in kinds if k in LEAF_KINDS):
        if not leaf_pool:
            break
        leaf = data.draw(st.sampled_from(leaf_pool), label="leaf for mac mode")
        leaf_pool.remove(leaf)
        out.append(Fault(kind=kind, leaf=leaf))
    cable_pool = sorted(c.label for c in hall.intent_cables.values() if c.a.device in node_pool)
    taken_ports: set[tuple[str, str]] = set()
    for kind in (k for k in kinds if k in CABLE_KINDS):
        if not cable_pool:
            break
        label = data.draw(st.sampled_from(cable_pool), label=f"cable for {kind}")
        cable_pool.remove(label)
        if kind == "wrong_port":
            leaf = data.draw(st.sampled_from(sorted(hall.leaves)), label="target leaf")
            spares = [p for p in hall.spare_ports(leaf) if (leaf, p) not in taken_ports]
            if not spares:
                continue
            port = data.draw(st.sampled_from(spares), label="target port")
            taken_ports.add((leaf, port))
            out.append(Fault(kind=kind, cable=label, to_device=leaf, to_interface=port))
        elif kind == "swap_pair":
            if not cable_pool:
                continue
            other = data.draw(st.sampled_from(cable_pool), label="swap partner")
            cable_pool.remove(other)
            out.append(Fault(kind=kind, cable_a=label, cable_b=other))
        elif kind == "link_degraded":
            speed = data.draw(st.sampled_from([200, "down"]), label="degraded speed")
            out.append(Fault(kind=kind, cable=label, speed_gbps=speed))
        else:
            out.append(Fault(kind=kind, cable=label))
    return out


def _mac_mode(hall: Hall, leaf: str, faults: list[Fault]) -> bool:
    return hall.spec.lldp_port_id_mode == "mac" or any(
        f.kind == "lldp_port_id_mac" and f.leaf == leaf for f in faults
    )


def _expect_neighbor(
    hall: Hall, rec: PortRecord, node: str, iface: str, leaf: str, faults: list[Fault]
) -> None:
    assert rec.remote_system == node
    assert rec.remote_chassis == hall.nodes[node].chassis_mac
    if _mac_mode(hall, leaf, faults):
        assert rec.remote_port_is_mac and rec.remote_port == hall.nodes[node].macs[iface]
    else:
        assert not rec.remote_port_is_mac and rec.remote_port == iface


def _port_no(ifname: str) -> int:
    n = naming.port_number(ifname)
    assert n is not None
    return n


def check_fault(hall: Hall, f: Fault, faults: list[Fault], lldp: dict[str, dict[int, PortRecord]]) -> None:
    """Oracle for one fault, read only through the emulated views."""
    if f.kind in ("wrong_port", "missing_link", "link_degraded"):
        c = hall.intent_cables[str(f.cable)]
        node, iface, leaf, port = c.a.device, c.a.interface, c.b.device, _port_no(c.b.interface)
        intended = lldp[leaf][port]
        rport = redfish_ports(hall, node)[iface]
        if f.kind == "wrong_port":
            assert intended.link_up is False and intended.remote_system is None
            actual = lldp[str(f.to_device)][_port_no(str(f.to_interface))]
            assert actual.link_up and actual.speed_gbps == c.speed_gbps
            _expect_neighbor(hall, actual, node, iface, str(f.to_device), faults)
            assert (
                rport["LinkStatus"] == "LinkUp"
                and rport["Ethernet"]["LLDPReceive"]["PortId"] == f.to_interface
            )
            assert rport["Ethernet"]["LLDPReceive"]["SystemName"] == f.to_device
        elif f.kind == "missing_link":
            assert intended.link_up is False and intended.remote_system is None
            assert rport["LinkStatus"] == "NoLink" and rport["Ethernet"]["LLDPReceive"]["SystemName"] is None
        elif f.speed_gbps == "down":
            assert intended.link_up is False
            _expect_neighbor(hall, intended, node, iface, leaf, faults)
            assert (
                rport["LinkStatus"] == "LinkDown" and rport["Ethernet"]["LLDPReceive"]["SystemName"] == leaf
            )
        else:
            assert intended.link_up and intended.speed_gbps == f.speed_gbps
            _expect_neighbor(hall, intended, node, iface, leaf, faults)
            assert rport["LinkStatus"] == "LinkUp" and rport["CurrentSpeedGbps"] == f.speed_gbps
    elif f.kind == "swap_pair":
        a, b = hall.intent_cables[str(f.cable_a)], hall.intent_cables[str(f.cable_b)]
        at_a = lldp[a.b.device][_port_no(a.b.interface)]
        at_b = lldp[b.b.device][_port_no(b.b.interface)]
        _expect_neighbor(hall, at_a, b.a.device, b.a.interface, a.b.device, faults)
        _expect_neighbor(hall, at_b, a.a.device, a.a.interface, b.b.device, faults)
        assert (
            redfish_ports(hall, a.a.device)[a.a.interface]["Ethernet"]["LLDPReceive"]["PortId"]
            == b.b.interface
        )
    elif f.kind == "unreachable_node":
        node = str(f.node)
        want_redfish = f.mode if f.scope in ("all", "redfish") else "ok"
        want_dcgm = f.mode if f.scope in ("all", "dcgm") else "ok"
        assert (hall.reach(node, "redfish"), hall.reach(node, "dcgm")) == (want_redfish, want_dcgm)
        for c in hall.cables_of_node(node):  # fabric links stay up
            assert lldp[c.b.device][_port_no(c.b.interface)].link_up
    elif f.kind == "partial_redfish":
        assert redfish_firmware(hall, str(f.node)) is None
        assert redfish_system(hall, str(f.node))["Status"]["Health"] == "OK"
    elif f.kind == "firmware_off_baseline":
        inv = redfish_firmware(hall, str(f.node))
        assert inv is not None
        names = {"bmc": "BMC Firmware", "bios": "System BIOS", "nic": "ConnectX-8 NIC Firmware"}
        assert inv[names[str(f.component)]] == f.version
        baseline = hall.spec.firmware_baseline
        for comp, name in names.items():
            if comp != f.component:
                assert inv[name] == baseline[comp]
    elif f.kind == "gpu_unhealthy":
        table = dcgm_table(hall, str(f.node))
        assert table["DCGM_FI_DEV_XID_ERRORS"][str(f.gpu)] == f.xid
        assert all(v == 0 for g, v in table["DCGM_FI_DEV_XID_ERRORS"].items() if g != str(f.gpu))
    elif f.kind == "gpu_missing":
        table = dcgm_table(hall, str(f.node))
        assert str(f.gpu) not in table["DCGM_FI_DEV_GPU_TEMP"]
        assert len(table["DCGM_FI_DEV_GPU_TEMP"]) == hall.nodes[str(f.node)].gpu_count - 1
    elif f.kind == "nvlink_degraded":
        table = dcgm_table(hall, str(f.node))
        assert all(v > 100 for v in table["DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL"].values())
    elif f.kind == "node_health_warning":
        assert redfish_system(hall, str(f.node))["Status"]["Health"] == "Warning"
    elif f.kind == "coolant_leak":
        targets = [str(f.node)] if f.node else [n.name for n in hall.nodes_in_rack(str(f.rack))]
        for node in targets:
            assert set(redfish_leak_states(hall, node)) == {"Critical"}
            assert redfish_system(hall, node)["Status"]["Health"] == "OK"
    elif f.kind == "pdu_outlet_off":
        node = str(f.node)
        assert hall.reach(node, "redfish") == "refused" and hall.reach(node, "dcgm") == "refused"
        for c in hall.cables_of_node(node):
            rec = lldp[c.b.device][_port_no(c.b.interface)]
            assert rec.link_up is False and rec.remote_system is None
    elif f.kind == "lldp_port_id_mac":
        leaf = str(f.leaf)
        for rec in lldp[leaf].values():
            if rec.remote_system is not None:
                assert (
                    rec.remote_port_is_mac
                    and rec.remote_port == hall.nodes[rec.remote_system].macs[_iface_of(hall, rec)]
                )


def _iface_of(hall: Hall, rec: PortRecord) -> str:
    node = hall.nodes[str(rec.remote_system)]
    return next(i for i, m in node.macs.items() if m == rec.remote_port)


def touched(hall: Hall, faults: list[Fault]) -> tuple[set[str], set[tuple[str, int]], set[str]]:
    """(nodes whose Redfish/DCGM may change, leaf ports that may change, leaves whose id-type changes)."""
    nodes: set[str] = set()
    ports: set[tuple[str, int]] = set()
    leaves: set[str] = set()
    for f in faults:
        if f.kind == "coolant_leak" and f.rack:
            nodes.update(n.name for n in hall.nodes_in_rack(f.rack))
        elif f.kind in NODE_KINDS:
            nodes.add(str(f.node))
            if f.kind == "pdu_outlet_off":
                ports.update((c.b.device, _port_no(c.b.interface)) for c in hall.cables_of_node(str(f.node)))
        elif f.kind == "lldp_port_id_mac":
            leaves.add(str(f.leaf))
        else:
            labels = [f.cable] if f.cable else [f.cable_a, f.cable_b]
            for label in labels:
                c = hall.intent_cables[str(label)]
                nodes.add(c.a.device)
                ports.add((c.b.device, _port_no(c.b.interface)))
            if f.kind == "wrong_port":
                ports.add((str(f.to_device), _port_no(str(f.to_interface))))
    return nodes, ports, leaves


@settings(
    max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(spec=specs, data=st.data())
def test_faults_are_reflected_exactly_and_clear_restores_pristine(
    spec: HallSpec, data: st.DataObject
) -> None:
    hall = Hall.generate(spec)
    pristine = snapshot(hall)
    faults = draw_faults(data, hall)
    injected = [hall.inject(f) for f in faults]
    assert [f.id for f in hall.faults()] == [f.id for f in injected]
    event(f"faults={len(injected)}")
    event(f"nodes={len(hall.nodes)}")
    for f in injected:
        event(f"kind={f.kind}")

    lldp = {lf: normalize_lldp(hall, lf) for lf in hall.leaves}
    for f in injected:
        check_fault(hall, f, injected, lldp)

    # No collateral change anywhere else.
    nodes_touched, ports_touched, leaves_touched = touched(hall, injected)
    for node in hall.nodes:
        if node not in nodes_touched:
            assert hall.redfish_tree(node) == pristine["redfish"][node]
            assert hall.dcgm_metrics(node) == pristine["dcgm"][node]
    pristine_lldp = {lf: normalize_lldp(Hall.generate(spec), lf) for lf in hall.leaves}
    for leaf in hall.leaves:
        if leaf in leaves_touched:
            continue
        for number, rec in lldp[leaf].items():
            if (leaf, number) not in ports_touched:
                assert rec == pristine_lldp[leaf][number], (leaf, number)

    # Clear in a random order: everything comes back exactly.
    order = data.draw(st.permutations([f.id for f in injected]), label="clear order")
    for fid in order:
        hall.clear(str(fid))
    assert hall.faults() == []
    assert snapshot(hall) == pristine


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=specs, data=st.data())
def test_injecting_the_same_faults_twice_renders_identically(spec: HallSpec, data: st.DataObject) -> None:
    a = Hall.generate(spec)
    faults = draw_faults(data, a)
    b = Hall.generate(spec)
    for f in faults:
        stamped = f.model_copy(update={"created_at": a.inject(f).created_at})
        b.inject(stamped)
    assert snapshot(a) == snapshot(b)
    assert a.state()["cables"] == b.state()["cables"]
