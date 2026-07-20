"""Validate orchestration contracts encoded in the focused-review skill."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_PATH = REPO_ROOT / "skills" / "focused-review" / "SKILL.md"


def test_rule_dispatch_uses_namespaced_plugin_agent():
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert "`focused-review:review-runner` Task agent" in skill
    assert "launch a `review-runner` Task agent" not in skill
