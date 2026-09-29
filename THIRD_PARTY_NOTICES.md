# Third-party notices

The repository's [MIT License](LICENSE) applies to CometAPI-owned source code. Third-party components retain their original licenses and copyright notices. Product names and trademarks identify supported integrations; they do not imply endorsement by their owners.

## Python and build dependencies

Python runtime dependency versions are declared in [pyproject.toml](pyproject.toml) and [requirements.txt](requirements.txt). Build tools and versions are specified in the workflows and [release script](scripts/build_release.py). Standalone artifacts also include the Python runtime and their platform dependencies. The packager collects project, Python, installed production-dependency, and PyInstaller notices into an embedded `licenses/` directory. Its `manifest.json` records component versions and notice hashes, which the executable self-test verifies. Preserve applicable license and notice files when distributing or updating these components.

Native LevelDB and Snappy notices are preserved under `packaging/licenses/` with source URLs and checksums. These supplement notices omitted by the Python wheel and are embedded under `licenses/native/` during packaging. The recorded upstream source versions identify the notice text, not the native library versions in every platform's binary; the manifest leaves unestablished runtime versions unset.

## LM Studio OpenAI-compatible plugin

`cometapi_helper/assets/lmstudio-openai-compat-rev9.zip` contains the `lmstudio/openai-compat-endpoint` plugin, revision 9, and its Node.js dependencies.

- Upstream: [LM Studio Hub plugin](https://lmstudio.ai/lmstudio/openai-compat-endpoint).
- Provenance: [LM Studio's generator documentation](https://github.com/lmstudio-ai/docs/blob/9b8bc2004f04880a0ca7cbb19932ea8eb390c2d5/2_typescript/3_plugins/3_generator/index.md) links to this plugin.
- License declaration: upstream [package.json](https://lmstudio.ai/lmstudio/openai-compat-endpoint/files/package.json) declares **ISC**. Revision 9 does not include a standalone license file or an explicit copyright notice. The upstream metadata and attribution are preserved; no copyright owner is inferred from the publisher name.
- Source verification: package metadata and the original bundle's source-map contents for `src/config.ts`, `src/generator.ts`, and `src/index.ts` were compared with the corresponding upstream revision 9 files on September 29, 2026; they matched after normalizing line endings. The rebuilt archive retains those source files and omits the machine-specific source map.

The plugin retains its upstream ISC declaration and is not relicensed under CometAPI's MIT license. Bundled Node.js dependencies are:

| Package | Version | License |
| --- | --- | --- |
| `@lmstudio/sdk` | 1.4.0 | Apache-2.0 |
| `@lmstudio/lms-isomorphic` | 0.4.6 | Metadata: Apache-2.0; included license: MIT (see below) |
| `@types/node` | 20.14.8 | MIT |
| `ansi-styles` | 4.3.0 | MIT |
| `chalk` | 4.1.2 | MIT |
| `color-convert` | 2.0.1 | MIT |
| `color-name` | 1.1.4 | MIT |
| `has-flag` | 4.0.0 | MIT |
| `jsonschema` | 1.5.0 | MIT |
| `openai` | 5.2.0 | Apache-2.0, with separate vendored notices |
| `supports-color` | 7.2.0 | MIT |
| `undici-types` | 5.26.5 | MIT |
| `ws` | 8.21.0 | MIT |
| `zod` | 3.24.1 | MIT |
| `zod-to-json-schema` | 3.22.5 | ISC |

CometAPI's packaging changes update `ws` from 8.18.2 to 8.21.0 and its lockfile records, add the missing Undici notice, rebuild the plugin with esbuild 0.25.10 without machine-specific paths or a source map, remove installation-state metadata and a build log, and include a provenance manifest. The upstream plugin source and API behavior are preserved.

The resulting archive SHA-256 is `4f32964b3b137c2e056fa94d99d5b635ee13f3e62ec0c34c34df1f8a4359cda6`.

The `undici-types` package is version 5.26.5 and declares MIT. Its notice is reproduced from [nodejs/undici at v5.26.5](https://github.com/nodejs/undici/blob/v5.26.5/LICENSE), retaining **Copyright (c) Matteo Collina and Undici contributors**.

Upstream licensing metadata is retained as supplied. In particular, `@lmstudio/lms-isomorphic` 0.4.6 declares Apache-2.0 in its package metadata, while its included `LICENSE` contains MIT terms and **Copyright (c) 2025 Element Labs Inc**. Both are preserved; this project does not resolve that upstream discrepancy by rewriting either. The `openai` dependency also includes separate notices for vendored `qs` (BSD-3-Clause) and `zod-to-json-schema` (ISC).

## Rebuild and verify the plugin bundle

The offline [rebuild script](scripts/build_lmstudio_plugin.py) verifies seven source files, the complete `ws` file set, the supplementary Undici license, and the compiler version against [the provenance manifest](scripts/lmstudio-plugin-provenance.json). The manifest records the Hub revision, source hashes, dependency archive integrity, supplementary license sources, and build recipe. Other dependencies are preserved from the reviewed input ZIP; the script does not authenticate arbitrary replacement archives.

Provide an esbuild **0.25.10** executable explicitly:

```sh
python scripts/build_lmstudio_plugin.py --esbuild /path/to/esbuild --check
```

Omit `--check` to rebuild the repository bundle. `--source` and `--output` select alternate input and output ZIPs. Rebuilding from an older bundle can require `--ws-tarball /path/to/ws-8.21.0.tgz` and `--undici-license /path/to/LICENSE`; inputs must match the pinned provenance. The script does not download dependencies or install tools.

Keep upstream identifiers, dependency licenses, and attribution intact. Update the bundle's integrity digest and installation tests together, and review any changes to the provenance manifest. This document records component attribution; it is not an exhaustive software bill of materials.
