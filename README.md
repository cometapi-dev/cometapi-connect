# CometAPI Connect

[![License: MIT](https://img.shields.io/badge/License-MIT-2ea44f.svg)](LICENSE) [![Python: 3.9–3.14](https://img.shields.io/badge/Python-3.9%E2%80%933.14-3776AB.svg)](pyproject.toml) [![Release: v0.1.3](https://img.shields.io/badge/Release-v0.1.3-1f883d.svg)](https://github.com/cometapi-dev/cometapi-connect/releases/tag/v0.1.3)

Connect your AI tools to [CometAPI](https://www.cometapi.com/) from one local interface. CometAPI Connect detects installed applications, previews configuration changes, and saves the settings needed to use your CometAPI account.

Maintained by CometAPI. Integration availability depends on your operating system, installed application, and configuration. See the [support matrix](cometapi_helper/catalog.json) for the integrations and limitations described by the application catalog.

## What it does

- Detects supported AI applications and project configurations.
- Configures endpoints, API credentials, and model preferences through app-specific adapters.
- Shows proposed changes before applying them and preserves unrelated settings.
- Keeps local backups and provides guarded restoration through **Change history**.
- Includes setup guidance for integrations that require manual steps and an interface in 16 languages.

CometAPI Connect configures applications that are already installed. The in-app catalog describes each integration's requirements and limitations.

## Get started

### Download CometAPI Connect v0.1.3

Download the latest stable release from [GitHub Releases](https://github.com/cometapi-dev/cometapi-connect/releases/tag/v0.1.3). The release is private while CometAPI completes its publication review; you need repository access to download it.

| Platform | Download | Verification files |
| --- | --- | --- |
| Windows x64 | [EXE](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-windows-x64-unsigned.exe) | [`windows-x64.json`](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/windows-x64.json), [SHA-256](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-windows-x64-unsigned.exe.sha256) |
| macOS Apple Silicon or Intel | [Universal2 DMG](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-macos-universal2-unsigned.dmg) | [`macos-universal2.json`](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/macos-universal2.json), [SHA-256](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-macos-universal2-unsigned.dmg.sha256) |
| Linux x64 | [Tarball](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-linux-x64.tar.gz) | [`linux-x64.json`](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/linux-x64.json), [SHA-256](https://github.com/cometapi-dev/cometapi-connect/releases/download/v0.1.3/CometAPI-Connect-0.1.3-linux-x64.tar.gz.sha256) |

These are unsigned development builds; the macOS build is not notarized. Verify the SHA-256 sidecar and inspect the JSON metadata before running a download. Standalone builds bundle Python and their runtime dependencies. Native Windows ARM, 32-bit systems, and musl/Alpine Linux are outside the current build matrix.

1. Extract the Linux tarball, open the Windows EXE, or open the macOS DMG and copy CometAPI Connect to Applications.
2. Open the app. Its local interface starts in your default browser.
3. Enter your [CometAPI API key](https://www.cometapi.com/console/token), select the applications you want to connect, and close those applications.
4. Choose **Review changes**, inspect the proposed settings, then choose **Connect selected apps**.
5. Reopen the configured applications and verify a task. Use **Change history** to restore previous settings when needed.

Restore refuses to overwrite settings changed after configuration. Existing project overrides, selected assistants, or operating-system credential prompts may require additional steps shown by the adapter.

### Development builds

The [GitHub Actions workflows](https://github.com/cometapi-dev/cometapi-connect/actions) also publish short-lived artifacts for pull requests and maintainer testing. These artifacts expire and are not a substitute for the versioned release above.

### Run from source

Use Python 3.13 for development. The declared source compatibility range is Python `>=3.9.2,<3.15`.

```sh
git clone https://github.com/cometapi-dev/cometapi-connect.git
cd cometapi-connect
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m cometapi_helper
```

On Windows, create the environment with `py -3.13 -m venv .venv` and activate it with `.venv\Scripts\Activate.ps1` in PowerShell. See [Windows testing](docs/windows-testing.md) for build and validation steps.

The optional command line includes read-only discovery and local recovery commands:

```sh
cometapi-connect scan
cometapi-connect --help
```

## Privacy, credentials, and recovery

The interface listens on `127.0.0.1` at a random port. A per-launch session token, host/origin checks, and a custom request header protect configuration operations. API keys are not placed in URLs or browser storage.

Selected applications receive your key in their configuration files or supported credential stores. Connect refuses to write credentials to Git-tracked files; configuration inside a Git project requires an available Git executable for these checks. Backups are stored under `~/.cometapi-helper/backups/` and may contain previous credentials; treat them as sensitive. If your home directory is Git-managed, Connect retains an ignore rule for its backup directory after application settings are restored. **Quit** stops the local server, and idle sessions expire after 30 minutes.

Model discovery fetches the public catalog at `https://api.cometapi.com/api/models` without sending your key. It does not verify account access. Setup itself makes no paid generation requests. Tasks you run in configured applications use your account and may incur charges.

OpenAI-compatible integrations generally use `https://api.cometapi.com/v1`; Claude Code uses `https://api.cometapi.com`. Supported models and protocols vary by application. Review each adapter's notes before connecting.

## Development

```sh
ruff check cometapi_helper tests scripts integrations launch.py
ruff format --check cometapi_helper tests scripts integrations launch.py
python scripts/sync_media_assets.py --check
python -m pytest -q
node tests/i18n.test.cjs
```

Automated tests use isolated fixtures and dummy credentials. Passing fixture tests or building an executable does not establish live compatibility with every supported application. Platform-specific checks run on their corresponding operating systems.

Read the [contribution guide](CONTRIBUTING.md), [architecture](ARCHITECTURE.md), [language guide](docs/i18n.md), [release notes](CHANGELOG.md), [community code of conduct](CODE_OF_CONDUCT.md), and [release packaging guide](distribution/README.md) for details.

## Support and security

Report reproducible bugs and feature requests in [GitHub Issues](https://github.com/cometapi-dev/cometapi-connect/issues). Contact [support@cometapi.com](mailto:support@cometapi.com) for account support or private vulnerability reports; follow [SECURITY.md](SECURITY.md). Never include credentials or private application profiles in an issue.

## License

CometAPI-owned source code is available under the [MIT License](LICENSE). Bundled third-party components retain their own licenses; see [Third-party notices](THIRD_PARTY_NOTICES.md). Application and provider names identify integrations and do not imply endorsement by their owners.
