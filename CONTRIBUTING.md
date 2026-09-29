# Contributing to CometAPI Connect

Contributions that improve integration reliability, security, documentation, and accessibility are welcome. For a substantial feature or new adapter, open an issue describing the intended behavior and supported environment before implementation.

## Development environment

Use Python 3.13 for development and release builds. The declared source compatibility range is Python `>=3.9.2,<3.15`.

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m cometapi_helper
```

On Windows, use `py -3.13 -m venv .venv` and `.venv\Scripts\Activate.ps1` in PowerShell. Node.js 22 is used for JavaScript checks. Platform-specific tests may be skipped on other operating systems.

## Validation

Run these checks from the repository root:

```sh
ruff check cometapi_helper tests scripts integrations launch.py
ruff format --check cometapi_helper tests scripts integrations launch.py
python scripts/sync_media_assets.py --check
python -m pytest -q
node tests/i18n.test.cjs
```

Use `ruff format` with the same paths to format Python changes. Browser tests and locale requirements are documented in [docs/i18n.md](docs/i18n.md). Build checks are documented in [distribution/README.md](distribution/README.md).

Ordinary tests must use isolated fixtures and dummy credentials. Native credential-store tests are opt-in. Do not run tests against personal application profiles or make paid requests as part of CI. Any live acceptance run needs an explicit test profile, credential destination, request budget, and recovery procedure.

## Integration changes

- Keep detection read-only and report unsupported layouts clearly.
- Preserve unrelated application settings and validate configuration structure and required native APIs.
- Test malformed input, repeat application, preview/apply conflicts, rollback, and restoration after an external change.
- Test refusal to write credentials into Git-tracked files, missing Git in project configurations, and persistent backup-directory exclusion in a Git-managed home. A tracked LiteLLM YAML needs a private configuration; do not assume automatic environment-file loading.
- Keep endpoint and credential handling in adapters and storage modules, outside the UI.
- Describe verified behavior precisely. Fixture coverage and successful builds do not establish live application compatibility.

Media transport is maintained in `integrations/sd-webui-cometapi/scripts/cometapi_media.py`. After editing it, run `python scripts/sync_media_assets.py` and include both generated assets in the change. Preserve third-party attribution and license files when updating bundled components. The LM Studio plugin has an offline, verified [rebuild procedure](THIRD_PARTY_NOTICES.md#rebuild-and-verify-the-plugin-bundle); do not edit its ZIP by hand.

User-facing strings require matching updates to all language catalogs. Keep documentation in English, and keep `AGENTS.md` and `CLAUDE.md` synchronized.

## Pull requests

Keep changes focused. Explain the problem, resulting behavior, checks performed, and any remaining limitations. Include a regression test when changing configuration, credential, or recovery behavior. Never commit API keys, local profiles, backup contents, browser captures, account evidence, or build outputs.

Linux CI runs isolated tests on Python 3.9, 3.12, 3.13, and 3.14. After these checks pass, it builds and verifies a Linux x64 executable on Python 3.13. Windows and macOS workflows test and build pull requests on Python 3.13; JavaScript checks use Node.js 22. CI artifacts are development downloads. Publishing a release requires a separate maintainer review and the signing checks in the release guide.

For security-sensitive findings, use the private reporting channel in [SECURITY.md](SECURITY.md).
