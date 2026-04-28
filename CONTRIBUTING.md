# Contributing

Thanks for taking the time to contribute! This is a small custom integration for Home Assistant; the goal is to keep the loop fast and the surface area small.

## Setup

```bash
# Python 3.13+ required (matches the HA version we test against)
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements_test.txt

# Optional but recommended: enable git hooks so lint/format runs on commit
pre-commit install
```

If your venv pre-dates a HA bump, refresh it:

```bash
pip install -r requirements_test.txt --upgrade
```

`pytest-homeassistant-custom-component` pulls a matched Home Assistant version, so you don't need to install HA separately.

## Run the test suite

```bash
pytest                     # full suite
pytest tests/              # phase-2 client tests only (fast, no HA)
pytest tests/integration/  # HA-fixture-dependent tests (config flow, sensors)
pytest --cov=custom_components/gazdebordeaux  # with coverage
```

CI runs the same lint and pytest commands on every push and PR (`.github/workflows/tests.yml`).

## Lint and format

We use [ruff](https://docs.astral.sh/ruff/) for both lint and format. Config lives in `pyproject.toml`.

```bash
ruff check .              # lint
ruff check . --fix        # auto-fix what's safe
ruff format .             # format
ruff format --check .     # CI-style check, no changes
```

`pre-commit install` wires both into the commit hook so this happens automatically.

## Manual smoke testing in Home Assistant

A standalone script is included for hitting the upstream API without needing a running HA:

```bash
GDB_USERNAME=you@example.com GDB_PASSWORD='...' python test_login.py
```

It enables DEBUG logging so request URLs, response status, content-type, and bodies show up — useful when the upstream API changes shape.

## Releasing

1. Bump `custom_components/gazdebordeaux/manifest.json` `version`.
2. Add a section to `CHANGELOG.md` (Keep a Changelog format).
3. Open a PR, get CI green, merge.
4. After merge, tag the commit: `git tag -a v1.1.X -m "..." && git push origin v1.1.X`. HACS picks up the release from the tag.

If a version is sitting on an open PR and you make further changes before merging, fold them into the same version's changelog rather than bumping again — only bump after the previous version actually shipped to `main`.

## Project notes

`CLAUDE.md` at the repo root has a longer write-up of API quirks, payload shapes, and historical pitfalls (multi-house accounts, the WAF browser-headers requirement, the voluptuous `Optional(default=...)` trap). Worth a read before touching `gazdebordeaux.py` or the config flow.
