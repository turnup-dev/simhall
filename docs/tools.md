# Technician bot and NetBox seeding

## Technician bot

```bash
simhall technician --acceptance-url http://127.0.0.1:8800 --sim-url http://127.0.0.1:8900 \
                   --worker tech-1 --error-rate 0.05 --speed 20 --seed 1
```

The bot pulls `GET /api/workers/{worker}/next?rack=`, posts `start`, performs the step as an action on
the hall, posts evidence (a QR scan of the cable label for cable steps, a note otherwise) and posts
`done`. With probability `--error-rate` a cable step goes wrong: onto an adjacent free port (chosen so
the verifier's fix reads "move cable one port up/down") or left unseated. `--speed 20` makes a nominal
60 s step take 3 s. It exits after `--idle-polls` consecutive empty polls (HTTP 204) or `--max-steps`.
The cable label is taken from any `cable`/`label` field on the step, else from a `CBL-rr-nn-pp` token in
the title or instructions, else from "node N port P" in the title plus the hall's intent.
`--approver NAME` approves `requires_approval` steps as that named person before starting them.

## Seeding a real NetBox

```bash
simhall seed-netbox --netbox-url http://127.0.0.1:8000 --token $NETBOX_TOKEN --golden hall-b
simhall seed-netbox --netbox-url http://127.0.0.1:8000 --golden hall-b --dry-run   # plan only, no pynetbox
```

Creates (idempotently, by natural key) the three `turnup_*` custom fields, the site, racks,
manufacturers, roles, device types, devices (with custom fields and `local_context_data.turnup`),
interfaces (MACs as `dcim.mac_addresses` objects on NetBox >= 4.2, inline before), and cables with
`a_terminations`/`b_terminations`. It has been exercised against a pynetbox stand-in in the tests, not
against a live NetBox yet.
