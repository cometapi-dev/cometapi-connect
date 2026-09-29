import ast
import json
import sys
import types

import pytest

from cometapi_helper import python_projects as projects
from cometapi_helper.common import Context, SetupError
from cometapi_helper.engine import Engine

KEY = "sk-python-project-fixture"


@pytest.mark.parametrize(
    "identity,module,constructor",
    [
        ("openai-sdk", "openai", "OpenAI"),
        ("openai-sdk", "openai", "AsyncOpenAI"),
        ("langchain", "langchain_openai", "ChatOpenAI"),
        ("semantica", "semantica.llms", "OpenAI"),
    ],
)
def test_constructor_reads_installer_file_and_restore(
    tmp_path, monkeypatch, identity, module, constructor
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "requirements.txt").write_text(module.split(".")[0] + "\n")
    path = root / "main.py"
    source = (
        '"""Existing module description — preserved."""\nfrom __future__ import annotations\n'
        "from " + module + " import " + constructor + " as Provider\n"
        "# Keep this comment and unrelated parameter.\n"
        'client = Provider(api_key="old-value", base_url="https://old.invalid/v1", timeout=17)\n'
    ).encode()
    path.write_bytes(source)
    path.chmod(0o755)
    (root / ".gitignore").write_text("# Existing patterns\n*.log\n")
    engine = Engine(Context(home=tmp_path, env={}, use_path=False, roots=[root]))
    preview = engine.preview(KEY, [identity], {"chat_model": "test-model"})
    assert KEY not in json.dumps(preview)
    tx = engine.apply(preview["plan_id"])
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o755
    assert path.read_bytes() != source and b"Keep this comment" in path.read_bytes()
    assert KEY.encode() not in path.read_bytes()
    fake = types.ModuleType(module)
    setattr(fake, constructor, lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, module, fake)
    values = {"__file__": str(path), "__name__": "fixture_program"}
    exec(compile(path.read_bytes(), str(path), "exec"), values)
    assert values["client"]["api_key"] == KEY
    assert values["client"]["base_url"] == "https://api.cometapi.com/v1"
    assert values["client"]["timeout"] == 17
    assert ("model" in values["client"]) == (identity != "openai-sdk")
    if identity != "openai-sdk":
        assert values["client"]["model"] == "test-model"
    assert all(
        x["action"] == "unchanged"
        for x in engine.preview(KEY, [identity], {"chat_model": "test-model"})["changes"]
    )
    engine.restore(tx["transaction_id"])
    assert path.read_bytes() == source
    assert not (root / projects.CONFIG).exists()
    assert (root / ".gitignore").read_text() == "# Existing patterns\n*.log\n"


@pytest.mark.parametrize(
    "source",
    [
        "from openai import OpenAI\nclient=OpenAI(**settings)\n",
        'from openai import OpenAI\nclient=OpenAI("key")\n',
        "from openai import OpenAI\nclient=OpenAI(http_client=transport)\n",
        'from openai import OpenAI\nclient=OpenAI(default_headers={"Authorization":"old"})\n',
        "from openai import OpenAI\ndef build(OpenAI): return OpenAI()\n",
        "from openai import OpenAI\nOpenAI=factory\nclient=OpenAI()\n",
        "from openai import OpenAI\nclient=OpenAI(api_key=fetch_secret())\n",
        "from openai import OpenAI\nfrom another_provider import OpenAI\nclient=OpenAI()\n",
        "import openai as sdk\nimport another_provider as sdk\nclient=sdk.OpenAI()\n",
        "from openai import OpenAI\nfrom another_provider import *\nclient=OpenAI()\n",
        "import openai\nopenai.OpenAI=factory\nclient=openai.OpenAI()\n",
    ],
)
def test_ambiguous_or_custom_constructors_refused(source):
    with pytest.raises(SetupError):
        projects.transform(source.encode(), "openai-sdk")


def test_module_alias_comments_and_existing_reader_protection():
    source = b"#!/usr/bin/env python3\n# Existing header\nimport openai as sdk\nclient=sdk.OpenAI(\n # User timeout\n timeout=42,\n)\n"
    result, count = projects.transform(source, "openai-sdk")
    assert count == 1 and result.startswith(b"#!/usr/bin/env python3\n# Existing header")
    assert b"# User timeout" in result
    ast.parse(result)
    assert projects.transform(result, "openai-sdk")[0] == result
    edited = result.replace(b"_info.st_size > 65536", b"_info.st_size > 99999")
    with pytest.raises(SetupError, match="edited"):
        projects.transform(edited, "openai-sdk")


def test_later_source_edit_stops_restore(tmp_path):
    (tmp_path / "requirements.txt").write_text("langchain-openai\n")
    path = tmp_path / "app.py"
    path.write_text(
        'from langchain_openai import ChatOpenAI\nmodel=ChatOpenAI(model_name="original", openai_api_key="old")\n'
    )
    engine = Engine(Context(home=tmp_path, env={}, use_path=False, roots=[tmp_path]))
    tx = engine.apply(engine.preview(KEY, ["langchain"])["plan_id"])
    path.write_text(path.read_text() + "# Later user edit\n")
    with pytest.raises(SetupError, match="newer settings"):
        engine.restore(tx["transaction_id"])
    assert path.read_text().endswith("# Later user edit\n")


def test_uninitialized_library_is_not_a_configurable_project(tmp_path):
    (tmp_path / "requirements.txt").write_text("openai\n")
    (tmp_path / "main.py").write_text(
        "from openai import OpenAI\n# No application constructor yet\n"
    )
    ctx = Context(home=tmp_path, env={}, use_path=False, roots=[tmp_path])
    with pytest.raises(SetupError, match="library installation alone"):
        projects.find(ctx, "openai-sdk")
    app = next(x for x in Engine(ctx).scan()["apps"] if x["id"] == "openai-sdk")
    assert app["mode"] == "guided"


def test_relative_module_is_not_mistaken_for_official_sdk():
    _, calls = projects.analyze(b"from .openai import OpenAI\nclient=OpenAI()\n", "openai-sdk")
    assert calls == []
