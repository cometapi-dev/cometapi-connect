# Release packaging

CometAPI Connect packages its Python runtime and dependencies into standalone downloads. Windows users open an EXE, macOS users install an app from a DMG, and Linux users extract an executable archive. The interface runs locally in the user's browser.

## Build workflows

| Workflow | Outputs | Purpose |
| --- | --- | --- |
| [Code quality and Linux build](../.github/workflows/quality.yml) | Linux x64 executable archive | Development artifact with bundled-resource and isolated configuration checks |
| [Windows test download](../.github/workflows/windows-test.yml) | Windows x64 EXE | Unsigned development artifact |
| [macOS test downloads](../.github/workflows/macos-test.yml) | Separate Apple Silicon and Intel DMGs | Unsigned, unnotarized development artifacts |
| [Build standalone downloads](../.github/workflows/release.yml) | Windows x64, macOS universal2, Linux x64 | Manual build with optional required signing |

The test workflows run on pushes to `main`, pull requests, and manual dispatch. The standalone workflow runs manually. Workflows upload CI artifacts; they do not create GitHub Releases or deploy website downloads. For release candidates, set `require_signing` to require Windows signing and macOS signing/notarization.

Build on the target operating system: PyInstaller does not cross-compile operating systems. Use Python 3.13 and a dedicated virtual environment:

```sh
python -m pip install -r requirements.txt "pyinstaller==6.22.2"
python -m pip install --no-deps -e .
python scripts/build_release.py
```

For a Mac development build using the current interpreter architecture:

```sh
python scripts/build_release.py --mac-arch native
```

The default macOS release build requires a python.org universal2 interpreter. The manual workflow validates both executable architectures. Do not combine PyInstaller onefile executables with `lipo`; their embedded archives are not compatible with that approach. See [PyInstaller macOS architecture support](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support).

macOS uses a directory-style application bundle inside a DMG. Windows and Linux use onefile executables. Linux archives retain executable permissions, but desktop policies may still require a user to mark the file executable. Native Windows ARM, 32-bit systems, and musl/Alpine Linux are outside the current build matrix.

For Linux container builds, `scripts/build_linux_container.sh` runs inside an Ubuntu 22.04 container with the repository already mounted read-only at `/source` and a writable output directory at `/out`. It installs Ubuntu's Python runtime and the required build tools. From a POSIX shell at the repository root:

```sh
mkdir -p dist/releases
docker run --rm --platform linux/amd64 \
  --mount "type=bind,source=$PWD,target=/source,readonly" \
  --mount "type=bind,source=$PWD/dist/releases,target=/out" \
  ubuntu:22.04 sh /source/scripts/build_linux_container.sh
```

Set Docker's `--platform` to `linux/arm64` for an ARM64 build; the script itself takes no platform argument. The current CI matrix covers x64. Use a source checkout without real application profiles or credentials when mounting it into a build container.

## Artifact verification

Outputs in `dist/releases/` include the artifact, a SHA-256 sidecar, and build metadata with the filename, signing status, build OS, Python version, and bundled-resource result.

The packager embeds project notices, the Python runtime license, installed production-dependency notices, and PyInstaller notices under `licenses/`, with component versions and file hashes in `licenses/manifest.json`. Missing or empty license texts fail the build. The executable self-test verifies each recorded notice against its SHA-256 digest, alongside catalogs, HTML, certificate data, and parser libraries. Supplementary LevelDB and Snappy notices are verified against `packaging/licenses/sources.json` and included under `licenses/native/`; notice-source versions do not establish the library version in a platform binary. Build metadata reports the number of collected license components. Node.js dependency notices remain inside the bundled LM Studio plugin archive; this inventory is not a complete system SBOM. `scripts/smoke_frozen.py` exercises preview, configuration, repeat setup, and exact restoration using temporary fixtures and a dummy key. These checks do not establish live compatibility with every application or desktop environment.

Before distributing a release:

1. Run the automated checks and inspect each target platform's results.
2. Verify checksums and signing status against the downloaded artifacts.
3. Test clean installations, browser launch, and the intended integration/recovery scenarios on supported operating systems and architectures.
4. Preserve third-party license notices and include accurate release notes and known limitations.
5. Publish approved artifacts to an actual release location and verify the resulting downloads.

## Signing and notarization

Unsigned Windows and unnotarized macOS builds are development artifacts. Use the following repository secrets for the manual signing workflow:

| Secret | Purpose |
| --- | --- |
| `WINDOWS_CERTIFICATE_PFX_BASE64` | Code-signing certificate and private key in an exportable PFX |
| `WINDOWS_CERTIFICATE_PASSWORD` | PFX password |
| `MACOS_CERTIFICATE_P12_BASE64` | Developer ID certificate and private key |
| `MACOS_CERTIFICATE_PASSWORD` | P12 password |
| `MACOS_SIGNING_IDENTITY` | Developer ID Application signing identity |
| `MACOS_APPLE_ID` | Apple account used by notarization |
| `MACOS_TEAM_ID` | Apple Developer team identifier |
| `MACOS_APP_PASSWORD` | App-specific notarization password |

`WINDOWS_TIMESTAMP_URL` is an optional repository variable. `SIGNTOOL_PATH` can select a local Windows SDK SignTool. When using hardware or cloud signing, adapt the signing integration to that service instead of exporting a nonexportable key.

The macOS flow signs the app with hardened runtime, verifies the signature, signs the disk image, submits it for notarization, and staples/validates the ticket. Checksums are calculated after signing and stapling. Temporary signing material must be removed after the build. Signing does not guarantee immediate SmartScreen reputation or acceptance by managed-device policies.

## Optional website download routing

[`download-worker.js`](download-worker.js) can expose stable Windows, macOS, and Linux download routes. It is an optional deployment component and is not part of the local desktop app.

Populate its `RELEASE_MANIFEST` environment binding only after uploading and verifying approved artifacts. The manifest contains a `downloads` object with `windows-x64`, `macos-universal2`, and `linux-x64` entries. Each entry requires the actual HTTPS `url`, 64-character `sha256`, `signing`, and `verified: true`.

Windows requires `signing: "signed"`; macOS requires `signing: "notarized"`. Linux still requires an explicit verification flag and checksum. Missing or invalid entries return an unavailable response. These checks validate operator configuration; they do not independently verify a remote artifact's bytes or signature.

Prefer explicit operating-system buttons. Automatic browser detection cannot reliably identify every architecture. Use immutable, versioned artifact URLs, verify the public download after deployment, and retain the previous manifest and artifacts for rollback.
