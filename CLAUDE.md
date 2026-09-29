# Agent guide

CometAPI Connect is a local configuration tool maintained by CometAPI. Read `README.md`, `ARCHITECTURE.md`, and `CONTRIBUTING.md` for product behavior and development setup.

## Working rules

- Keep changes focused and preserve existing configuration and recovery behavior.
- Use Python 3.13 for development; maintain the declared source compatibility range in `pyproject.toml`.
- Keep detection read-only. Route configuration changes through the engine, adapters, and storage transactions.
- Preserve unrelated settings, reject stale previews, and guard restore against later edits. Native credentials need conflict and rollback checks too.
- Never log or commit real credentials, local profiles, backups, browser captures, or account evidence. Use isolated fixtures and dummy credentials in automated tests.
- Do not run paid requests or modify real application profiles as an implicit test step. Live acceptance must define its target profiles, credential destination, budget, and recovery procedure.
- Keep runtime endpoint and credential handling out of UI code. Preserve loopback binding and request authentication.
- Edit the canonical media transport in `integrations/sd-webui-cometapi/scripts/cometapi_media.py`, then regenerate assets with `python scripts/sync_media_assets.py`.
- Update every language catalog when user-facing strings change. Preserve third-party licenses and attribution when changing bundled assets.
- Write documentation in English. Keep this file and `CLAUDE.md` identical.

## Checks

Install the project with `python -m pip install -e '.[dev]'` in a virtual environment, then run:

```sh
ruff check cometapi_helper tests scripts integrations launch.py
ruff format --check cometapi_helper tests scripts integrations launch.py
python scripts/sync_media_assets.py --check
python -m pytest -q
node tests/i18n.test.cjs
```

Run relevant browser and packaged-resource checks for UI or distribution changes. Report the checks actually run and any platform limits. Fixture tests and successful builds must not be described as live acceptance of an upstream application.
