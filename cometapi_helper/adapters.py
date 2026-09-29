"""Documented client schemas. Each adapter prepares bytes; it never writes files."""

import io
import json
import re
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

import json5
import tomlkit
from ruamel.yaml import YAML

from . import chatbox
from .common import ANTHROPIC_URL, BASE_URL, SetupError
from .storage import read_file

AUTOMATIC_IDS = {
    "claude-code",
    "codex",
    "aider",
    "continue",
    "opencode",
    "nextchat",
    "librechat",
    "chatbox",
    "invokeai",
    "openviking",
    "openmaic",
    "sillytavern",
    "automatic1111",
    "goose",
    "anythingllm",
    "kilo-code",
    "comfyui",
    "cline",
    "lobechat",
    "zed",
    "jan",
    "open-webui",
    "litellm",
    "fooocus",
    "n8n",
    "flowise",
    "gemini-cli",
    "cherry-studio",
    "roo-code",
    "openai-sdk",
    "langchain",
    "semantica",
    "dify",
    "github-copilot",
    "lm-studio",
}


@dataclass(repr=False)
class Change:
    app_id: str
    app_name: str
    path: Path
    before: bytes
    after: bytes
    fields: list
    warnings: list = field(default_factory=list)
    resource: dict = None
    preserve_mode: bool = False
    contains_credentials: bool = False
    private_sidecar: bool = False
    credential_protection: bool = False
    restore_contains_credentials: bool = False
    persistent_protection: bool = False

    @property
    def action(self):
        if self.before == self.after:
            return "unchanged"
        return "create" if self.before is None else "update"

    def public(self):
        location = str(self.path)
        if self.resource and self.resource.get("kind") == "dify-local-docker-native-v1":
            location = (
                "Local Docker: Dify "
                + self.resource["container"][:12]
                + " (encrypted model credentials)"
            )
        elif self.resource and self.resource.get("database"):
            location += " (connection settings only)"
        elif self.resource:
            label = (
                "Windows Credential Manager: "
                if self.resource.get("kind", "").startswith("windows-")
                else "macOS Keychain: "
            )
            location = label + (
                self.resource.get("server")
                or self.resource["service"] + " / " + self.resource["account"]
            )
        return {
            "app_id": self.app_id,
            "app_name": self.app_name,
            "path": location,
            "fields": self.fields,
            "action": self.action,
        }


def object_at(value, key):
    if key not in value:
        value[key] = {}
    if not isinstance(value[key], MutableMapping):
        raise SetupError(
            "The existing '" + key + "' setting has an unexpected format; no files were changed."
        )
    return value[key]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def edit(
    app_id,
    name,
    path,
    fmt,
    update,
    fields,
    warnings=None,
    *,
    contains_credentials=False,
    private_sidecar=False,
):
    before = read_file(path)
    try:
        raw = before.decode("utf-8-sig") if before is not None else ""
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.allow_duplicate_keys = False
        if fmt == "json":
            document = json.loads(raw, object_pairs_hook=_unique_object) if raw.strip() else {}
        elif fmt == "jsonc":
            document = json5.loads(raw, allow_duplicate_keys=False) if raw.strip() else {}
        elif fmt == "toml":
            document = tomlkit.parse(raw)
        elif fmt == "yaml":
            document = yaml.load(raw) if raw.strip() else {}
        else:
            raise ValueError("Unknown format")
        if not isinstance(document, MutableMapping):
            raise SetupError("Expected an object in " + str(path))
        # Compare semantic structure before serialization to avoid formatting-only rewrites.
        previous = json.dumps(document, default=str, sort_keys=True)
        update(document)
        if previous == json.dumps(document, default=str, sort_keys=True) and before is not None:
            after = before
        elif fmt in ("json", "jsonc"):
            after = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        elif fmt == "toml":
            after = tomlkit.dumps(document).encode("utf-8")
        else:
            stream = io.StringIO()
            yaml.dump(document, stream)
            after = stream.getvalue().encode("utf-8")
    except SetupError:
        raise
    except Exception:
        # Parser diagnostics often embed source lines containing old API keys.
        raise SetupError(
            "Cannot safely parse or update " + str(path) + ". Fix its format before retrying."
        ) from None
    return Change(
        app_id,
        name,
        path,
        before,
        after,
        fields,
        warnings or [],
        contains_credentials=contains_credentials,
        private_sidecar=private_sidecar,
    )


