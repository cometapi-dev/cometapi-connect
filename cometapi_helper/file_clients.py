"""Additional adapters tied to configuration readers in the installed clients."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from . import (
    cherry_store,
    copilot_store,
    credentials,
    dify_store,
    flowise_store,
    fooocus,
    gemini_launcher,
    jan,
    litellm_config,
    lmstudio_store,
    n8n_store,
    python_projects,
    roo_store,
    webui_store,
)
from .adapters import Change, edit, env_edit, object_at
from .common import ANTHROPIC_URL, BASE_URL, SetupError
from .storage import read_file

SOURCE_IDS = {"openmaic", "sillytavern", "automatic1111", "comfyui", "lobechat", "fooocus"}
IDS = (
    SOURCE_IDS
    | python_projects.IDS
    | {
        "invokeai",
        "openviking",
        "goose",
        "anythingllm",
        "kilo-code",
        "cline",
        "zed",
        "jan",
        "open-webui",
        "litellm",
        "n8n",
        "flowise",
        "gemini-cli",
        "cherry-studio",
        "roo-code",
        "dify",
        "github-copilot",
        "lm-studio",
    }
)


def zed_settings(ctx):
    # Zed's documented --user-data-dir keeps config alongside its DB/extensions.
    profiles = [
        root / "config/settings.json"
        for root in ctx.roots
        if (root / "config/settings.json").is_file()
        and (root / "db").is_dir()
        and (root / "extensions").is_dir()
    ]
    if len(profiles) > 1:
        raise SetupError("Multiple Zed profiles found; scan one profile at a time.")
    if profiles:
        return profiles[0]
    if ctx.platform == "win32":
        return ctx.config_dir("APPDATA", "AppData/Roaming") / "Zed/settings.json"
    return ctx.xdg / "zed/settings.json"


def anything_storage(ctx):
    if ctx.env.get("STORAGE_DIR"):
        return ctx.config_dir("STORAGE_DIR", ".config/anythingllm-desktop/storage")
    candidates = [root for root in ctx.roots if (root / "anythingllm.db").is_file()]
    if ctx.platform == "darwin":
        default = ctx.home / "Library/Application Support/anythingllm-desktop/storage"
    elif ctx.platform == "win32":
        default = (
            Path(ctx.env.get("APPDATA", str(ctx.home / "AppData/Roaming")))
            / "anythingllm-desktop/storage"
        )
    else:
        default = ctx.xdg / "anythingllm-desktop/storage"
    if (default / "anythingllm.db").is_file():
        candidates.append(default)
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) > 1:
        raise SetupError(
            "Multiple AnythingLLM storage directories found; scan one installation at a time."
        )
    return candidates[0] if candidates else default


def goose_dir(ctx):
    if ctx.env.get("GOOSE_PATH_ROOT"):
        return ctx.config_dir("GOOSE_PATH_ROOT", ".config/goose") / "config"
    if ctx.platform == "win32":
        return (
            Path(ctx.env.get("APPDATA", str(ctx.home / "AppData/Roaming"))) / "Block/goose/config"
        )
    return ctx.xdg / "goose"


def invoke_root(ctx):
    if ctx.env.get("INVOKEAI_ROOT"):
        return ctx.config_dir("INVOKEAI_ROOT", "invokeai")
    candidates = [root for root in ctx.roots if (root / "invokeai.yaml").is_file()]
    if (ctx.home / "invokeai/invokeai.yaml").is_file():
        candidates.append(ctx.home / "invokeai")
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) > 1:
        raise SetupError("Multiple InvokeAI data roots found; scan one installation at a time.")
    return candidates[0] if candidates else ctx.home / "invokeai"


def silly_profile(root):
    try:
        config = YAML(typ="safe").load((read_file(root / "config.yaml") or b"{}").decode("utf-8"))
        data = Path(config.get("dataRoot", "./data"))
        data = data if data.is_absolute() else root / data
        profile = data / "default-user"
        # Multi-user accounts must not silently receive another user's key.
        if config.get("enableUserAccounts"):
            raise SetupError(
                "SillyTavern multi-user installations require an account-specific configuration connection."
            )
        if not (profile / "settings.json").is_file():
            raise SetupError("SillyTavern has not initialized its default profile yet.")
        return profile
    except SetupError:
        raise
    except Exception:
        raise SetupError("Cannot identify SillyTavern's data directory safely.") from None


def prepare_files(app_id, name, ctx, key, models, repositories):
    chat = models["chat_model"]
    if app_id in python_projects.IDS:
        return python_projects.changes(app_id, name, ctx, key, models)
    if app_id == "lm-studio":
        from .common import DEFAULT_MODELS

        target = lmstudio_store.find(ctx)
        before = lmstudio_store.read(target)
        model = "gpt-4.1-mini-2025-04-14" if chat == DEFAULT_MODELS["chat_model"] else chat
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                lmstudio_store.prepare_value(before, key, model),
                ["Official remote generator endpoint and key", "Selected chat model: " + model],
                [
                    "Supports compatible LM Studio profiles on macOS and Windows with the official OpenAI-compatible generator. A missing plugin is installed from a verified bundled copy of the official plugin and its dependencies; an initial CometAPI chat is created when no chat is selected. Close the app first. The official plugin stores its key in a private plaintext configuration file. This adapter configures the selected chat and preserves messages during restore. Installed plugin files remain after connection restore. First-run LM Studio terms must already be accepted. The standard Connect model selection uses GPT-4.1 mini because the official generator declares only four models."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "github-copilot":
        target = copilot_store.find(ctx)
        before = copilot_store.read(target)
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                copilot_store.prepare_value(before, key, chat),
                ["Native encrypted Custom Endpoint key, chat model and utility models"],
                [
                    "Supports VS Code with bundled Copilot Chat with compatible native model APIs in an initialized default macOS or default/portable Windows profile. Close that profile first. Setup opens an empty native configuration window and closes it. BYOK chat and utility tasks are configured; GitHub-only completions/embeddings are not covered. Restore preserves original model files and selection; unused encrypted secrets created by VS Code remain in its native secret store."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "dify":
        target = dify_store.find(ctx)
        before = dify_store.read(target)
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                dify_store.prepare_value(target, before, key),
                ["Existing OpenAI-compatible encrypted model credentials and API endpoints"],
                [
                    "Supports the verified Dify API container Docker API image with official OpenAI-compatible plugin 0.0.66 on local macOS/Linux Docker. Requires one active owner and existing chat models. Model IDs and app bindings are preserved. Other images, remote Docker, multiple workspaces, custom routing and load balancing require further adaptation."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "roo-code":
        target = roo_store.find(ctx)
        before = roo_store.read(target)
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                roo_store.prepare_value(before, key, chat),
                ["Native encrypted CometAPI profile, active model and current mode binding"],
                [
                    "Supports Roo Code with a compatible native configuration API in the default macOS or default/portable Windows profile of VS Code. Close that profile and any active Roo task first. Preview, apply and restore briefly open an empty configuration window and close it automatically. Other profiles, tasks, providers and workspace trust are preserved. Named/remote profiles and incompatible native APIs need separate adapters."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "cherry-studio":
        target = cherry_store.find(ctx)
        before = cherry_store.read(target)
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                cherry_store.prepare_value(before, key, chat),
                [
                    "CometAPI provider, private key, enabled model, default model and default assistant"
                ],
                [
                    "Supports compatible Cherry Studio Redux provider stores on macOS and Windows. Quit Cherry Studio before applying or restoring, then reopen it. Configures the default assistant and new-assistant template. Other assistants, conversations and providers are preserved. Restoring removes provider and default-assistant model changes, while refusing later edits to those settings."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "gemini-cli":
        return gemini_launcher.changes(name, ctx, key, chat)
    if app_id == "flowise":
        target = flowise_store.find(ctx)
        before = flowise_store.read(target)
        return [
            Change(
                app_id,
                name,
                Path(target["database"]),
                before,
                flowise_store.prepare_value(target, before, key),
                ["Existing ChatOpenAI node endpoints and encrypted credentials"],
                [
                    "Supports existing ChatOpenAI nodes with compatible credential and endpoint fields in a single-user, single-workspace Flowise SQLite instance. Stop Flowise before applying and restart it afterwards. Existing models and workflow connections are preserved; the selected chat model does not rewrite workflows. Credentials shared with other node types require separate setup."
                ],
                target,
                contains_credentials=True,
            )
        ]
    if app_id == "n8n":
        path = n8n_store.find(ctx)
        resource = n8n_store.resource(path)
        before = n8n_store.read(resource)
        return [
            Change(
                app_id,
                name,
                path,
                before,
                n8n_store.prepare_value(resource, before, key),
                [
                    "Existing OpenAI credential endpoint and encrypted API key",
                    "Clear previous OpenAI organization ID",
                ],
                [
                    "Updates existing OpenAI credentials in a single personal n8n SQLite installation. Stop n8n before applying and start it again afterwards. Existing workflow credential bindings and chosen models are preserved; the selected chat model above does not rewrite workflows. Models and operations must be available through CometAPI. Multi-user, external database and rotating/managed credentials need separate adapters."
                ],
                resource,
                contains_credentials=True,
            )
        ]
    if app_id == "litellm":
        return litellm_config.changes(name, ctx, key, chat)
    if app_id == "open-webui":
        if ctx.env.get("ENABLE_PERSISTENT_CONFIG", "true").lower() == "false":
            raise SetupError(
                "This Open WebUI launch disables persistent settings, so the database adapter cannot configure it."
            )
        path = webui_store.find(ctx)
        resource = webui_store.resource(path, images=True)
        before = webui_store.read(resource)
        return [
            Change(
                app_id,
                name,
                path,
                before,
                webui_store.prepare_value(before, key, chat, models["image_model"]),
                [
                    "Dedicated CometAPI connection",
                    "default model for new chats",
                    "OpenAI image generation endpoint/key/model (1024×1024, low quality)",
                ],
                [
                    "Configures Open WebUI's per-key SQLite configuration schema. Restart the server to refresh model caches. Existing connections, users and chats are preserved. User-specific defaults may override the server's default model; external databases and disabled persistence require a different adapter."
                ],
                resource,
                contains_credentials=True,
            )
        ]
    if app_id == "jan":
        if ctx.platform not in ("darwin", "win32"):
            raise SetupError(
                "Jan's native credential adapter is currently verified on macOS and Windows."
            )
        resource = dict(
            credentials.JAN_WINDOWS_RESOURCE
            if ctx.platform == "win32"
            else credentials.JAN_RESOURCE
        )
        changes = [
            edit(
                app_id,
                name,
                jan.settings_path(ctx),
                "json",
                lambda doc: jan.update(doc, chat),
                ["CometAPI Connect provider", "selected provider and model"],
                [
                    "Configures initialized Jan Desktop compatible provider-store fields. Quit Jan before applying, then reopen it. macOS may request permission to read the keyring. Existing conversations retain their own model selections. This adapter enables text chat; it does not claim untested model capabilities."
                ],
            ),
            Change(
                app_id,
                name,
                ctx.state_dir / "credential-targets/jan",
                credentials.read(resource),
                credentials.encode(jan.PROVIDER, json.dumps([key]).encode("utf-8")),
                ["Jan system keyring provider key chain"],
                resource=resource,
                contains_credentials=True,
            ),
        ]
        assistant = jan.assistant_path(ctx)
        if assistant:
            changes.append(
                edit(
                    app_id,
                    name,
                    assistant,
                    "json",
                    jan.update_assistant,
                    ["Default Jan assistant: remove top_k and repeat_penalty"],
                    [
                        "Removes local-only top_k and repeat_penalty sampling overrides from the initialized default Jan assistant so new chats work with CometAPI. This default assistant is shared across providers. Other parameters, instructions and tools are preserved. Existing conversations retain their saved assistant parameters; start a new chat after setup."
                    ],
                )
            )
        return changes
    if app_id == "zed":
        if ctx.platform not in ("darwin", "win32"):
            raise SetupError("Zed's system credential adapter supports macOS and Windows.")

        def settings(doc):
            provider = object_at(
                object_at(object_at(doc, "language_models"), "openai_compatible"),
                "CometAPI Connect",
            )
            provider["api_url"] = BASE_URL
            available = provider.setdefault("available_models", [])
            if not isinstance(available, list) or any(not isinstance(m, dict) for m in available):
                raise SetupError("Unrecognized Zed model list.")
            model = next((m for m in available if m.get("name") == chat), None)
            if model is None:
                model = {"name": chat, "max_tokens": 32768, "max_output_tokens": 2048}
                available.append(model)
            object_at(doc, "agent")["default_model"] = {
                "provider": "CometAPI Connect",
                "model": chat,
            }

        resource = (
            credentials.ZED_WINDOWS_RESOURCE
            if ctx.platform == "win32"
            else {"kind": "macos-internet-password", "server": BASE_URL, "app": "zed"}
        )
        return [
            edit(
                app_id,
                name,
                zed_settings(ctx),
                "jsonc",
                settings,
                ["language_models.openai_compatible.CometAPI Connect", "agent.default_model"],
                [
                    "Configures Zed's built-in Agent and system credential store. Restart Zed to load the credential. API URL credentials are shared across Zed profiles; an explicit provider environment variable can override the stored credential."
                ],
            ),
            Change(
                app_id,
                name,
                ctx.state_dir / "credential-targets/zed",
                credentials.read(resource),
                credentials.encode("Bearer", key.encode("utf-8")),
                ["System provider credential (Bearer)"],
                resource=resource,
                contains_credentials=True,
            ),
        ]
    if app_id == "cline":
        directory = (
            ctx.config_dir("CLINE_DATA_DIR", ".cline/data")
            if ctx.env.get("CLINE_DATA_DIR")
            else ctx.config_dir("CLINE_DIR", ".cline") / "data"
        )
        path = (
            Path(ctx.env["CLINE_PROVIDER_SETTINGS_PATH"]).expanduser()
            if ctx.env.get("CLINE_PROVIDER_SETTINGS_PATH")
            else directory / "settings/providers.json"
        )
        if not path.is_absolute():
            raise SetupError("CLINE_PROVIDER_SETTINGS_PATH must be an absolute path.")

        def update(doc):
            doc.setdefault("version", 1)
            if type(doc["version"]) is not int:
                raise SetupError("This Cline provider file uses an malformed schema version.")
            doc.setdefault("modes", {})
            doc["lastUsedProvider"] = "openai-compatible"
            entry = object_at(object_at(doc, "providers"), "openai-compatible")
            settings = object_at(entry, "settings")
            before = json.dumps(settings, sort_keys=True)
            settings.update(provider="openai-compatible", apiKey=key, model=chat, baseUrl=BASE_URL)
            for field, value in (("maxTokens", 2048), ("contextWindow", 32768), ("timeout", 60000)):
                settings.setdefault(field, value)
            # Cline 3.0.61 drops maxTokens while constructing the agent. Its
            # fallback derives output tokens from the context budget instead.
            # Bound that budget for this family until upstream preserves the
            # explicit limit; otherwise short prompts request >16,384 output.
            if chat.startswith("gpt-4o-mini"):
                window = settings.get("contextWindow")
                if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
                    raise SetupError("Cline's contextWindow must be a positive integer.")
                settings["contextWindow"] = min(window, 16384)
            settings.pop("auth", None)
            entry["tokenSource"] = "manual"
            if json.dumps(settings, sort_keys=True) != before or "updatedAt" not in entry:
                entry["updatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        return [
            edit(
                app_id,
                name,
                path,
                "json",
                update,
                ["lastUsedProvider", "OpenAI-compatible provider credentials/model"],
                [
                    "Configures Cline's current shared provider store used by CLI and migrated editor backends. Restart Cline. Older editor versions and explicit per-task model selections use separate settings."
                ]
                + (
                    [
                        "Cline 3.0.61 ignores its provider output limit in agent runs. For GPT-4o mini, the adapter caps the context budget at 16,384 to prevent rejected output-token requests; this reduces the available conversation length."
                    ]
                    if chat.startswith("gpt-4o-mini")
                    else []
                ),
                contains_credentials=True,
            )
        ]
    if app_id == "kilo-code":
        if ctx.env.get("KILO_CONFIG"):
            paths = [Path(ctx.env["KILO_CONFIG"]).expanduser()]
            if not paths[0].is_absolute():
                raise SetupError("KILO_CONFIG must be an absolute path.")
        else:
            directory = (
                ctx.config_dir("KILO_CONFIG_DIR", ".config/kilo")
                if ctx.env.get("KILO_CONFIG_DIR")
                else ctx.xdg / "kilo"
            )
            paths = [
                directory / filename
                for filename in (
                    "config.json",
                    "kilo.json",
                    "kilo.jsonc",
                    "opencode.json",
                    "opencode.jsonc",
                )
            ]
            paths = [path for path in paths if path.exists()] or [directory / "kilo.json"]

        def update(doc):
            doc["model"] = "openai-compatible/" + chat
            entry = object_at(object_at(doc, "provider"), "openai-compatible")
            object_at(entry, "options").update(baseURL=BASE_URL, apiKey=key)
            selected = object_at(object_at(entry, "models"), chat)
            selected.setdefault("name", chat)
            selected.setdefault("limit", {"context": 32768, "output": 2048})

        return [
            edit(
                app_id,
                name,
                path,
                "jsonc",
                update,
                ["OpenAI-compatible provider", "default model"],
                [
                    "Configures Kilo's current shared kilo.json configuration (CLI/new IDE backend). Older extension-only versions use a separate credential store. Restart Kilo."
                ],
                contains_credentials=True,
            )
            for path in paths
        ]
    if app_id == "anythingllm":
        change = env_edit(
            app_id,
            name,
            anything_storage(ctx) / ".env",
            {
                "LLM_PROVIDER": "generic-openai",
                "GENERIC_OPEN_AI_API_KEY": key,
                "GENERIC_OPEN_AI_MODEL_PREF": chat,
                "GENERIC_OPEN_AI_BASE_PATH": BASE_URL,
                "GENERIC_OPENAI_STREAMING_DISABLED": "false",
                "GENERIC_OPEN_AI_MODEL_TOKEN_LIMIT": "32768",
                "GENERIC_OPEN_AI_MAX_TOKENS": "2048",
            },
            contains_credentials=True,
        )
        change.warnings.append(
            "Configures AnythingLLM's Generic OpenAI provider for CometAPI with streaming enabled so agent replies appear immediately. The native CometAPI provider is avoided because of its empty-chunk parsing failure. Restart AnythingLLM. Workspaces with explicit provider/model overrides retain those selections."
        )
        return [change]
    if app_id == "goose":
        directory = goose_dir(ctx)

        def config(doc):
            doc["active_provider"] = "cometapi_connect"
            object_at(object_at(doc, "providers"), "cometapi_connect").update(
                enabled=True, configured=True, model=chat
            )
            for legacy in ("GOOSE_PROVIDER", "GOOSE_MODEL"):
                doc.pop(legacy, None)

        def provider(doc):
            doc.update(
                name="cometapi_connect",
                engine="openai",
                display_name="CometAPI",
                base_url=BASE_URL + "/chat/completions",
                api_key_env="",
                requires_auth=False,
                headers={"Authorization": "Bearer " + key},
                supports_streaming=True,
                dynamic_models=False,
                models=[{"name": chat, "context_limit": 32768}],
            )
            doc.pop("auth", None)

        return [
            edit(
                app_id,
                name,
                directory / "config.yaml",
                "yaml",
                config,
                ["active_provider", "providers.cometapi_connect"],
                [
                    "Uses Goose's supported custom-provider Authorization header, stored in a private provider file. Existing system-keychain settings and other provider credentials are retained. Restart Goose."
                ],
            ),
            edit(
                app_id,
                name,
                directory / "custom_providers/cometapi_connect.json",
                "json",
                provider,
                ["Custom OpenAI-compatible provider, static model list and Authorization header"],
                contains_credentials=True,
            ),
        ]
    if app_id == "invokeai":
        root = invoke_root(ctx)
        if not (root / "invokeai.yaml").is_file():
            raise SetupError(
                "InvokeAI's data root must contain invokeai.yaml. Add that folder to the scan paths."
            )
        return [
            edit(
                app_id,
                name,
                root / "api_keys.yaml",
                "yaml",
                lambda doc: doc.update(
                    external_openai_api_key=key, external_openai_base_url=ANTHROPIC_URL
                ),
                ["external_openai_api_key", "external_openai_base_url"],
                [
                    "Configures InvokeAI's external OpenAI image provider. Local checkpoint generation keeps its models. Restart InvokeAI to load the provider credential."
                ],
                contains_credentials=True,
            )
        ]
    if app_id == "openviking":
        raw = ctx.env.get("OPENVIKING_CONFIG_FILE")
        path = Path(raw) if raw else ctx.home / ".openviking/ov.conf"
        if not path.is_absolute():
            raise SetupError("OPENVIKING_CONFIG_FILE must be an absolute path.")

        def update(doc):
            vlm = object_at(doc, "vlm")
            vlm.update(
                provider="openai", backend="openai", api_key=key, api_base=BASE_URL, model=chat
            )
            # These are alternate VLM credential sources, not unrelated settings.
            for field in ("credentials", "providers", "backup"):
                vlm.pop(field, None)

        return [
            edit(
                app_id,
                name,
                path,
                "json",
                update,
                ["vlm provider, model, endpoint and credential"],
                [
                    "Configures OpenViking's VLM. Existing embedding/index settings remain in place to avoid invalidating stored vectors. Restart OpenViking."
                ],
                contains_credentials=True,
            )
        ]
    locations = repositories.get(app_id, [])
    if not locations:
        raise SetupError(name + " requires a verified source installation in the scan paths.")
    changes = []
    for root in locations:
        if app_id == "fooocus":
            path = root / "webui.py"
            before = read_file(path)
            changes.append(
                Change(
                    app_id,
                    name,
                    path,
                    before,
                    fooocus.patch(before),
                    ["Add cloud panel before Fooocus starts its server"],
                    [
                        "Adds a CometAPI image/video panel to a compatible Fooocus UI. Restart Fooocus normally to load it. Local model settings and generation keep their behavior. Source changes are backed up; ambiguous or incompatible launch structures are not patched."
                    ],
                )
            )
            extension = root / "extensions/cometapi_connect"
            path = extension / "scripts/cometapi_media.py"
            changes.append(
                Change(
                    app_id,
                    name,
                    path,
                    read_file(path),
                    fooocus.extension_source(),
                    ["Install cloud image/video panel"],
                )
            )
            changes.append(
                edit(
                    app_id,
                    name,
                    extension / "cometapi.json",
                    "json",
                    lambda doc: doc.update(api_key=key),
                    ["Private cloud credential"],
                    contains_credentials=True,
                    private_sidecar=True,
                )
            )
        elif app_id == "lobechat":
            changes.append(
                env_edit(
                    app_id,
                    name,
                    root / ".env.local",
                    {
                        "OPENAI_API_KEY": key,
                        "OPENAI_PROXY_URL": BASE_URL,
                        "OPENAI_MODEL_LIST": "+" + chat,
                        "ENABLED_OPENAI": "1",
                    },
                    contains_credentials=True,
                    private_sidecar=True,
                )
            )
            changes[-1].warnings.append(
                "Configures LobeChat's source server provider. Restart the server to load .env.local. Existing agents and browser-saved credentials can override server defaults. Docker image defaults can override a mounted .env.local: existing Compose services are updated below; direct docker-run deployments must consume this file as an environment file."
            )
            for filename in (
                "compose.yaml",
                "compose.yml",
                "docker-compose.yaml",
                "docker-compose.yml",
            ):
                path = root / filename
                if not path.is_file():
                    continue

                compose_state = {"matched": False, "had_credentials": False}

                def compose(doc):
                    services = object_at(doc, "services")
                    for service in services.values():
                        if not isinstance(service, dict):
                            raise SetupError("Unrecognized Compose service definition.")
                        image = service.get("image", "")
                        if not isinstance(image, str) or image.split("@")[0].split(":")[0] not in (
                            "lobehub/lobe-chat",
                            "lobehub/lobehub",
                        ):
                            continue
                        env = service.get("environment", {})
                        if isinstance(env, list):
                            parsed = {}
                            for item in env:
                                if not isinstance(item, str):
                                    raise SetupError("Unrecognized Compose environment entry.")
                                name, sep, value = item.partition("=")
                                if name in parsed:
                                    raise SetupError("Duplicate Compose environment entries.")
                                parsed[name] = value if sep else None
                            env = parsed
                        if not isinstance(env, dict):
                            raise SetupError("Unrecognized Compose environment format.")
                        compose_state["matched"] = True
                        old_key = env.get("OPENAI_API_KEY")
                        reference = isinstance(old_key, str) and re.fullmatch(
                            r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})", old_key
                        )
                        compose_state["had_credentials"] |= bool(old_key) and not bool(reference)
                        env.pop("OPENAI_API_KEY", None)
                        env_files = service.get("env_file", [])
                        if isinstance(env_files, str):
                            env_files = [env_files]
                        if not isinstance(env_files, list) or any(
                            not isinstance(item, str)
                            and not (isinstance(item, dict) and isinstance(item.get("path"), str))
                            for item in env_files
                        ):
                            raise SetupError("Unrecognized Compose environment file configuration.")
                        private_env = ".cometapi-connect.env"
                        service["env_file"] = [
                            item
                            for item in env_files
                            if (item if isinstance(item, str) else item["path"])
                            not in (private_env, "./" + private_env)
                        ] + ["./" + private_env]
                        # Quote newly inserted strings even in an existing flow
                        # mapping; Compose parsers differ on unquoted URL colons.
                        env.update(
                            {
                                field: DoubleQuotedScalarString(value)
                                for field, value in {
                                    "OPENAI_PROXY_URL": BASE_URL,
                                    "OPENAI_MODEL_LIST": "+" + chat,
                                    "ENABLED_OPENAI": "1",
                                }.items()
                            }
                        )
                        service["environment"] = env

                changes.append(
                    edit(
                        app_id,
                        name,
                        path,
                        "yaml",
                        compose,
                        ["LobeChat Compose service provider environment"],
                        [
                            "Recreate the selected LobeChat Compose service to load its updated environment. Existing ports, volumes, authentication and other services are preserved."
                        ],
                    )
                )
                changes[-1].restore_contains_credentials = compose_state["had_credentials"]
                if compose_state["matched"] and not any(
                    change.path == root / ".cometapi-connect.env" for change in changes
                ):
                    changes.append(
                        env_edit(
                            app_id,
                            name,
                            root / ".cometapi-connect.env",
                            {"OPENAI_API_KEY": key},
                            contains_credentials=True,
                            private_sidecar=True,
                        )
                    )
        elif app_id == "openmaic":
            changes.append(
                env_edit(
                    app_id,
                    name,
                    root / ".env.local",
                    {
                        "OPENAI_API_KEY": key,
                        "OPENAI_BASE_URL": BASE_URL,
                        "OPENAI_MODELS": chat,
                        "IMAGE_OPENAI_API_KEY": key,
                        "IMAGE_OPENAI_BASE_URL": BASE_URL,
                    },
                    contains_credentials=True,
                    private_sidecar=True,
                )
            )
            changes[-1].warnings.append(
                "Configures OpenMAIC's OpenAI chat and image providers for its source installation. Restart the server. Existing browser selections can select a different provider. Video providers require their own protocol adapters."
            )
        elif app_id == "sillytavern":
            profile = silly_profile(root)

            def settings(doc):
                doc["main_api"] = "openai"
                object_at(doc, "oai_settings").update(
                    chat_completion_source="custom",
                    custom_model=chat,
                    custom_url=BASE_URL,
                    custom_include_headers="",
                    custom_include_body="",
                    custom_exclude_body="",
                )

            def secrets(doc):
                # Preserve the installed schema: old releases use strings; newer
                # releases migrate all entries together into active-key arrays.
                modern = (
                    "_migrated" in doc
                    or any(isinstance(value, list) for value in doc.values())
                    or not doc
                )
                if not modern:
                    doc["api_key_custom"] = key
                    return
                items = doc.setdefault("api_key_custom", [])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise SetupError("Unrecognized SillyTavern credential schema.")
                for item in items:
                    item["active"] = False
                own = next((item for item in items if item.get("id") == "cometapi-connect"), None)
                if own is None:
                    own = {"id": "cometapi-connect"}
                    items.append(own)
                own.update(value=key, label="CometAPI Connect", active=True)

            changes.extend(
                [
                    edit(
                        app_id,
                        name,
                        profile / "settings.json",
                        "json",
                        settings,
                        [
                            "main_api",
                            "oai_settings Custom OpenAI endpoint, model and request overrides",
                        ],
                    ),
                    edit(
                        app_id,
                        name,
                        profile / "secrets.json",
                        "json",
                        secrets,
                        ["api_key_custom (active CometAPI credential)"],
                        contains_credentials=True,
                        private_sidecar=True,
                    ),
                ]
            )
        elif app_id == "comfyui":
            extension = root / "custom_nodes/cometapi_connect"
            assets = Path(__file__).parent / "assets"
            for source, filename in (
                ("comfy_nodes.py", "__init__.py"),
                ("comfy_cloud.py", "cometapi_cloud.py"),
            ):
                target = extension / filename
                changes.append(
                    Change(
                        app_id,
                        name,
                        target,
                        read_file(target),
                        (assets / source).read_bytes(),
                        ["Install CometAPI image/video node"],
                        [
                            "Restart ComfyUI to load CometAPI Image and CometAPI Video nodes. The API key is stored outside workflows and does not appear in exported graphs."
                        ],
                    )
                )
            changes.append(
                edit(
                    app_id,
                    name,
                    extension / "cometapi.json",
                    "json",
                    lambda doc: doc.update(api_key=key),
                    ["Private node credential"],
                    contains_credentials=True,
                    private_sidecar=True,
                )
            )
        elif app_id == "automatic1111":
            extension = root / "extensions/sd-webui-cometapi"
            template = Path(__file__).parent / "assets/sd_webui_cometapi.py"
            target = extension / "scripts/cometapi_media.py"
            changes.append(
                Change(
                    app_id,
                    name,
                    target,
                    read_file(target),
                    template.read_bytes(),
                    ["Install CometAPI image/video extension"],
                    [
                        "Adds a CometAPI cloud tab to AUTOMATIC1111. Restart WebUI to load the extension. Local txt2img/img2img pipelines keep their checkpoints."
                    ],
                )
            )
            changes.append(
                edit(
                    app_id,
                    name,
                    extension / "cometapi.json",
                    "json",
                    lambda doc: doc.update(api_key=key),
                    ["Extension's private CometAPI credential"],
                    contains_credentials=True,
                    private_sidecar=True,
                )
            )
    return changes
