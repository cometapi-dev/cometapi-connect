# Architecture

CometAPI Connect is a local application configurator with a browser interface and an optional command line. Standalone packages include their Python runtime.

## Application flow

`cometapi_helper/cli.py` creates the application context and engine. With no subcommand it starts the loopback HTTP server in `server.py`; `static/index.html` provides the interface. Command-line operations use the same engine.

`detection.py` locates supported installations without modifying them. `adapters.py` coordinates application-specific adapters; individual client modules validate configuration schemas and interact with supported native stores. The catalogs provide integration descriptions and setup guidance.

`engine.py` coordinates preview, apply, history, and restore. `storage.py` manages file writes and backups, while credential modules handle platform stores. Configuration changes preserve unrelated values and reject stale inputs between preview and apply. Native credential changes have their own conflict and rollback checks. New adapters must use these transaction boundaries.

The local state directory remains `~/.cometapi-helper/` for compatibility with existing backups. Do not change its layout or names without a migration and recovery plan.

## Bundled integrations

`cometapi_helper/assets/` contains code installed into supported hosts, including JavaScript bridges and a bundled LM Studio plugin. Third-party attribution and redistribution details are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The canonical media transport lives in `integrations/sd-webui-cometapi/scripts/cometapi_media.py`. `scripts/sync_media_assets.py` derives its AUTOMATIC1111 and ComfyUI assets, and Fooocus adapts the generated host integration. Generated copies are checked for drift in CI.

## Validation and packaging

`tests/` contains isolated regression tests, including configuration preservation, restoration, native-store fixtures, and browser localization flows. Live application acceptance is separate from fixture coverage.

`scripts/build_release.py` creates platform artifacts and invokes bundled resource and configuration smoke checks. `packaging/` contains application icons; `distribution/` documents signing, release verification, and optional download routing.

Keep adapter, storage, engine, and UI responsibilities separate. Prefer focused changes that preserve existing recovery behavior.