def env_edit(app_id, name, path, updates, *, contains_credentials=False, private_sidecar=False):
    before = read_file(path)
    try:
        raw = before.decode("utf-8") if before is not None else ""
    except UnicodeError:
        raise SetupError("Cannot read the environment file as UTF-8: " + str(path)) from None
    lines = raw.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in raw else "\n"
    seen = set()
    output = []
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)[\r\n]*$", line)
        if match and match[2].startswith(('"', "'")):
            # A target-looking line may be inside another variable's multiline
            # value. Refuse unsupported quoting before editing any assignments.
            value = match[2]
            quoted = (
                r'^"(?:[^"\\]|\\.)*"\s*(?:#.*)?$'
                if value.startswith('"')
                else r"^'[^']*'\s*(?:#.*)?$"
            )
            if not re.fullmatch(quoted, value):
                raise SetupError(
                    "An environment setting uses multiline or unusual quoting in " + str(path)
                )
        if match and match[1] in updates:
            name_key = match[1]
            if name_key in seen:
                raise SetupError(
                    "Duplicate target setting in " + str(path) + "; resolve it before retrying."
                )
            seen.add(name_key)
            update = updates[name_key]
            value = update(match[2]) if callable(update) else update
            output.append(name_key + "=" + value + newline)
        else:
            output.append(line)
    if output and not output[-1].endswith(("\n", "\r")):
        output[-1] += newline
    for name_key, value in updates.items():
        if name_key not in seen:
            value = value(None) if callable(value) else value
            output.append(name_key + "=" + value + newline)
    return Change(
        app_id,
        name,
        path,
        before,
        "".join(output).encode("utf-8"),
        list(updates),
        contains_credentials=contains_credentials,
        private_sidecar=private_sidecar,
    )


def nextchat_custom_models(raw_value, model):
    """Preserve existing directives and ensure this model uses the OpenAI route."""
    value = (raw_value or "").strip()
    if value.startswith(('"', "'")):
        quote = value[0]
        match = re.fullmatch(re.escape(quote) + r"(.*?)" + re.escape(quote) + r"\s*(?:#.*)?", value)
        if not match:
            raise SetupError(
                "Cannot safely read NextChat CUSTOM_MODELS; resolve its quoting before setup."
            )
        value = match[1]
    else:
        value = value.split("#", 1)[0].rstrip()
    # Next.js expands dollar references, and backslashes/escaped quotes are
    # ambiguous across dotenv versions. Never rewrite unresolved expressions.
    if any(character in value for character in ("$", "\\", '"', "`")) or not all(
        character.isprintable() for character in value
    ):
        raise SetupError(
            "NextChat CUSTOM_MODELS contains unresolved variables or unsupported escaping; use a plain model list before setup."
        )
    target = model + "@openai"
    has_route = False
    enabled = False
    for directive in value.split(","):
        available = not directive.startswith("-")
        identity = directive[1:] if directive.startswith(("+", "-")) else directive
        identity = identity.split("=", 1)[0]
        if "@" in identity:
            model_id, provider = identity.rsplit("@", 1)
            if model_id == model and provider.lower() == "openai":
                has_route, enabled = True, available
        elif identity in ("all", model) and has_route:
            enabled = available
    if not has_route or not enabled:
        value += ("," if value and not value.endswith(",") else "") + "+" + target
    # Quoting keeps spaces and literal # characters in existing display aliases.
    return json.dumps(value, ensure_ascii=False)


