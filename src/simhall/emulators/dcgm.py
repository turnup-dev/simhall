# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""DCGM-exporter Prometheus exposition text (public metric names, CONTRACT.md section 3.3)."""

from __future__ import annotations

from simhall.models import HallSpec, NodeIntent, NodeState, gpu_model_name

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DRIVER_VERSION = "580.82.07"

# (metric family, type, help) in the order dcgm-exporter prints its default set.
FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock frequency (in MHz)."),
    ("DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (in C)."),
    ("DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (in W)."),
    ("DCGM_FI_DEV_FB_TOTAL", "gauge", "Framebuffer memory total (in MiB)."),
    ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "counter", "Total number of PCIe retries."),
    ("DCGM_FI_DEV_XID_ERRORS", "gauge", "Value of the last XID error encountered."),
    ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "counter", "Total number of double-bit volatile ECC errors."),
    ("DCGM_FI_DEV_ROW_REMAP_FAILURE", "gauge", "Whether remapping of rows has failed."),
    (
        "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS",
        "counter",
        "Number of remapped rows for uncorrectable errors.",
    ),
    (
        "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
        "counter",
        "Total number of NVLink flow-control CRC errors.",
    ),
    (
        "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
        "counter",
        "Total number of NVLink bandwidth counters for all lanes.",
    ),
)

NVLINK_DEGRADED_CRC_ERRORS = 1500


def _labels(nd: NodeIntent, model_name: str, g: int, gpu_uuid: str) -> str:
    bus = f"00000000:{0x0F + 0x10 * g:02X}:00.0"
    return (
        f'gpu="{g}",UUID="{gpu_uuid}",pci_bus_id="{bus}",device="nvidia{g}",'
        f'modelName="{model_name}",Hostname="{nd.name}",DCGM_FI_DRIVER_VERSION="{DRIVER_VERSION}"'
    )


def render(spec: HallSpec, nd: NodeIntent, ns: NodeState) -> str:
    """The `/metrics` body: one sample per present GPU per family."""
    model_name = gpu_model_name(spec.node_model)
    present = [g for g in ns.gpus if g.present]
    lines: list[str] = []
    for family, kind, help_text in FAMILIES:
        lines.append(f"# HELP {family} {help_text}")
        lines.append(f"# TYPE {family} {kind}")
        for g in present:
            labels = _labels(nd, model_name, g.index, g.uuid)
            value: int | float
            if family == "DCGM_FI_DEV_SM_CLOCK":
                value = g.sm_clock_mhz
            elif family == "DCGM_FI_DEV_GPU_TEMP":
                value = g.temp_c
            elif family == "DCGM_FI_DEV_POWER_USAGE":
                value = g.power_w
            elif family == "DCGM_FI_DEV_FB_TOTAL":
                value = g.fb_total_mib
            elif family == "DCGM_FI_DEV_PCIE_REPLAY_COUNTER":
                value = g.pcie_replay
            elif family == "DCGM_FI_DEV_XID_ERRORS":
                value = g.xid_errors
            elif family == "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL":
                value = g.ecc_dbe
            elif family == "DCGM_FI_DEV_ROW_REMAP_FAILURE":
                value = g.row_remap_failure
            elif family == "DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS":
                value = g.uncorrectable_remapped_rows
            elif family == "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL":
                value = (
                    (NVLINK_DEGRADED_CRC_ERRORS + 37 * g.index) if ns.nvlink_degraded else g.nvlink_crc_errors
                )
            else:  # DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL
                value = g.nvlink_bandwidth // 4 if ns.nvlink_degraded else g.nvlink_bandwidth
            lines.append(f"{family}{{{labels}}} {value}")
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, dict[str, float]]:
    """Tiny exposition parser for tests: family -> {gpu label -> value}."""
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, rest = line.split("{", 1)
        labels, value = rest.rsplit("} ", 1)
        gpu = labels.split('gpu="', 1)[1].split('"', 1)[0]
        out.setdefault(name, {})[gpu] = float(value)
    return out
