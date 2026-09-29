"""Shared validation and platform paths. No client commands are executed."""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

BASE_URL = "https://api.cometapi.com/v1"
ANTHROPIC_URL = "https://api.cometapi.com"
DEFAULT_MODELS = {
    "chat_model": "gpt-5.4-mini",
    "claude_model": "claude-sonnet-5",
    "codex_model": "gpt-6-astra",
    "image_model": "gpt-image-1.5",
}


class SetupError(Exception):
    """A safe, user-facing error that must never include configuration contents."""


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"sk-[A-Za-z0-9_\-]{8,250}", value):
        raise SetupError("Enter a CometAPI key beginning with sk-, without spaces or line breaks.")
    return value


def validate_models(values=None):
    if values is not None and not isinstance(values, dict):
        raise SetupError("Model settings must be an object.")
    result = dict(DEFAULT_MODELS)
    for key, value in (values or {}).items():
        if key not in result:
            raise SetupError("Unknown model setting.")
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/\-]{0,149}", value
        ):
            raise SetupError("Use an exact CometAPI model ID without spaces or line breaks.")
        result[key] = value
    if not result["claude_model"].startswith("claude-"):
        raise SetupError("Claude Code requires a Claude model ID beginning with claude-.")
    return result


@dataclass
class Context:
    home: Path = field(default_factory=Path.home)
    platform: str = sys.platform
    env: Dict[str, str] = field(default_factory=lambda: dict(os.environ))
    roots: list = field(default_factory=list)
    use_path: bool = True

    def __post_init__(self):
        self.home = Path(self.home).expanduser().absolute()
        if not self.home.is_dir():
            raise SetupError("The selected home directory does not exist.")
        self.roots = [Path(p).expanduser().absolute() for p in self.roots]

    def config_dir(self, variable, fallback):
        value = self.env.get(variable)
        if value:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise SetupError(variable + " must contain an absolute directory path.")
            return path
        return self.home / fallback

    @property
    def xdg(self):
        return self.config_dir("XDG_CONFIG_HOME", ".config")

    @property
    def state_dir(self):
        return self.home / ".cometapi-helper"

    @property
    def project_roots(self):
        defaults = [
            self.home / p for p in ("code", "Projects", "Developer", "GitHub", "Documents/GitHub")
        ]
        return list(dict.fromkeys(self.roots + defaults))