def prepare(app_id, name, ctx, key, models, repositories):
    from .file_clients import IDS, prepare_files

    if app_id in IDS:
        return prepare_files(app_id, name, ctx, key, models, repositories)
    chat = models["chat_model"]
    if app_id == "chatbox":
        path = chatbox.selected_config(ctx)
        chatbox.require_closed(ctx)
        return [
            edit(
                app_id,
                name,
                path,
                "json",
                lambda document: chatbox.update_document(document, key, chat),
                [
                    "settings.customProviders.CometAPI",
                    "settings.providers.cometapi-connect",
                    "settings.defaultChatModel",
                ],
                [
                    "Keep Chatbox closed until setup finishes. Open a new conversation to use CometAPI; existing conversations keep their selected model. Other providers and non-chat features keep their settings.",
                    "New models use text chat with a 2,048-token output allowance. Vision, tools, and model-specific sampling support are not inferred.",
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "claude-code":
        path = ctx.config_dir("CLAUDE_CONFIG_DIR", ".claude") / "settings.json"

        def update(doc):
            env = object_at(doc, "env")
            env.update(
                {
                    "ANTHROPIC_AUTH_TOKEN": key,
                    "ANTHROPIC_BASE_URL": ANTHROPIC_URL,
                    "ANTHROPIC_MODEL": models["claude_model"],
                }
            )
            env.pop("ANTHROPIC_API_KEY", None)
            # Native cloud-provider switches would bypass the configured gateway.
            for option in (
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
            ):
                if option in env:
                    env[option] = "0"
            doc["model"] = models["claude_model"]

        return [
            edit(
                app_id,
                name,
                path,
                "json",
                update,
                [
                    "env.ANTHROPIC_AUTH_TOKEN",
                    "env.ANTHROPIC_BASE_URL",
                    "env.ANTHROPIC_MODEL",
                    "model",
                    "remove conflicting stored API key; disable stored cloud-provider switches",
                ],
                [
                    "Restart Claude Code. Existing login sessions, project settings, or externally set environment variables may take precedence; use /logout if it still uses your subscription."
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "codex":
        path = ctx.config_dir("CODEX_HOME", ".codex") / "config.toml"

        def update(doc):
            doc["model"] = models["codex_model"]
            doc["model_provider"] = "cometapi"
            provider = object_at(object_at(doc, "model_providers"), "cometapi")
            provider.update(
                {
                    "name": "CometAPI",
                    "base_url": BASE_URL,
                    "wire_api": "responses",
                    "experimental_bearer_token": key,
                    "requires_openai_auth": False,
                }
            )
            for item in (
                "auth",
                "env_key",
                "env_key_instructions",
                "http_headers",
                "env_http_headers",
            ):
                provider.pop(item, None)

        return [
            edit(
                app_id,
                name,
                path,
                "toml",
                update,
                [
                    "model",
                    "model_provider",
                    "model_providers.cometapi (endpoint, Responses protocol, credential)",
                ],
                [
                    "Uses Codex's documented experimental_bearer_token setting in its private config file. Restart Codex; an explicitly selected profile or project override can take precedence. Responses/tool support depends on the model."
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "aider":

        def update(doc):
            doc.update(
                {"openai-api-base": BASE_URL, "openai-api-key": key, "model": "openai/" + chat}
            )
            for override in ("weak-model", "editor-model"):
                if override in doc:
                    doc[override] = "openai/" + chat

        return [
            edit(
                app_id,
                name,
                ctx.home / ".aider.conf.yml",
                "yaml",
                update,
                ["openai-api-base", "openai-api-key", "model", "existing weak-model/editor-model"],
                [
                    "Restart Aider. Repository config, .env files, and command-line flags can override home settings. Unrecognized models may produce Aider's model-metadata warning."
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "continue":
        directory = ctx.config_dir("CONTINUE_GLOBAL_DIR", ".continue")
        path = directory / "config.yaml"
        if not path.exists() and (directory / "config.json").exists():
            raise SetupError(
                "Continue uses legacy config.json. Migrate it to config.yaml in Continue before automatic setup, to preserve existing settings."
            )

        def update(doc):
            doc.setdefault("name", "Local configuration")
            doc.setdefault("version", "1.0.0")
            doc.setdefault("schema", "v1")
            if doc["schema"] != "v1":
                raise SetupError(
                    "This Continue schema is not supported. Use its in-app provider settings."
                )
            items = doc.setdefault("models", [])
            if not isinstance(items, list):
                raise SetupError("Continue models must be a list.")
            entry = {
                "name": "CometAPI",
                "provider": "openai",
                "model": chat,
                "apiBase": BASE_URL,
                "apiKey": key,
                "roles": ["chat", "edit", "apply"],
            }
            # Put this model first while retaining all other providers and their options.
            doc["models"] = [entry] + [
                item
                for item in items
                if not isinstance(item, dict) or item.get("name") != "CometAPI"
            ]

        return [
            edit(
                app_id,
                name,
                path,
                "yaml",
                update,
                ["models.CometAPI (chat, edit, apply)", "missing name/version/schema"],
                [
                    "Reload the editor and choose the local configuration/CometAPI model if Continue remembers a different assistant. Autocomplete and embeddings retain their existing providers."
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "opencode":
        paths = opencode_paths(ctx)
        existing = [p for p in paths if p.exists()]

        def update(doc):
            if "providers" not in doc:
                entry = object_at(object_at(doc, "provider"), "cometapi")
                entry["npm"] = "@ai-sdk/openai-compatible"
                entry["name"] = "CometAPI"
                entry["options"] = {"baseURL": BASE_URL, "apiKey": key}
                selected = object_at(object_at(entry, "models"), chat)
                selected.setdefault("name", chat)
                selected.setdefault("limit", {"context": 32768, "output": 2048})
            elif "providers" in doc and "provider" not in doc:
                raise SetupError(
                    "OpenCode 2 beta uses the separate opencode2 application. Use guided setup for its changing provider schema; OpenCode 1 cannot use these settings."
                )
            else:
                raise SetupError(
                    "Cannot identify this OpenCode provider schema safely. Add one provider in OpenCode first or use guided setup."
                )
            doc["model"] = "cometapi/" + chat

        return [
            edit(
                app_id,
                name,
                path,
                "jsonc",
                update,
                ["CometAPI provider", "model"],
                [
                    "Existing JSONC comments will be removed; other provider values are retained. Restart OpenCode. Project config can override the global provider."
                ],
                contains_credentials=True,
            )
            for path in (existing or paths[:1])
        ]
    if app_id in ("nextchat", "librechat"):
        locations = repositories.get(app_id, [])
        if not locations:
            raise SetupError(
                name
                + " needs a detected source checkout. Add its project folder under additional scan paths."
            )
        changes = []
        for root in locations:
            if app_id == "nextchat":
                change = env_edit(
                    app_id,
                    name,
                    root / ".env.local",
                    {
                        "OPENAI_API_KEY": key,
                        "BASE_URL": ANTHROPIC_URL,
                        "DEFAULT_MODEL": chat + "@openai",
                        "CUSTOM_MODELS": lambda value: nextchat_custom_models(value, chat),
                    },
                    contains_credentials=True,
                    private_sidecar=True,
                )
                change.warnings = [
                    "NextChat source install: restart the Next.js process. Docker/cloud deployments and saved browser settings require their own settings. .env.local is not automatically loaded by Docker Compose.",
                    "The examined NextChat upstream server code logs selected API keys. Check your installed app/config/server.ts logging before use; the helper's redacted output cannot prevent another app from logging your key.",
                ]
                changes.append(change)
            else:
                path = root / "librechat.yaml"
                template = root / "librechat.example.yaml"
                version = None
                if not path.exists():
                    try:
                        parsed = YAML(typ="safe").load((read_file(template) or b"").decode("utf-8"))
                        version = parsed["version"]
                    except Exception:
                        raise SetupError(
                            "LibreChat needs its installed librechat.example.yaml to determine the supported schema."
                        ) from None

                def update(doc):
                    if version is not None:
                        doc["version"] = version
                    if "version" not in doc:
                        raise SetupError("LibreChat config is missing its schema version.")
                    endpoints = object_at(doc, "endpoints")
                    custom = endpoints.setdefault("custom", [])
                    if not isinstance(custom, list):
                        raise SetupError("LibreChat custom endpoints must be a list.")
                    custom[:] = [
                        item
                        for item in custom
                        if not isinstance(item, dict) or item.get("name") != "CometAPI"
                    ]
                    custom.append(
                        {
                            "name": "CometAPI",
                            "apiKey": "${COMETAPI_API_KEY}",
                            "baseURL": BASE_URL,
                            "models": {"default": [chat], "fetch": False},
                            "titleConvo": True,
                            "titleModel": "current_model",
                            "modelDisplayLabel": "CometAPI",
                        }
                    )

                changes.append(
                    edit(
                        app_id,
                        name,
                        path,
                        "yaml",
                        update,
                        ["endpoints.custom.CometAPI"],
                        [
                            "Restart LibreChat, then select CometAPI. Docker installations must mount librechat.yaml into /app/librechat.yaml; this helper does not change container deployments. Model fetching is disabled because the public CometAPI catalog uses /api/models."
                        ],
                    )
                )
                changes.append(
                    env_edit(
                        app_id,
                        name,
                        root / ".env",
                        {"COMETAPI_API_KEY": key},
                        contains_credentials=True,
                        private_sidecar=True,
                    )
                )
        return changes
    raise SetupError("This app supports guided setup only.")


def opencode_paths(ctx):
    if ctx.env.get("OPENCODE_CONFIG"):
        path = Path(ctx.env["OPENCODE_CONFIG"]).expanduser()
        if not path.is_absolute():
            raise SetupError("OPENCODE_CONFIG must contain an absolute path.")
        return [path]
    return [ctx.xdg / "opencode" / filename for filename in ("opencode.json", "opencode.jsonc")]
