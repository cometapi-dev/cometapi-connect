"""Generate packaged media adapters; --check verifies committed copies without writing."""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generated_assets():
    source = (ROOT / "integrations/sd-webui-cometapi/scripts/cometapi_media.py").read_text(
        encoding="utf-8"
    )
    if source.count("\ndef ui():") != 1:
        raise ValueError("Expected exactly one media UI boundary")
    cloud = source.split("\ndef ui():", 1)[0]
    replacements = {
        "import gradio as gr\n": "import folder_paths\n",
        "from modules import paths, script_callbacks\n": "",
        'Path(paths.data_path) / "outputs" / "cometapi"': 'Path(folder_paths.get_output_directory()) / "cometapi"',
        'Path(__file__).resolve().parent.parent / "cometapi.json"': 'Path(__file__).resolve().parent / "cometapi.json"',
    }
    for old, new in replacements.items():
        if cloud.count(old) != 1:
            raise ValueError(f"Expected one media template anchor: {old!r}")
        cloud = cloud.replace(old, new)
    return {"sd_webui_cometapi.py": source, "comfy_cloud.py": cloud.rstrip() + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, content in generated_assets().items():
        path = ROOT / "cometapi_helper/assets" / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
    if stale:
        parser.exit(1, "Regenerate media assets: " + ", ".join(stale) + "\n")


if __name__ == "__main__":
    main()
