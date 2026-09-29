"""Validate the shipped plugin, including the dependency actually loaded by Node."""

import hashlib
import json
import re
import shutil
import subprocess
import zipfile

import pytest

from cometapi_helper import lmstudio_store as lm


def test_plugin_sources_dependency_patch_and_licenses_are_complete():
    with zipfile.ZipFile(lm.bundled_plugin()) as archive:
        provenance = json.loads(archive.read("COMETAPI-PROVENANCE.json"))
        manifest = json.loads(archive.read("manifest.json"))
        assert (manifest["owner"], manifest["name"], manifest["revision"]) == (
            "lmstudio",
            "openai-compat-endpoint",
            9,
        )
        for name, expected in provenance["source_sha256"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        ws = json.loads(archive.read("node_modules/ws/package.json"))
        assert ws["version"] == "8.21.0"
        for name, expected in provenance["ws"]["files_sha256"].items():
            assert hashlib.sha256(archive.read("node_modules/ws/" + name)).hexdigest() == expected
        for name in ("package-lock.json", "node_modules/.package-lock.json"):
            locked = json.loads(archive.read(name))["packages"]["node_modules/ws"]
            assert locked["version"] == ws["version"]
            assert locked["integrity"] == provenance["ws"]["integrity"]
        assert (
            hashlib.sha256(archive.read("node_modules/undici-types/LICENSE")).hexdigest()
            == (provenance["undici_license"]["sha256"])
        )
        assert b"Matteo Collina and Undici contributors" in archive.read(
            "node_modules/undici-types/LICENSE"
        )
        for name in archive.namelist():
            if re.fullmatch(r"node_modules/(?:@[^/]+/)?[^/]+/package.json", name):
                directory = name.removesuffix("package.json")
                assert any(
                    filename.casefold()
                    in {directory.casefold() + "license", directory.casefold() + "license.md"}
                    for filename in archive.namelist()
                ), f"Missing license for {name}"
        assert "install-state.json" not in archive.namelist()
        assert not any("/.turbo/" in name for name in archive.namelist())
        production = archive.read(".lmstudio/production.js")
        assert b"/Users/" not in production
        assert b"sourceMappingURL=" not in production


def test_plugin_bootstrap_loads_patched_runtime_and_registers_generator(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the bundled plugin")
    with zipfile.ZipFile(lm.bundled_plugin()) as archive:
        archive.extractall(tmp_path)
    production = tmp_path / ".lmstudio/production.js"
    subprocess.run([node, "--check", str(production)], check=True, capture_output=True, timeout=30)
    # Real dependency imports, with only the LM Studio process connection mocked.
    program = r"""
const { createRequire } = require('node:module');
const { readFileSync } = require('node:fs');
const { runInNewContext } = require('node:vm');
const { resolve } = require('node:path');
const root = process.argv[1];
const fromPlugin = createRequire(resolve(root, 'package.json'));
const sdk = fromPlugin('@lmstudio/sdk');
const fromTransport = createRequire(fromPlugin.resolve('@lmstudio/lms-isomorphic'));
if (fromTransport('ws/package.json').version !== '8.21.0') throw Error('Unpatched ws loaded');
if (typeof fromTransport('ws').WebSocket !== 'function') throw Error('ws failed to load');
const calls = [];
const host = {};
for (const name of ['setConfigSchematics', 'setGlobalConfigSchematics', 'setGenerator', 'initCompleted']) {
    host[name] = value => calls.push([name, value]);
}
const mocked = {
    ...sdk,
    LMStudioClient: class { constructor() { this.plugins = { getSelfRegistrationHost: () => host }; } }
};
runInNewContext(readFileSync(resolve(root, '.lmstudio/production.js'), 'utf8'), {
    require: name => name === '@lmstudio/sdk' ? mocked : fromPlugin(name),
    process: { env: {} },
    console: { error: (...args) => { throw Error(args.join(' ')); } }
});
setImmediate(() => {
    const expected = 'setConfigSchematics,setGlobalConfigSchematics,setGenerator,initCompleted';
    if (calls.map(call => call[0]).join(',') !== expected) throw Error('Incomplete registration');
    if (typeof calls[2][1] !== 'function') throw Error('Missing generator');
});
"""
    subprocess.run(
        [node, "-e", program, str(tmp_path)], check=True, capture_output=True, text=True, timeout=30
    )
