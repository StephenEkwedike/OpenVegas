"""Contract checks for OpenClaw manifest route drift."""

from openvegas.agent.openclaw_skill import OPENVEGAS_SKILL_MANIFEST
from server.main import app


def test_manifest_paths_exist_in_api_routes():
    route_paths = set(app.openapi()["paths"])
    for action in OPENVEGAS_SKILL_MANIFEST["actions"]:
        path = action["path"].split("?", 1)[0]
        assert path in route_paths, f"Manifest path missing from API routes: {path}"
