"""Translation integrity: omitted warnings and broken placeholders must fail CI."""

import ast
import json
import re
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1] / "cometapi_helper/static/locales"
LANGUAGES = {
    "en",
    "zh-TW",
    "ja",
    "ko",
    "fr",
    "de",
    "es",
    "it",
    "pt",
    "ru",
    "ar",
    "th",
    "vi",
    "id",
    "tr",
    "pl",
}


def test_all_website_languages_have_complete_translations():
    assert {path.stem for path in FOLDER.glob("*.json")} == LANGUAGES
    english = json.loads((FOLDER / "en.json").read_text(encoding="utf-8"))
    for code in LANGUAGES:
        translated = json.loads((FOLDER / (code + ".json")).read_text(encoding="utf-8"))
        assert translated.keys() == english.keys(), code
        for source, value in translated.items():
            assert isinstance(value, str) and value.strip(), (code, source)
            assert sorted(re.findall(r"\{\w+\}", value)) == sorted(
                re.findall(r"\{\w+\}", source)
            ), (code, source)
            for url in re.findall(r"https?://[^\s,;]+", source):
                assert url.rstrip(".") in value, (code, source)


def test_app_guides_and_preview_warnings_have_translation_keys():
    root = FOLDER.parents[1]
    english = json.loads((FOLDER / "en.json").read_text(encoding="utf-8"))
    for filename in ("catalog.json", "coding_catalog.json"):
        for app in json.loads((root / filename).read_text(encoding="utf-8")):
            for source in [
                app.get("description", ""),
                *app.get("instructions", []),
                *app.get("caveats", []),
            ]:
                if source:
                    assert source in english, (app["id"], source)
    for item in ast.walk(ast.parse((root / "file_clients.py").read_text(encoding="utf-8"))):
        if (
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "Change"
            and len(item.args) >= 7
            and isinstance(item.args[6], ast.List)
        ):
            for warning in item.args[6].elts:
                if isinstance(warning, ast.Constant) and isinstance(warning.value, str):
                    assert warning.value in english, warning.value
