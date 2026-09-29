import copy
import json
import shutil
import subprocess
import time

import pytest

from cometapi_helper import roo_store as roo
from cometapi_helper.adapters import Change
from cometapi_helper.common import Context, SetupError


def fixture():
    return {
        "schema": 1,
        "mode": "code",
        "secret": "previous-openai-secret",
        "profiles": roo.encode(
            {
                "currentApiConfigName": "Original",
                "apiConfigs": {
                    "Original": {
                        "id": "original-id",
                        "apiProvider": "openrouter",
                        "openRouterApiKey": "keep-secret",
                    }
                },
                "modeApiConfigs": {"code": "original-id", "ask": "original-id"},
                "migrations": {"openAiHeadersMigrated": True},
            }
        ).decode(),
        "values": {
            **{key: None for key in roo.FIELDS},
            "apiProvider": "openrouter",
            "currentApiConfigName": "Original",
            "openAiHeaders": {"Host": "old.example"},
            "openAiUseAzure": True,
            "listApiConfigMeta": [
                {"name": "Original", "id": "original-id", "apiProvider": "openrouter"}
            ],
        },
    }


def test_routing_preserves_other_profiles_modes_and_is_idempotent():
    before = fixture()
    original = copy.deepcopy(before)
    after = json.loads(roo.prepare_value(roo.encode(before), "sk-fixture-secret", "gpt-4.1-mini"))
    profiles = json.loads(after["profiles"])
    assert before == original
    assert (
        profiles["apiConfigs"]["Original"]
        == json.loads(before["profiles"])["apiConfigs"]["Original"]
    )
    assert profiles["modeApiConfigs"] == {"code": roo.ID, "ask": "original-id"}
    assert profiles["migrations"] == {"openAiHeadersMigrated": True}
    assert after["values"]["openAiHeaders"] == {}
    assert after["values"]["openAiUseAzure"] is False
    assert after["secret"] == "sk-fixture-secret"
    assert roo.prepare_value(roo.encode(after), "sk-fixture-secret", "gpt-4.1-mini") == roo.encode(
        after
    )


def test_collision_and_unknown_schema_fail_closed():
    before = fixture()
    profiles = json.loads(before["profiles"])
    profiles["apiConfigs"][roo.NAME] = {"id": "users-profile"}
    before["profiles"] = json.dumps(profiles)
    with pytest.raises(SetupError, match="user-created"):
        roo.prepare_value(roo.encode(before), "sk-fixture-secret", "gpt-4.1-mini")
    before = fixture()
    before["schema"] = 7
    with pytest.raises(SetupError, match="Unsupported"):
        roo.prepare_value(roo.encode(before), "sk-fixture-secret", "gpt-4.1-mini")


def test_resource_identity_and_platform_are_restricted(tmp_path):
    with pytest.raises(SetupError, match="macOS"):
        roo.find(Context(home=tmp_path, platform="win32", env={}, use_path=False))
    resource = {
        "kind": roo.KIND,
        "app": str(tmp_path),
        "profile": str(tmp_path),
        "extensions": str(tmp_path),
        "database": str(tmp_path / "wrong.db"),
    }
    with pytest.raises(SetupError, match="do not match"):
        roo.validate(resource)
    resource["arbitrary_command"] = "echo should-not-run"
    with pytest.raises(SetupError, match="Invalid Roo"):
        roo.validate(resource)


