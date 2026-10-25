# Faults and actions

## Faults and what they look like

| Fault | Observable effect | Expected `turnup-check` finding |
|---|---|---|
| `wrong_port(cable, to_device, to_interface)` | neighbor appears on the target port, intended port down with no neighbor; node Redfish port `LinkUp` with `LLDPReceive.PortId` = target | `wrong_port` |
| `missing_link(cable)` | intended port down, no neighbor anywhere; node port `NoLink` | `missing_link` |
| `swap_pair(a, b)` | the two cables' leaf ports are exchanged | 2 x `wrong_port` |
| `link_degraded(cable, 200)` | neighbor correct, speed 200G on both ends | `link_degraded` |
| `link_degraded(cable, "down")` | link down, LLDP neighbor still present (aging entry) | `link_degraded` |
| `unreachable_node(node, mode, scope=all\|redfish\|dcgm)` | Redfish and/or DCGM answer per `mode`; fabric links stay up. Also accepted for a leaf (eAPI/NVUE) | `unreachable_node` (DCGM-only scope: `gpu: unknown`, no finding) |
| `partial_redfish(node)` | `FirmwareInventory` 404, everything else OK | none (firmware `unknown`) |
| `firmware_off_baseline(node, component, version)` | inventory member version changes (`BiosVersion`/`FirmwareVersion` follow) | `firmware_off_baseline` |
| `gpu_unhealthy(node, gpu, xid=79)` | `DCGM_FI_DEV_XID_ERRORS{gpu}` = xid, event log entry | `gpu_unhealthy` |
| `gpu_missing(node, gpu)` | the GPU disappears from `/metrics` | `gpu_count_mismatch` |
| `nvlink_degraded(node)` | `NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL` > 100 on every GPU, bandwidth quartered | `nvlink_degraded` |
| `node_health_warning(node)` | `Systems/<id>.Status.Health` = `Warning`, event log entry | `node_health_warning` |
| `coolant_leak(node \| rack)` | `LeakDetection` detectors `Critical`, chassis health Critical; `Systems` health unchanged | none (acceptance reads it) |
| `pdu_outlet_off(node)` | BMC and exporter refused, host off, all of the node's links down | `unreachable_node` + `missing_link` per port |
| `lldp_port_id_mac(leaf)` | that leaf reports NIC port ids as MACs (`id-type: mac` / `macAddress`) | none when MAC matching works |

Faults get ids `flt-0001`, `flt-0002`, ... Validation errors are 400 (unknown kind, missing fields),
unknown objects 404, occupied ports 409; the server never answers 500 for a bad request.

## Actions

`seat_cable(label, a_device?, a_interface?, b_device?, b_interface?)` (ends default to the intent),
`unseat_cable(label)`, `power_on(device)` (also restores a PDU outlet), `power_off(device)` (host off:
BMC still answers with `PowerState: Off`, exporter refused, links down), `replace_node(device,
new_serial)` (fresh healthy GPUs, baseline firmware, node faults retired, cables stay),
`update_firmware(device, component, version)`, `scan_qr(label)` (no state change; returns the label
record). Over HTTP: `POST /sim/actions {"kind": ..., ...}`.
