"""Validate plugin.json and marketplace.json against the Copilot CLI plugin schema.

Schema reference: https://docs.github.com/en/copilot/reference/cli-plugin-reference
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]

PLUGIN_JSON_PATHS = [
    REPO_ROOT / "plugin.json",
    REPO_ROOT / ".claude-plugin" / "plugin.json",
]

MARKETPLACE_JSON_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"


# ── plugin.json ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PLUGIN_JSON_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
class TestPluginJson:
    def test_is_valid_json(self, path: Path):
        json.loads(path.read_text(encoding="utf-8"))

    def test_has_required_name(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "name" in data, "plugin.json must have a 'name' field"
        assert isinstance(data["name"], str) and data["name"], "name must be a non-empty string"

    def test_name_is_kebab_case(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["name"]
        assert name == name.lower(), "name must be lowercase"
        assert " " not in name, "name must use hyphens, not spaces"

    def test_has_description(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "description" in data, "plugin.json should have a 'description' field"

    def test_has_version(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "version" in data, "plugin.json should have a 'version' field"

    def test_skills_field_format(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "skills" in data:
            val = data["skills"]
            assert isinstance(val, (str, list)), "skills must be a string path or array of paths"

    def test_agents_field_format(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "agents" in data:
            val = data["agents"]
            assert isinstance(val, (str, list)), "agents must be a string path or array of paths"

    def test_author_is_object_if_present(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "author" in data:
            assert isinstance(data["author"], dict), "author must be an object with {name, email?}"
            assert "name" in data["author"], "author object must have a 'name' field"


# ── marketplace.json ─────────────────────────────────────────────────────


class TestMarketplaceJson:
    @pytest.fixture()
    def data(self) -> dict:
        return json.loads(MARKETPLACE_JSON_PATH.read_text(encoding="utf-8"))

    def test_is_valid_json(self):
        json.loads(MARKETPLACE_JSON_PATH.read_text(encoding="utf-8"))

    def test_has_required_name(self, data: dict):
        assert "name" in data, "marketplace.json must have a 'name' field"
        assert isinstance(data["name"], str) and data["name"]

    def test_name_is_kebab_case(self, data: dict):
        name = data["name"]
        assert name == name.lower() and " " not in name

    def test_name_max_length(self, data: dict):
        assert len(data["name"]) <= 64, "marketplace name must be ≤64 chars"

    def test_has_required_owner(self, data: dict):
        assert "owner" in data, "marketplace.json must have an 'owner' field"
        assert isinstance(data["owner"], dict), "owner must be an object, not a string"
        assert "name" in data["owner"], "owner must have a 'name' field"

    def test_has_required_plugins_array(self, data: dict):
        assert "plugins" in data, "marketplace.json must have a 'plugins' field"
        assert isinstance(data["plugins"], list), "plugins must be an array"
        assert len(data["plugins"]) > 0, "plugins array must not be empty"

    def test_each_plugin_has_required_fields(self, data: dict):
        for i, plugin in enumerate(data["plugins"]):
            assert "name" in plugin, f"plugin[{i}] must have a 'name' field"
            assert "source" in plugin, f"plugin[{i}] must have a 'source' field"

    def test_plugin_source_directories_exist(self, data: dict):
        for plugin in data["plugins"]:
            source = plugin["source"]
            resolved = (REPO_ROOT / source).resolve()
            assert resolved.exists(), f"plugin source '{source}' does not exist at {resolved}"

    def test_metadata_format_if_present(self, data: dict):
        if "metadata" in data:
            assert isinstance(data["metadata"], dict), "metadata must be an object"


# ── Cross-file consistency ───────────────────────────────────────────────


class TestCrossFileConsistency:
    def test_versions_match(self):
        root = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        claude = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE_JSON_PATH.read_text(encoding="utf-8"))

        versions = {
            "plugin.json": root.get("version"),
            ".claude-plugin/plugin.json": claude.get("version"),
            "marketplace plugin": marketplace["plugins"][0].get("version"),
        }
        unique = set(versions.values())
        assert len(unique) == 1, f"Version mismatch across manifests: {versions}"

    def test_names_match(self):
        root = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        claude = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE_JSON_PATH.read_text(encoding="utf-8"))

        plugin_names = {
            "plugin.json": root.get("name"),
            ".claude-plugin/plugin.json": claude.get("name"),
            "marketplace plugin": marketplace["plugins"][0].get("name"),
        }
        unique = set(plugin_names.values())
        assert len(unique) == 1, f"Plugin name mismatch across manifests: {plugin_names}"
