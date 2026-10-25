# What simhall serves

`simhall serve` is one FastAPI server on `127.0.0.1` (port 8900 by default). In Python,
`create_app(hall)` serves that very `Hall` object (in-process actions are visible over HTTP) and
`ServerThread(app)` runs it on a free port for tests. Devices are distinguished
by path prefix:

| Path | Protocol | Shape |
|---|---|---|
| `/dev/<node>/redfish/v1/...` | DMTF Redfish (GET, any basic auth) | service root, `Systems` (follow `Members`), `Managers`, `UpdateService/FirmwareInventory`, `Chassis/<id>/NetworkAdapters/<id>/Ports/<ifname>` with `Ethernet.LLDPReceive`, `ThermalSubsystem/LeakDetection`, `ThermalSubsystem/ThermalMetrics`, `Systems/<id>/LogServices/EventLog/Entries` |
| `/dev/<node>/metrics` | DCGM-exporter Prometheus text | eleven `DCGM_FI_DEV_*` families, labels `gpu`, `UUID`, `modelName`, `Hostname`, `device`, `pci_bus_id` |
| `POST /dev/<leaf>/command-api` | Arista eAPI JSON-RPC (`runCmds`) | `show lldp neighbors detail`, `show interfaces status`, `show lldp neighbors`, `show version`, `show hostname` |
| `/dev/<leaf>/nvue_v1/interface?rev=applied` | NVIDIA NVUE REST | `{"swp21": {"type": "swp", "link": {...}, "lldp": {"neighbor": {...}}}}`; also `/nvue_v1/interface/<name>` and `/nvue_v1/system` |
| `/netbox/api/dcim/{sites,racks,devices,interfaces,cables}/` | NetBox REST (any token) | `{count, next, previous, results}`, filters `site=`, `device_id=`, `device=`, `name=`, `rack=`, `role=`, `limit=`, `offset=`; `/netbox/api/status/` for pynetbox |
| `/sim/...` | control plane | `GET /sim/intent`, `GET /sim/state`, `GET /sim/spec`, `GET/POST /sim/faults`, `DELETE /sim/faults/{id}`, `POST /sim/actions`, `POST /sim/reset`, `GET /sim/history`, `GET /sim/health` |

Redfish `@odata.id` values are device-relative (`/redfish/v1/...`), like a real BMC at `https://<ip>`.
A collector that joins `base_url + odata_id` works against both the simulator and real hardware.
Device mgmt URLs in the intent and in the NetBox custom fields (`turnup_bmc_url`, `turnup_dcgm_url`,
`turnup_switch_url`) always point at the host and port the server is actually listening on, so
`NETBOX_URL=http://127.0.0.1:8900/netbox` exercises the real NetBox adapter end to end. Node devices
also carry `config_context.turnup.firmware_baseline`.

Per-device fault modes: `timeout` sleeps `--timeout-sleep` seconds
(default 10, longer than the collector's 3 s), `http_500` answers 500, `refused` answers 404 at the
prefix ("device not registered"), `partial` answers 404 for `FirmwareInventory` only.
