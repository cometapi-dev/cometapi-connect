"""Executed in an identified, local Dify API container; request travels over stdin."""

import copy
import json
import sys

PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible"
BASE = "https://api.cometapi.com/v1"
ALLOWED = {
    "api_key",
    "endpoint_url",
    "endpoint_model_name",
    "display_name",
    "mode",
    "context_size",
    "max_tokens_to_sample",
    "agent_thought_support",
    "web_search_support",
    "compatibility_mode",
    "api_type",
    "token_param_name",
    "user_identity_support",
    "stream_include_usage",
    "function_calling_type",
    "stream_function_calling",
    "vision_support",
    "video_support",
    "audio_support",
    "document_support",
    "structured_output_support",
    "stream_mode_auth",
    "stream_mode_delimiter",
}


def configured(snapshot, key, decrypt, encrypt):
    result = copy.deepcopy(snapshot)
    for row in result["credentials"]:
        config = json.loads(row["encrypted_config"])
        if (
            not isinstance(config, dict)
            or set(config) - ALLOWED
            or config.get("mode", "chat") != "chat"
            or config.get("compatibility_mode", "strict") != "strict"
            or config.get("api_type", "chat_completions") != "chat_completions"
            or config.get("stream_mode_auth", "not_use") != "not_use"
            or config.get("web_search_support", "not_supported") != "not_supported"
        ):
            raise ValueError("unsupported-credential")
        unchanged = (
            config.get("endpoint_url") == BASE
            and config.get("api_key")
            and decrypt(config["api_key"]) == key
        )
        if not unchanged:
            config["api_key"] = encrypt(key)
            config["endpoint_url"] = BASE
            row["encrypted_config"] = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return result


def build_snapshot(tenant, account_id, plugin_id, credentials, models, settings):
    """Capture the exact native binding identity for conflict detection."""
    fields = [
        "id",
        "tenant_id",
        "provider_name",
        "model_name",
        "model_type",
        "credential_name",
        "encrypted_config",
    ]
    return {
        "schema": 1,
        "tenant": tenant,
        "account": account_id,
        "plugin": plugin_id,
        "credentials": [{k: str(getattr(c, k)) for k in fields} for c in credentials],
        "models": [
            {
                "id": m.id,
                "model": m.model_name,
                "type": str(m.model_type),
                "credential": m.credential_id,
                "valid": m.is_valid,
            }
            for m in models
        ],
        "settings": [
            {
                "id": s.id,
                "model": s.model_name,
                "type": str(s.model_type),
                "enabled": s.enabled,
                "load_balancing": s.load_balancing_enabled,
            }
            for s in settings
        ],
    }


