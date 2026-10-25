# Development and releasing

## Tests

```bash
make install  # pip install -e '.[dev]' into the active venv (else ./.venv)
make test     # pytest -q, about 20 s
make lint     # ruff check + ruff format --check
```

Unit tests cover generation and every emulator shape against the vendor-shaped examples kept under
`tests/fixtures/`; hypothesis property tests draw random specs (1-3 racks, 2-18 nodes, 4-16 ports,
1-4 leaves, both vendors, both LLDP id modes) and random fault sets, and check that the emulated LLDP,
Redfish and DCGM views reflect each fault exactly, that nothing else changes, and that clearing the
faults in random order restores the pristine views; an HTTP test starts the real server on a free
`127.0.0.1` port and fetches one resource of each protocol plus every fault mode; the technician bot
runs against an in-repo stub of the Acceptance routes it uses.

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check` and pytest on Python 3.12 and 3.13
for every push to `main` and every pull request, and checks that the sdist and wheel build.

## Releasing

`.github/workflows/release.yml` publishes to PyPI when a `v*` tag is pushed. It checks that the tag
matches `version` in `pyproject.toml` and `__version__` in `src/simhall/__init__.py`, builds the sdist
and wheel with `python -m build`, and uploads them with `pypa/gh-action-pypi-publish` through PyPI
trusted publishing (OIDC: no API token is stored anywhere). A tag for a version that is already on PyPI
(uploaded by hand with twine) publishes nothing and passes.

Before the first tag, the PyPI trusted publisher must be configured: on pypi.org, Account settings,
Publishing ("Add a new pending publisher" while the project does not exist yet; the project's Settings,
Publishing afterwards) with PyPI project name `simhall`, owner `turnup-dev`, repository name `simhall`,
workflow name `release.yml` and environment name `pypi`. GitHub creates the `pypi` environment on the
first run; add required reviewers to it under Settings, Environments if releases should wait for a click.
Tag only while the repository is public (GitHub Free offers environments on public repositories only).

```bash
# bump the version in pyproject.toml and src/simhall/__init__.py, commit, then:
git tag v0.1.1 && git push origin v0.1.1
```

## Layout

```
src/simhall/
  models.py        HallSpec, CableSpec, physical-state records
  naming.py        labels, port names, display forms, MAC scheme
  hall.py          Hall: generation, faults, actions, views, state
  faults.py        Fault model and constructors
  actions.py       Action model and constructors
  golden.py        hall-b
  specfile.py      spec YAML/JSON
  emulators/       redfish.py, lldp.py (eAPI + NVUE), dcgm.py, netbox.py
  server.py        FastAPI app, ServerThread for tests
  technician.py    the bot
  seed_netbox.py   pynetbox seeding
  views.py         normalizers used by tests and oracles
  cli.py           click commands
```