def test_unconfirmed_native_write_keeps_recovery_manifest(tmp_path, monkeypatch):
    from cometapi_helper import engine as module

    engine = module.Engine(Context(home=tmp_path, env={}, use_path=False))
    target = tmp_path / "state.vscdb"
    change = Change(
        "roo-code",
        "Roo Code",
        target,
        b"original-native-snapshot",
        b"new-native-snapshot",
        [],
        resource={"kind": roo.KIND, "database": str(target)},
    )
    engine.plans["fixture"] = (time.monotonic(), [change])
    monkeypatch.setattr(module, "read_target", lambda *args: b"original-native-snapshot")

    def interrupted(*args, **kwargs):
        raise roo.IncompleteNativeWrite("Native process stopped before an acknowledgment")

    monkeypatch.setattr(module, "write_target", interrupted)
    with pytest.raises(SetupError, match="could not be rolled back"):
        engine.apply("fixture")
    manifests = list((engine.ctx.state_dir / "backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    value = json.loads(manifests[0].read_text())
    assert value["status"] == "rollback_failed"
    assert (manifests[0].parent / "0.bak").read_bytes() == b"original-native-snapshot"


def test_native_secret_cache_rollback_and_concurrent_guard(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Native bridge unit fixture requires Node")
    before = fixture()
    after = json.loads(roo.prepare_value(roo.encode(before), "sk-fixture-secret", "gpt-4.1-mini"))
    data = tmp_path / "fixture.json"
    data.write_text(json.dumps({"before": before, "after": after}))
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const store=require(process.argv[2]);
const {before,after}=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
function host() {
 const globals=new Map(Object.entries(before.values).filter(([,v])=>v!==null));
 globals.set('mode',before.mode);globals.set('taskHistory',[{id:'preserve-user-task'}]);
 const secrets=new Map([['roo_cline_config_api_config',before.profiles],['openAiApiKey',before.secret],['unrelated','keep-me']]);
 const cache=new Map(secrets);let failOnce=false;
 const ctx={globalState:{get:k=>globals.get(k)},secrets:{get:async k=>secrets.get(k),store:async(k,v)=>secrets.set(k,v)}};
 const proxy={setValue:async(k,v)=>{if(failOnce){failOnce=false;throw Error('fixture');}v===undefined?globals.delete(k):globals.set(k,v);},
  storeSecret:async(k,v)=>{v===undefined?secrets.delete(k):secrets.set(k,v);v===undefined?cache.delete(k):cache.set(k,v);}};
 const api={context:ctx,sidebarProvider:{contextProxy:proxy,providerSettingsManager:{lock:cb=>cb(),secretsKey:'roo_cline_config_api_config'}},getCurrentTaskStack:()=>[]};
 return {api,globals,secrets,cache,fail:()=>{failOnce=true;}};
}
(async()=>{
 let h=host();await store.write(h.api,'3.54.0',after,before);
 assert.equal(h.cache.get('openAiApiKey'),after.secret);
 assert.equal(store.canonical(await store.snapshot(h.api,'3.54.0')),store.canonical(after));
 await store.write(h.api,'3.54.0',before,after);
 assert.equal(h.cache.get('openAiApiKey'),before.secret);
 assert.equal(h.secrets.get('roo_cline_config_api_config'),before.profiles);
 assert.equal(h.secrets.get('unrelated'),'keep-me');
 assert.deepEqual(h.globals.get('taskHistory'),[{id:'preserve-user-task'}]);
 h=host();h.globals.set('openAiModelId','later-user-edit');
 await assert.rejects(store.write(h.api,'3.54.0',after,before),/conflict/);
 assert.equal(h.globals.get('openAiModelId'),'later-user-edit');
 assert.equal(h.secrets.get('roo_cline_config_api_config'),before.profiles);
 h=host();h.fail();await assert.rejects(store.write(h.api,'3.54.0',after,before),/write-failed-restored/);
 assert.equal(store.canonical(await store.snapshot(h.api,'3.54.0')),store.canonical(before));
 h=host();h.api.getCurrentTaskStack=()=>['active'];
 await assert.rejects(store.write(h.api,'3.54.0',after,before),/active-task/);
 h=host();await store.write(h.api,'99.0.0',after,before);
 assert.equal(store.canonical(await store.snapshot(h.api,'99.0.0')),store.canonical(after));
 h=host();h.api.sidebarProvider.contextProxy.storeSecret=null;
 await assert.rejects(store.write(h.api,'99.0.0',after,before),/unsupported/);
 h=host();h.globals.delete('mode');
 assert.equal((await store.snapshot(h.api,'3.54.0')).mode,'architect');
 h=host();h.secrets.set('roo_cline_config_api_config',JSON.stringify(JSON.parse(before.profiles),null,2));
 assert.equal(store.canonical(await store.snapshot(h.api,'3.54.0')),store.canonical(before));
 console.log('native state, cache, rollback, concurrency and version checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    file = tmp_path / "native.cjs"
    file.write_text(script)
    completed = subprocess.run(
        [node, str(file), str(roo.ASSETS / "store.cjs"), str(data)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