def main(request):
    import os
    import re

    from app import app
    from core.helper import encrypter
    from core.helper.model_provider_cache import (
        ProviderCredentialsCache,
        ProviderCredentialsCacheType,
    )
    from core.plugin.plugin_service import PluginService
    from core.provider_manager import ProviderConfigurationCacheSource, ProviderManager
    from extensions.ext_database import db
    from models.account import Account, Tenant, TenantAccountJoin
    from models.provider import (
        LoadBalancingModelConfig,
        ProviderModel,
        ProviderModelCredential,
        ProviderModelSetting,
    )
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session

    if os.geteuid() == 0:
        raise ValueError("ordinary-user-required")
    operation = request.get("operation")
    if operation not in {"read", "prepare", "write"}:
        raise ValueError("invalid-operation")
    committed = False
    with app.app_context(), Session(db.engine) as session:
        try:
            # Block concurrent membership, credential and model-binding edits for
            # the short native transaction. No inference/network call under lock.
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            session.execute(
                text(
                    "LOCK TABLE tenants, accounts, tenant_account_joins, provider_models, "
                    "provider_model_credentials, provider_model_settings, load_balancing_model_configs "
                    "IN SHARE ROW EXCLUSIVE MODE"
                )
            )
            tenants = session.scalars(select(Tenant)).all()
            accounts = session.scalars(select(Account)).all()
            joins = session.scalars(select(TenantAccountJoin)).all()
            if (
                len(tenants) != 1
                or len(accounts) != 1
                or len(joins) != 1
                or joins[0].tenant_id != tenants[0].id
                or joins[0].account_id != accounts[0].id
                or str(joins[0].role) != "owner"
                or str(accounts[0].status) != "active"
            ):
                raise ValueError("personal-workspace-required")
            tenant = tenants[0].id
            bindings = [
                b
                for b in PluginService.list_model_provider_bindings(tenant)
                if b.plugin_id == "langgenius/openai_api_compatible"
            ]
            if (
                len(bindings) != 1
                or not re.fullmatch(
                    r"langgenius/openai_api_compatible:[^@]+@[0-9a-f]{64}",
                    bindings[0].plugin_unique_identifier,
                )
                or not bindings[0].verified
                or str(bindings[0].source) != "marketplace"
                or bindings[0].runtime_type != "local"
            ):
                raise ValueError("verified-plugin-required")
            credentials = session.scalars(
                select(ProviderModelCredential)
                .where(
                    ProviderModelCredential.tenant_id == tenant,
                    ProviderModelCredential.provider_name == PROVIDER,
                )
                .order_by(ProviderModelCredential.id)
            ).all()
            models = session.scalars(
                select(ProviderModel)
                .where(ProviderModel.tenant_id == tenant, ProviderModel.provider_name == PROVIDER)
                .order_by(ProviderModel.id)
            ).all()
            settings = session.scalars(
                select(ProviderModelSetting)
                .where(
                    ProviderModelSetting.tenant_id == tenant,
                    ProviderModelSetting.provider_name == PROVIDER,
                )
                .order_by(ProviderModelSetting.id)
            ).all()
            lb = session.scalar(
                select(LoadBalancingModelConfig.id).where(
                    LoadBalancingModelConfig.tenant_id == tenant,
                    LoadBalancingModelConfig.provider_name == PROVIDER,
                )
            )
            if (
                not 1 <= len(credentials) <= 100
                or not models
                or lb
                or any(str(c.model_type) != "llm" for c in credentials)
                or any(s.load_balancing_enabled for s in settings)
                or any(
                    not m.is_valid or m.credential_id not in {c.id for c in credentials}
                    for m in models
                )
            ):
                raise ValueError("existing-chat-models-required")
            snapshot = build_snapshot(
                tenant,
                accounts[0].id,
                bindings[0].plugin_unique_identifier,
                credentials,
                models,
                settings,
            )
            if operation == "read":
                return {"ok": True, "snapshot": snapshot}
            if request.get("expected") != snapshot:
                raise ValueError("conflict")
            if operation == "prepare":
                key = request.get("key", "")
                if not re.fullmatch(r"sk-[A-Za-z0-9_\-]{8,250}", key):
                    raise ValueError("invalid-key")
                desired = configured(
                    snapshot,
                    key,
                    lambda token: encrypter.decrypt_token(tenant, token),
                    lambda value: encrypter.encrypt_token(tenant, value),
                )
                return {"ok": True, "snapshot": desired}
            desired = request["desired"]
            masked = copy.deepcopy(desired)
            if len(masked.get("credentials", [])) != len(credentials):
                raise ValueError("invalid-write")
            for old, new in zip(snapshot["credentials"], masked["credentials"]):
                raw = new.get("encrypted_config")
                if (
                    not isinstance(raw, str)
                    or len(raw) > 65536
                    or not isinstance(json.loads(raw), dict)
                ):
                    raise ValueError("invalid-write")
                new["encrypted_config"] = old["encrypted_config"]
            if masked != snapshot:
                raise ValueError("invalid-write")
            for record, value in zip(credentials, desired["credentials"]):
                record.encrypted_config = value["encrypted_config"]
            session.commit()
            committed = True
            for model in snapshot["models"]:
                ProviderCredentialsCache(
                    tenant_id=tenant,
                    identity_id=model["id"],
                    cache_type=ProviderCredentialsCacheType.MODEL,
                ).delete()
            ProviderManager.invalidate_configurations_cache(
                tenant,
                sources=[
                    ProviderConfigurationCacheSource.PROVIDER_MODEL_CREDENTIALS,
                    ProviderConfigurationCacheSource.PROVIDER_MODELS,
                ],
            )
            return {"ok": True, "snapshot": desired}
        except Exception:
            session.rollback()
            if committed:
                return {"ok": False, "error": "commit-unconfirmed"}
            raise


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("request-too-large")
        result = main(json.loads(raw))
    except Exception as error:
        safe = {
            "conflict",
            "personal-workspace-required",
            "verified-plugin-required",
            "existing-chat-models-required",
            "unsupported-credential",
            "ordinary-user-required",
            "invalid-write",
        }
        result = {
            "ok": False,
            "error": str(error)
            if isinstance(error, ValueError) and str(error) in safe
            else "native-operation-failed",
        }
    print("COMETAPI_DIFY=" + json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
