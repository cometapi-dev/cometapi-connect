# Windows testing

## Download a development build

1. Sign in to GitHub with repository access and open [Windows test download](https://github.com/cometapi-dev/cometapi-connect/actions/workflows/windows-test.yml).
2. Select a successful run for the commit you want to test.
3. Download the `cometapi-connect-windows-x64` artifact and extract its ZIP.
4. Inspect the checksum and build metadata, then open the included EXE. Python is bundled, and the local interface opens in your default browser.
5. Enter your CometAPI key, select detected applications, and review changes. Close selected applications before applying changes and reopen them afterward.
6. Verify an ordinary task in each configured application. Use **Change history** to restore previous settings when finished.

Development builds are unsigned; Windows or an organization policy may block them. The workflow verifies unit tests, browser localization, bundled resources, and isolated configuration/recovery checks. It does not establish compatibility with every installed application or a complete Windows desktop acceptance result.

## Build from source

Install Python 3.13 x64, Git, and Node.js 22. In PowerShell:

```powershell
git clone https://github.com/cometapi-dev/cometapi-connect.git
cd cometapi-connect
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install "pyinstaller==6.22.2"
.\.venv\Scripts\python.exe -m pytest -q
node tests/i18n.test.cjs
.\.venv\Scripts\python.exe scripts/build_release.py
```

Outputs are written to `dist/releases/`. For browser checks using installed Edge, follow [the language test guide](i18n.md). Production signing requirements are documented in [release packaging](../distribution/README.md).

## Acceptance checklist

Use a dedicated Windows account or isolated application profiles. Record the CometAPI Connect commit, Windows version, architecture, application version, and configuration layout for each test.

- Launch the packaged executable and confirm the interface opens locally.
- Check application detection and the requirements shown for automatic setup.
- Preview and apply a change, preserving unrelated settings and selected workflows.
- Reapply the same configuration and check idempotence.
- Restore the original configuration and confirm that a subsequent external edit is protected from overwrite.
- Exercise native credential storage where the adapter requires it.
- Run a real application task only with an explicitly chosen account and request budget.

Windows Credential Manager, VS Code profiles, portable installations, and application process checks require native verification. A fixture or named-pipe test alone does not establish compatibility with an application's full runtime. Do not include credentials, personal profiles, or raw account evidence in test reports.
