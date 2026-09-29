"""Isolated browser fixture: real local HTTP server, in-memory app operations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cometapi_helper.detection import catalog
from cometapi_helper.server import LocalServer


class FixtureEngine:
    status = "applied"

    def scan(self, roots=None):
        apps = catalog()
        for app in apps:
            app.update(detected=True, mode="automatic", evidence=["C:/fixture/settings.json"])
        return {"apps": apps, "defaults": {}, "version": "test", "platform": "win32"}

    def preview(self, key, apps, models):
        return {
            "plan_id": "fixture-plan",
            "warnings": [],
            "changes": [
                {
                    "app_name": "Claude Code",
                    "action": "update",
                    "path": "C:/fixture/settings.json",
                    "fields": ["api_key"],
                }
            ],
        }

    def history(self):
        return {
            "transactions": [
                {
                    "id": "fixture-transaction",
                    "apps": ["Claude Code"],
                    "created_at": "2026-09-21T12:00:00Z",
                    "status": self.status,
                }
            ]
        }

    def apply(self, plan_id):
        assert plan_id == "fixture-plan"
        self.status = "applied"
        return {
            "message": "Configuration saved. Restart the selected apps and follow any app-specific notes."
        }

    def restore(self, transaction_id):
        assert transaction_id == "fixture-transaction"
        self.status = "restored"
        return {"message": "Original configuration files restored. Restart the affected apps."}


with LocalServer(FixtureEngine()) as server:
    print(server.launch_url, flush=True)
    server.serve_forever()
