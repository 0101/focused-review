"""Tests for the ``validate-action`` subcommand + disregard run state (Phase 6).

Covers the canvas action-bar round-trip:

* ``validate_action`` — the pure validate/expand contract: a posted
  ``{run_id, ids[], action, instructions}`` is accepted only when the ``run_id``
  matches the rendered run and every id resolves *by prefix* — a finding id
  (``r#``) to file/line/title/suggestion, a rule-quality note id (``RQ#``) to its
  rule file + suggested change + the record_ids its fix invalidates. A forged
  ``run_id``, an unknown id, or an id matching neither prefix is rejected with the
  same structured-error shape as ``validate_records``.
* ``validate_action_command`` — the CLI handler (stdout on success, structured
  errors to stderr + exit 1 on failure, ``--apply-disregard`` persistence).
* run-state helpers ``persist_disregard`` / ``load_run_state``.
* disregard **persists across a re-render**: after a disregard is applied,
  ``render-review`` re-bakes the ``dimmed`` class on every subsequent render.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test via its hyphenated filename.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _envelope() -> dict:
    """A fully valid envelope: two Confirmed, one Questionable, one Invalid.

    Schema-valid so it round-trips through ``render-review`` for the persistence
    test; the Invalid finding (``r4``) confirms ``validate_action`` resolves *any*
    present record_id regardless of verdict (the canvas action targets stable ids).
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": "20260617-120000",
            "scope": "branch",
            "date": "2026-06-17T12:00:00Z",
            "rule_count": 4,
            "concern_count": 2,
            "consolidated_count": 4,
            "confirmed": 2,
            "questionable": 1,
            "invalid": 1,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [],
        "findings": [
            {
                "record_id": "r1",
                "assessment_id": "A-01",
                "display_number": 1,
                "display_bucket": "confirmed",
                "title": "Null deref in request handler",
                "file": "src/a.py",
                "line": 10,
                "original_severity": "High",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "introduced_by": "diff",
                "description": "Dereferences req.user without a null check.",
                "assessment": "Confirmed at the call site.",
                "suggestion": "Guard req.user before access.",
                "provenance": ["concern--bugs--opus"],
                "has_detail": False,
            },
            {
                "record_id": "r2",
                "assessment_id": "A-02",
                "display_number": 2,
                "display_bucket": "confirmed",
                "title": "Duplicated parsing logic",
                "file": "src/b.py",
                "line": 20,
                "original_severity": "Medium",
                "severity": "Medium",
                "fix_complexity": "complex",
                "verdict": "Confirmed",
                "type": "rule",
                "introduced_by": "diff",
                "description": "Same parse block in three methods.",
                "assessment": "Real duplication.",
                "suggestion": "Extract a shared parse helper.",
                "provenance": ["rule--simplicity"],
                "has_detail": False,
            },
            {
                "record_id": "r3",
                "assessment_id": "A-05",
                "display_number": 1,
                "display_bucket": "needs-decision",
                "title": "Magic number in retry loop",
                "file": "src/c.py",
                "line": None,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": "Questionable",
                "type": "concern",
                "introduced_by": "diff",
                "description": "Retry count 5 is hardcoded.",
                "assessment": "",
                "suggestion": "",
                "provenance": ["concern--style--gemini"],
                "has_detail": False,
            },
            {
                "record_id": "r4",
                "assessment_id": "A-09",
                "display_number": None,
                "display_bucket": "hidden",
                "title": "Section divider comment",
                "file": "src/d.py",
                "line": 5,
                "original_severity": "Low",
                "severity": "Medium",
                "fix_complexity": "quickfix",
                "verdict": "Invalid",
                "type": "rule",
                "introduced_by": "diff",
                "description": "A // ---- divider.",
                "assessment": "Navigational aid.",
                "suggestion": "",
                "provenance": ["rule--no-comments"],
                "has_detail": False,
            },
        ],
    }


RUN_ID = "20260617-120000"


def _finding(record_id: str, title: str, provenance: list[str]) -> dict:
    """A minimal-but-resolvable finding for the RQ / invalidation fixtures."""
    return {
        "record_id": record_id,
        "assessment_id": f"A-{record_id}",
        "display_number": None,
        "display_bucket": "confirmed",
        "title": title,
        "file": f"src/{record_id}.py",
        "line": 1,
        "original_severity": "Low",
        "severity": "Low",
        "fix_complexity": "quickfix",
        "verdict": "Confirmed",
        "type": "rule",
        "introduced_by": "diff",
        "description": "",
        "assessment": "",
        "suggestion": f"fix {record_id}",
        "provenance": provenance,
        "has_detail": False,
    }


def _envelope_with_notes() -> dict:
    """Envelope wired for rule-quality (RQ) resolution + Decision-12 invalidation.

    The findings exercise the rule "a finding dies only when *all* of its rule
    sources are fixed, and never while a concern keeps it alive":

    * ``r1`` — single rule source ``rule--no-comments`` (← RQ1). Dies on RQ1.
    * ``r2`` — two rule sources ``rule--no-comments`` + ``rule--simplicity``
      (← RQ1 **and** RQ2). Dies only when *both* are applied in one call.
    * ``r3`` — ``rule--simplicity`` **plus** a ``concern--`` source: the concern
      is an independent justification, so no rule fix can invalidate it.

    Two notes resolve the RQ ids: RQ1 (``rule--no-comments``) and RQ2
    (``rule--simplicity``).
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": RUN_ID,
            "scope": "branch",
            "date": "2026-06-17T12:00:00Z",
            "rule_count": 2,
            "concern_count": 1,
            "consolidated_count": 3,
            "confirmed": 3,
            "questionable": 0,
            "invalid": 0,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [
            {
                "id": "RQ1",
                "rule": "Avoid explanatory comments",
                "rule_source": "rule--no-comments",
                "rule_file": "review/rules/no-comments.md",
                "observation": "Flagged a navigational divider as a smell.",
                "suggestion": "Exempt section dividers from the no-comments rule.",
            },
            {
                "id": "RQ2",
                "rule": "Prefer simple code",
                "rule_source": "rule--simplicity",
                "rule_file": "review/rules/simplicity.md",
                "observation": "Flagged a two-line helper as over-engineered.",
                "suggestion": "Only flag helpers used in a single call site.",
            },
        ],
        "findings": [
            _finding("r1", "Single rule source", ["rule--no-comments"]),
            _finding("r2", "Two rule sources", ["rule--no-comments", "rule--simplicity"]),
            _finding("r3", "Concern keeps it alive", ["rule--simplicity", "concern--bugs--opus"]),
        ],
    }


def _two_rule_renderable_envelope() -> dict:
    """A schema-valid (renderable) envelope where ``r2`` depends on TWO rules.

    Unlike ``_envelope_with_notes`` (whose findings carry no ``display_number`` —
    fine for the pure ``validate_action`` tests), this one is contiguous-numbered so
    it round-trips through ``render-review``. ``r1`` dies on RQ1 alone; ``r2`` has
    *both* rule sources (RQ1 + RQ2) and no concern source, so it dies only once BOTH
    rules are fixed — the multi-step-apply case in Finding C-11 / Decision 12.
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": RUN_ID,
            "scope": "branch",
            "date": "2026-06-17T12:00:00Z",
            "rule_count": 2,
            "concern_count": 0,
            "consolidated_count": 2,
            "confirmed": 2,
            "questionable": 0,
            "invalid": 0,
        },
        "rebuttal_overrides": [],
        "rule_quality_notes": [
            {
                "id": "RQ1",
                "rule": "Avoid explanatory comments",
                "rule_source": "rule--no-comments",
                "rule_file": "review/rules/no-comments.md",
                "observation": "Flagged a navigational divider as a smell.",
                "suggestion": "Exempt section dividers from the no-comments rule.",
            },
            {
                "id": "RQ2",
                "rule": "Prefer simple code",
                "rule_source": "rule--simplicity",
                "rule_file": "review/rules/simplicity.md",
                "observation": "Flagged a two-line helper as over-engineered.",
                "suggestion": "Only flag helpers used in a single call site.",
            },
        ],
        "findings": [
            {**_finding("r1", "Single rule source", ["rule--no-comments"]), "display_number": 1},
            {
                **_finding("r2", "Two rule sources", ["rule--no-comments", "rule--simplicity"]),
                "display_number": 2,
            },
        ],
    }


def _write_records(tmp_path: Path, env: dict | None = None) -> Path:
    records = tmp_path / "records.json"
    records.write_text(json.dumps(env if env is not None else _envelope()), encoding="utf-8")
    return records


def _action_args(records: Path, run_id: str, ids: str, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        records=str(records),
        run_id=run_id,
        ids=ids,
        action=None,
        instructions="",
        apply_disregard=False,
        apply_rule_fixes=False,
        run_dir=None,
        # Pin the rules dir so the action-time rule_file re-validation (C-12/C-13)
        # is deterministic regardless of the cwd's configured rules_dir; the note
        # fixtures live under "review/rules/", which is under this prefix.
        repo=".",
        rules_dir="review/",
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _render_args(records: Path, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        records=str(records),
        run_dir=None,
        repo=".",
        review_out=None,
        canvas_out=None,
        template=None,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


# ---------------------------------------------------------------------------
# validate_action — pure validate/expand (accept)
# ---------------------------------------------------------------------------


class TestValidateActionAccept:
    def test_valid_ids_resolve_to_file_line_title_suggestion(self) -> None:
        expanded, errors = fr.validate_action(
            _envelope(), RUN_ID, ["r1", "r3"],
            action="focused-review.fix", instructions="fix minimally",
        )
        assert errors == []
        assert expanded is not None
        assert expanded["valid"] is True
        assert expanded["action"] == "focused-review.fix"
        assert expanded["run_id"] == RUN_ID
        assert expanded["instructions"] == "fix minimally"
        assert expanded["record_count"] == 2
        # A findings-only action resolves no rules.
        assert expanded["rule_count"] == 0
        assert expanded["rules"] == []

        by_id = {f["record_id"]: f for f in expanded["findings"]}
        # Order is preserved and every targeted id resolved.
        assert [f["record_id"] for f in expanded["findings"]] == ["r1", "r3"]
        # r1 resolves to its file/line/title/suggestion (the spec's contract).
        assert by_id["r1"]["file"] == "src/a.py"
        assert by_id["r1"]["line"] == 10
        assert by_id["r1"]["title"] == "Null deref in request handler"
        assert by_id["r1"]["suggestion"] == "Guard req.user before access."
        assert by_id["r1"]["severity"] == "High"
        assert by_id["r1"]["verdict"] == "Confirmed"
        # r3 has a null line and empty suggestion — both pass through faithfully.
        assert by_id["r3"]["line"] is None
        assert by_id["r3"]["suggestion"] == ""

    def test_resolves_invalid_verdict_finding(self) -> None:
        # The canvas targets stable ids; an Invalid finding still resolves if posted.
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, ["r4"])
        assert errors == []
        assert expanded["findings"][0]["record_id"] == "r4"
        assert expanded["findings"][0]["verdict"] == "Invalid"

    def test_default_action_and_instructions(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, ["r2"])
        assert errors == []
        assert expanded["action"] is None
        assert expanded["instructions"] == ""

    def test_does_not_mutate_input(self) -> None:
        env = _envelope()
        snapshot = copy.deepcopy(env)
        fr.validate_action(env, RUN_ID, ["r1", "r2"], action="focused-review.disregard")
        assert env == snapshot

    def test_all_valid_action_verbs_accepted(self) -> None:
        # Every verb the canvas action bar emits is on the allowlist and resolves.
        assert fr.VALID_ACTIONS == (
            "focused-review.fix",
            "focused-review.disregard",
            "focused-review.document",
        )
        for verb in fr.VALID_ACTIONS:
            expanded, errors = fr.validate_action(_envelope(), RUN_ID, ["r1"], action=verb)
            assert errors == [], verb
            assert expanded is not None and expanded["action"] == verb


# ---------------------------------------------------------------------------
# validate_action — rule-quality (RQ#) resolution + Decision-12 invalidation
# ---------------------------------------------------------------------------


class TestValidateActionRuleResolution:
    """``RQ#`` ids resolve to rule fixes, with Decision-12 invalidation attribution."""

    def test_single_rule_resolves_to_rule_file_and_invalidation(self) -> None:
        expanded, errors = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["RQ1"], action="focused-review.fix"
        )
        assert errors == []
        assert expanded is not None
        # No findings posted -> findings[] empty; the RQ resolves into rules[].
        assert expanded["record_count"] == 0
        assert expanded["findings"] == []
        assert expanded["rule_count"] == 1
        rule = expanded["rules"][0]
        assert rule["rule_id"] == "RQ1"
        assert rule["rule_source"] == "rule--no-comments"
        assert rule["rule_file"] == "review/rules/no-comments.md"
        assert rule["suggestion"] == "Exempt section dividers from the no-comments rule."
        assert rule["observation"] == "Flagged a navigational divider as a smell."
        # RQ1 alone kills r1 (its only rule source) but NOT r2 (also needs RQ2).
        assert rule["invalidated_record_ids"] == ["r1"]

    def test_multi_rule_finding_dies_only_when_all_its_rules_applied(self) -> None:
        # Posting BOTH RQ1 and RQ2 in one call: r1 (← RQ1) and r2 (← RQ1+RQ2) both
        # die; the two-rule finding r2 is attributed under EVERY rule it depends on,
        # which is exactly the shape persist_rule_fixes consumes.
        expanded, errors = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["RQ1", "RQ2"], action="focused-review.fix"
        )
        assert errors == []
        assert expanded["rule_count"] == 2
        by_rule = {r["rule_id"]: r for r in expanded["rules"]}
        assert by_rule["RQ1"]["invalidated_record_ids"] == ["r1", "r2"]
        assert by_rule["RQ2"]["invalidated_record_ids"] == ["r2"]

    def test_rule_kept_alive_by_concern_never_invalidates(self) -> None:
        # r3 carries rule--simplicity AND a concern source; the concern is an
        # independent justification, so applying RQ2 invalidates nothing.
        expanded, errors = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["RQ2"], action="focused-review.fix"
        )
        assert errors == []
        assert expanded["rules"][0]["rule_id"] == "RQ2"
        assert expanded["rules"][0]["invalidated_record_ids"] == []

    def test_mixed_ids_resolve_into_findings_and_rules(self) -> None:
        # A heterogeneous selection resolves r# into findings[] and RQ# into rules[]
        # in one expansion, each by prefix, order preserved within each list.
        expanded, errors = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["r1", "RQ2"], action="focused-review.fix"
        )
        assert errors == []
        assert [f["record_id"] for f in expanded["findings"]] == ["r1"]
        assert expanded["record_count"] == 1
        assert [r["rule_id"] for r in expanded["rules"]] == ["RQ2"]
        assert expanded["rule_count"] == 1


# ---------------------------------------------------------------------------
# validate_action — rule_file trust boundary re-validation (C-12 / C-13)
# ---------------------------------------------------------------------------


class TestValidateActionRuleFileTrustBoundary:
    """``validate_action`` re-validates each resolved note's ``rule_file``.

    The action round-trip loads records.json via ``_load_records_only``, which
    deliberately skips schema validation — so the path-safety and rule_source
    consistency checks ``render-review`` applied are NOT in force at the point the
    agent consumes ``rule_file`` to *edit* it. These tests pin the defense-in-depth
    re-validation that closes the TOCTOU / never-rendered-records.json gap.
    """

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",                  # absolute (POSIX)
            "C:/secrets.md",                # absolute (Windows drive)
            "review/../escape.md",          # traversal
            "outside/no-comments.md",       # outside the rules dir
            "review/rules/no-comments.txt", # not a .md file
        ],
    )
    def test_unsafe_rule_file_rejected_at_action_time(self, bad_path: str) -> None:
        env = copy.deepcopy(_envelope_with_notes())
        env["rule_quality_notes"][0]["rule_file"] = bad_path
        expanded, errors = fr.validate_action(
            env, RUN_ID, ["RQ1"], action="focused-review.fix", rules_dir="review/"
        )
        assert expanded is None
        assert any(
            e["path"] == "rules[0].rule_file" and e["field"] == "rule_file"
            for e in errors
        )

    def test_rule_file_source_mismatch_rejected_at_action_time(self) -> None:
        # Safe path, but its stem names a DIFFERENT rule than rule_source (C-12): the
        # fix would edit simplicity.md while the no-comments findings are invalidated.
        env = copy.deepcopy(_envelope_with_notes())
        env["rule_quality_notes"][0]["rule_file"] = "review/rules/simplicity.md"
        expanded, errors = fr.validate_action(
            env, RUN_ID, ["RQ1"], action="focused-review.fix", rules_dir="review/"
        )
        assert expanded is None
        assert any(
            e["path"] == "rules[0].rule_file"
            and "does not match rule_source" in e["message"]
            for e in errors
        )

    def test_safe_matching_rule_file_accepted(self) -> None:
        # The unchanged fixture (safe path under review/, stem matches rule_source)
        # still resolves cleanly — the re-validation adds no false positives.
        expanded, errors = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["RQ1"],
            action="focused-review.fix", rules_dir="review/",
        )
        assert errors == []
        assert expanded is not None and expanded["rule_count"] == 1

    def test_defaults_to_review_prefix_when_rules_dir_omitted(self) -> None:
        # With no rules_dir passed, the check falls back to the secure-by-default
        # "review/" prefix (mirroring validate_records): a note outside review/ is
        # rejected, while the under-review/ fixture is accepted.
        env = copy.deepcopy(_envelope_with_notes())
        env["rule_quality_notes"][0]["rule_file"] = "outside/no-comments.md"
        expanded, errors = fr.validate_action(
            env, RUN_ID, ["RQ1"], action="focused-review.fix"
        )
        assert expanded is None
        assert any(e["path"] == "rules[0].rule_file" for e in errors)

        ok, errors2 = fr.validate_action(
            _envelope_with_notes(), RUN_ID, ["RQ1"], action="focused-review.fix"
        )
        assert errors2 == []
        assert ok is not None

    def test_unsafe_rule_file_attributes_error_to_its_note(self) -> None:
        # Two notes posted; only the second carries an unsafe rule_file. The error is
        # attributed to that note's index/id so the orchestrator can pinpoint it.
        env = copy.deepcopy(_envelope_with_notes())
        env["rule_quality_notes"][1]["rule_file"] = "review/../escape.md"
        expanded, errors = fr.validate_action(
            env, RUN_ID, ["RQ1", "RQ2"], action="focused-review.fix", rules_dir="review/"
        )
        assert expanded is None
        offending = [e for e in errors if e["field"] == "rule_file"]
        assert len(offending) == 1
        assert offending[0]["path"] == "rules[1].rule_file"
        assert offending[0]["record_id"] == "RQ2"


# ---------------------------------------------------------------------------
# validate_action — pure validate/expand (reject)
# ---------------------------------------------------------------------------


class TestValidateActionReject:
    def test_forged_run_id_rejected(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), "FORGED-RUN", ["r1"])
        assert expanded is None
        assert len(errors) == 1
        err = errors[0]
        assert err["scope"] == "action"
        assert err["field"] == "run_id"
        assert "run_id mismatch" in err["message"]

    def test_unknown_record_id_rejected(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, ["r1", "r999"])
        assert expanded is None
        # Only the unknown id errors; the known one does not produce an error.
        assert len(errors) == 1
        err = errors[0]
        assert err["scope"] == "action"
        assert err["field"] == "record_id"
        assert err["record_id"] == "r999"
        assert err["path"] == "ids[1]"
        assert "unknown record_id" in err["message"]

    def test_forged_run_id_and_unknown_id_both_reported(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), "FORGED", ["r999"])
        assert expanded is None
        fields = {e["field"] for e in errors}
        assert fields == {"run_id", "record_id"}

    def test_empty_ids_rejected(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, [])
        assert expanded is None
        assert any(e["field"] == "ids" for e in errors)

    def test_blank_posted_run_id_rejected(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), "   ", ["r1"])
        assert expanded is None
        assert any(e["field"] == "run_id" for e in errors)

    def test_records_without_run_id_rejected(self) -> None:
        env = _envelope()
        del env["run"]["run_id"]
        expanded, errors = fr.validate_action(env, RUN_ID, ["r1"])
        assert expanded is None
        assert any("no run.run_id" in e["message"] for e in errors)

    def test_non_dict_data_rejected(self) -> None:
        expanded, errors = fr.validate_action([1, 2, 3], RUN_ID, ["r1"])
        assert expanded is None
        assert errors[0]["scope"] == "action"
        assert "must be a JSON object" in errors[0]["message"]

    def test_blank_id_token_rejected(self) -> None:
        # A non-empty list carrying a blank token is reported per-id, not resolved.
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, [""])
        assert expanded is None
        assert any(e["field"] == "id" for e in errors)

    def test_unknown_action_verb_rejected(self) -> None:
        # An arbitrary/forged verb is fail-closed: rejected, never echoed back.
        expanded, errors = fr.validate_action(
            _envelope(), RUN_ID, ["r1"], action="focused-review.exec"
        )
        assert expanded is None
        action_errors = [e for e in errors if e["field"] == "action"]
        assert len(action_errors) == 1
        assert action_errors[0]["scope"] == "action"
        assert "unknown action" in action_errors[0]["message"]
        assert "'focused-review.exec'" in action_errors[0]["message"]

    def test_unknown_action_reported_alongside_run_and_record_errors(self) -> None:
        # The verb check aggregates with the run_id / record_id checks rather than
        # masking them, so a fully-bogus action surfaces every problem at once.
        expanded, errors = fr.validate_action(
            _envelope(), "FORGED", ["r999"], action="bogus"
        )
        assert expanded is None
        assert {"action", "run_id", "record_id"} <= {e["field"] for e in errors}

    def test_none_action_still_accepted_pure_resolve(self) -> None:
        # None means "resolve only" (no verb posted) and stays valid — the verb
        # allowlist only rejects a *provided* unknown verb.
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, ["r1"], action=None)
        assert errors == []
        assert expanded is not None and expanded["action"] is None

    def test_unknown_rule_quality_note_id_rejected(self) -> None:
        # A well-formed RQ id absent from the envelope is rejected — RQ ids are
        # resolved in Python (the trust boundary), never trusted from the payload.
        expanded, errors = fr.validate_action(_envelope_with_notes(), RUN_ID, ["RQ9"])
        assert expanded is None
        assert len(errors) == 1
        err = errors[0]
        assert err["scope"] == "action"
        assert err["field"] == "rule_id"
        assert err["record_id"] == "RQ9"
        assert err["path"] == "ids[0]"
        assert "unknown rule-quality note id" in err["message"]

    def test_unrecognized_id_prefix_rejected(self) -> None:
        # An id matching neither the finding (r#) nor the rule-quality (RQ#) prefix
        # fails closed: lowercase rq, capital R, junk, and bare digits all reject.
        for bogus in ("rq2", "R3", "x5", "12"):
            expanded, errors = fr.validate_action(_envelope_with_notes(), RUN_ID, [bogus])
            assert expanded is None, bogus
            assert len(errors) == 1, bogus
            err = errors[0]
            assert err["field"] == "id", bogus
            assert err["record_id"] == bogus, bogus
            assert "unrecognized id" in err["message"], bogus


# ---------------------------------------------------------------------------
# _split_ids
# ---------------------------------------------------------------------------


class TestSplitIds:
    def test_splits_strips_and_dedupes_preserving_order(self) -> None:
        # Findings (r#) and rule-quality (RQ#) ids share one comma list; the id
        # *type* is disambiguated later by prefix in validate_action.
        assert fr._split_ids(" r1, RQ2 ,r1,, r3 ") == ["r1", "RQ2", "r3"]

    def test_empty_and_none(self) -> None:
        assert fr._split_ids("") == []
        assert fr._split_ids(None) == []
        assert fr._split_ids("  ,  ,") == []


# ---------------------------------------------------------------------------
# validate_action_command — CLI handler
# ---------------------------------------------------------------------------


class TestValidateActionCommand:
    def test_valid_prints_resolved_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        fr.validate_action_command(
            _action_args(records, RUN_ID, "r1,r2", action="focused-review.fix")
        )
        out = json.loads(capsys.readouterr().out)
        assert out["valid"] is True
        assert out["record_count"] == 2
        assert out["rule_count"] == 0
        assert out["records_path"] == str(records)
        assert {f["record_id"] for f in out["findings"]} == {"r1", "r2"}
        # No disregard side effect without --apply-disregard.
        assert "disregarded" not in out
        assert not (tmp_path / "run-state.json").exists()

    def test_forged_run_id_exits_1_with_errors_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(_action_args(records, "FORGED", "r1"))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""  # nothing on stdout for the failure path
        payload = json.loads(captured.err)
        assert payload["valid"] is False
        assert payload["run_id"] == "FORGED"
        assert any(e["field"] == "run_id" for e in payload["errors"])

    def test_unknown_id_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(_action_args(records, RUN_ID, "r1,ghost"))
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False
        assert any(e.get("record_id") == "ghost" for e in payload["errors"])

    def test_missing_file_exits_1_with_envelope_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(_action_args(tmp_path / "nope.json", RUN_ID, "r1"))
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False
        assert payload["errors"][0]["scope"] == "envelope"
        assert "not found" in payload["errors"][0]["message"]

    def test_malformed_json_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = tmp_path / "records.json"
        records.write_text("{ not valid json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(_action_args(records, RUN_ID, "r1"))
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert "not valid JSON" in payload["errors"][0]["message"]

    def test_apply_disregard_persists_and_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "r1",
                action="focused-review.disregard",
                apply_disregard=True,
                run_dir=str(tmp_path),
            )
        )
        out = json.loads(capsys.readouterr().out)
        assert out["disregarded"] == ["r1"]
        state = json.loads((tmp_path / "run-state.json").read_text(encoding="utf-8"))
        assert state["run_id"] == RUN_ID
        assert state["disregarded"] == ["r1"]

    def test_apply_disregard_does_not_persist_on_forged_run_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, "FORGED", "r1",
                    action="focused-review.disregard",
                    apply_disregard=True,
                    run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        # A rejected (forged) action must never write run state.
        assert not (tmp_path / "run-state.json").exists()

    def test_unknown_action_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(records, RUN_ID, "r1", action="focused-review.exec")
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""  # nothing on stdout for the failure path
        payload = json.loads(captured.err)
        assert payload["valid"] is False
        assert any(e["field"] == "action" for e in payload["errors"])

    def test_apply_disregard_with_non_disregard_action_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --apply-disregard is bound to the disregard verb: pairing it with fix
        # is rejected and writes NO run state (the persisted side effect is gated).
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, RUN_ID, "r1",
                    action="focused-review.fix",
                    apply_disregard=True,
                    run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False
        assert any(
            e["field"] == "action" and "apply-disregard" in e["message"]
            for e in payload["errors"]
        )
        assert not (tmp_path / "run-state.json").exists()

    def test_apply_disregard_with_no_action_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Even with no --action at all (the argparse default), --apply-disregard
        # must not silently persist state — it is rejected outright.
        records = _write_records(tmp_path)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, RUN_ID, "r1",
                    apply_disregard=True,
                    run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert any(e["field"] == "action" for e in payload["errors"])
        assert not (tmp_path / "run-state.json").exists()

    def test_apply_rule_fixes_persists_and_reports(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The fix verb + --apply-rule-fixes persists the resolved rules' invalidated
        # record_ids. Posting BOTH RQ ids in one call is what kills the two-rule
        # finding r2 (Decision 12 — invalidation is computed against the full set).
        records = _write_records(tmp_path, _envelope_with_notes())
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "RQ1,RQ2",
                action="focused-review.fix",
                apply_rule_fixes=True,
                run_dir=str(tmp_path),
            )
        )
        out = json.loads(capsys.readouterr().out)
        applied = {f["rule_id"]: f["invalidated_record_ids"] for f in out["rule_fixes_applied"]}
        assert applied == {"RQ1": ["r1", "r2"], "RQ2": ["r2"]}
        state = json.loads((tmp_path / "run-state.json").read_text(encoding="utf-8"))
        assert state["run_id"] == RUN_ID
        assert {f["rule_id"] for f in state["rule_fixes_applied"]} == {"RQ1", "RQ2"}

    def test_apply_rule_fixes_does_not_persist_on_forged_run_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Symmetric with the disregard gate: a rejected (forged) fix action must
        # never write rule-fix run state — the side effect is gated behind a clean
        # validation, so a forged run_id leaves no run-state.json behind.
        records = _write_records(tmp_path, _envelope_with_notes())
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, "FORGED", "RQ1",
                    action="focused-review.fix",
                    apply_rule_fixes=True,
                    run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        assert not (tmp_path / "run-state.json").exists()

    def test_apply_rule_fixes_with_non_fix_action_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # --apply-rule-fixes is bound to the fix verb: pairing it with disregard is
        # rejected and writes NO run state (the persisted side effect is gated).
        records = _write_records(tmp_path, _envelope_with_notes())
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, RUN_ID, "RQ1",
                    action="focused-review.disregard",
                    apply_rule_fixes=True,
                    run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False
        assert any(
            e["field"] == "action" and "apply-rule-fixes" in e["message"]
            for e in payload["errors"]
        )
        assert not (tmp_path / "run-state.json").exists()

    def test_mixed_disregard_persists_only_finding_ids(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A disregard over a mixed selection persists only the resolved FINDING ids;
        # any RQ# in the mix is a rule fix, never written to the disregarded set.
        records = _write_records(tmp_path, _envelope_with_notes())
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "r1,RQ1",
                action="focused-review.disregard",
                apply_disregard=True,
                run_dir=str(tmp_path),
            )
        )
        out = json.loads(capsys.readouterr().out)
        assert out["disregarded"] == ["r1"]
        state = json.loads((tmp_path / "run-state.json").read_text(encoding="utf-8"))
        assert state["disregarded"] == ["r1"]
        # The disregard path writes no rule-fix state.
        assert state.get("rule_fixes_applied", []) == []

    def test_apply_rule_fixes_rejects_unsafe_rule_file_and_persists_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # End-to-end defense-in-depth (C-13): an unsafe rule_file in records.json
        # makes validate-action exit 1 and persist NO rule-fix state, even though
        # _load_records_only skips schema validation. This is the TOCTOU /
        # never-rendered-records.json path the action-time re-validation closes.
        env = copy.deepcopy(_envelope_with_notes())
        env["rule_quality_notes"][0]["rule_file"] = "review/../escape.md"
        records = _write_records(tmp_path, env)
        with pytest.raises(SystemExit) as exc:
            fr.validate_action_command(
                _action_args(
                    records, RUN_ID, "RQ1",
                    action="focused-review.fix",
                    apply_rule_fixes=True, run_dir=str(tmp_path),
                )
            )
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False
        assert any(e["field"] == "rule_file" for e in payload["errors"])
        # The persisted side effect is gated behind a clean validation.
        assert not (tmp_path / "run-state.json").exists()


class TestValidateActionParser:
    def test_parser_rejects_unknown_action_choice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse enforces choices= before the handler runs: an unknown verb is a
        # usage error (exit 2), the first line of defence for the verb allowlist.
        records = _write_records(tmp_path)
        argv = [
            "focused-review", "validate-action",
            "--records", str(records),
            "--run-id", RUN_ID,
            "--ids", "r1",
            "--action", "focused-review.exec",
        ]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as exc:
                fr.main()
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_parser_accepts_each_valid_action_choice(self, tmp_path: Path) -> None:
        # Each allowlisted verb parses and is dispatched to the handler verbatim.
        records = _write_records(tmp_path)
        for verb in fr.VALID_ACTIONS:
            captured: dict[str, object] = {}

            def spy(args: argparse.Namespace) -> None:
                captured["action"] = args.action

            argv = [
                "focused-review", "validate-action",
                "--records", str(records),
                "--run-id", RUN_ID,
                "--ids", "r1",
                "--action", verb,
            ]
            with patch("sys.argv", argv):
                with patch.object(fr, "validate_action_command", spy):
                    fr.main()
            assert captured["action"] == verb

    def test_parser_accepts_apply_rule_fixes(self, tmp_path: Path) -> None:
        # The --apply-rule-fixes store_true flag parses and reaches the handler
        # (the gate that binds it to the fix verb lives in the handler, not argparse).
        records = _write_records(tmp_path)
        captured: dict[str, object] = {}

        def spy(args: argparse.Namespace) -> None:
            captured["apply_rule_fixes"] = args.apply_rule_fixes
            captured["ids"] = args.ids

        argv = [
            "focused-review", "validate-action",
            "--records", str(records),
            "--run-id", RUN_ID,
            "--ids", "RQ1",
            "--action", "focused-review.fix",
            "--apply-rule-fixes",
        ]
        with patch("sys.argv", argv):
            with patch.object(fr, "validate_action_command", spy):
                fr.main()
        assert captured["apply_rule_fixes"] is True
        assert captured["ids"] == "RQ1"

    def test_parser_accepts_rules_dir_and_repo(self, tmp_path: Path) -> None:
        # The validate-action subparser gained --repo / --rules-dir (mirroring
        # render-review / validate-records) so the action-time rule_file trust
        # boundary is configurable and resolves from the same config as the rest.
        records = _write_records(tmp_path)
        captured: dict[str, object] = {}

        def spy(args: argparse.Namespace) -> None:
            captured["repo"] = args.repo
            captured["rules_dir"] = args.rules_dir

        argv = [
            "focused-review", "validate-action",
            "--records", str(records),
            "--run-id", RUN_ID,
            "--ids", "r1",
            "--repo", "some/repo",
            "--rules-dir", "custom-rules/",
        ]
        with patch("sys.argv", argv):
            with patch.object(fr, "validate_action_command", spy):
                fr.main()
        assert captured["repo"] == "some/repo"
        assert captured["rules_dir"] == "custom-rules/"


# ---------------------------------------------------------------------------
# run-state helpers
# ---------------------------------------------------------------------------


class TestRunState:
    def test_persist_disregard_merges_monotonic_dedup_order(self, tmp_path: Path) -> None:
        s1 = fr.persist_disregard(str(tmp_path), RUN_ID, ["r1"])
        assert s1["disregarded"] == ["r1"]
        # Re-applying r1 and adding r2 merges without duplication, preserving order.
        s2 = fr.persist_disregard(str(tmp_path), RUN_ID, ["r1", "r2"])
        assert s2["disregarded"] == ["r1", "r2"]
        s3 = fr.persist_disregard(str(tmp_path), RUN_ID, ["r3", "r2"])
        assert s3["disregarded"] == ["r1", "r2", "r3"]

    def test_load_run_state_absent_is_empty(self, tmp_path: Path) -> None:
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": [], "rule_fixes_applied": []}

    def test_load_run_state_malformed_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text("{ broken", encoding="utf-8")
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": [], "rule_fixes_applied": []}

    def test_load_run_state_non_dict_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text("[1, 2]", encoding="utf-8")
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": [], "rule_fixes_applied": []}

    def test_load_run_state_stale_run_id_ignored(self, tmp_path: Path) -> None:
        fr.persist_disregard(str(tmp_path), "OLD-RUN", ["r1"])
        # A different expected run_id => treat the state as stale (empty).
        assert fr.load_run_state(str(tmp_path), expected_run_id="NEW-RUN") == {
            "disregarded": [],
            "rule_fixes_applied": [],
        }
        # Matching run_id => the state is honoured.
        assert fr.load_run_state(str(tmp_path), expected_run_id="OLD-RUN")["disregarded"] == ["r1"]

    def test_load_run_state_missing_run_id_ignored_when_expected(self, tmp_path: Path) -> None:
        # A state file without a run_id must not be applied to a named run.
        (tmp_path / "run-state.json").write_text(
            json.dumps({"disregarded": ["r1"]}), encoding="utf-8"
        )
        assert fr.load_run_state(str(tmp_path), expected_run_id="RID") == {
            "disregarded": [],
            "rule_fixes_applied": [],
        }
        # ...but a raw read (no expected run_id) still surfaces it.
        assert fr.load_run_state(str(tmp_path))["disregarded"] == ["r1"]

    def test_load_run_state_drops_non_string_ids(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text(
            json.dumps({"run_id": RUN_ID, "disregarded": ["r1", 5, "", None, "r2"]}),
            encoding="utf-8",
        )
        assert fr.load_run_state(str(tmp_path))["disregarded"] == ["r1", "r2"]

    # -- rule_fixes_applied sibling key -------------------------------------

    def test_persist_rule_fixes_round_trips(self, tmp_path: Path) -> None:
        state = fr.persist_rule_fixes(
            str(tmp_path),
            RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--no-comments", "invalidated_record_ids": ["r4"]}],
        )
        assert state["rule_fixes_applied"] == [
            {"rule_id": "RQ1", "rule_source": "rule--no-comments", "invalidated_record_ids": ["r4"]}
        ]
        # Read back from disk, run_id-stamped.
        loaded = fr.load_run_state(str(tmp_path), expected_run_id=RUN_ID)
        assert loaded["rule_fixes_applied"] == state["rule_fixes_applied"]

    def test_persist_rule_fixes_merges_by_rule_id_add_only(self, tmp_path: Path) -> None:
        fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r1"]}],
        )
        # Re-applying RQ1 unions ids (no dup); a new rule RQ2 is appended in order.
        state = fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [
                {"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r1", "r2"]},
                {"rule_id": "RQ2", "rule_source": "rule--b", "invalidated_record_ids": ["r3"]},
            ],
        )
        assert state["rule_fixes_applied"] == [
            {"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r1", "r2"]},
            {"rule_id": "RQ2", "rule_source": "rule--b", "invalidated_record_ids": ["r3"]},
        ]

    def test_persist_rule_fixes_preserves_disregarded(self, tmp_path: Path) -> None:
        fr.persist_disregard(str(tmp_path), RUN_ID, ["r1"])
        state = fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r4"]}],
        )
        # The sibling disregarded set is untouched by the rule-fix write.
        assert state["disregarded"] == ["r1"]
        assert state["rule_fixes_applied"][0]["rule_id"] == "RQ1"

    def test_persist_disregard_preserves_rule_fixes(self, tmp_path: Path) -> None:
        fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r4"]}],
        )
        state = fr.persist_disregard(str(tmp_path), RUN_ID, ["r1"])
        # Writing a disregard must not wipe the recorded rule fixes.
        assert state["disregarded"] == ["r1"]
        assert state["rule_fixes_applied"] == [
            {"rule_id": "RQ1", "rule_source": "rule--a", "invalidated_record_ids": ["r4"]}
        ]

    def test_load_run_state_sanitizes_rule_fixes(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "disregarded": [],
                    "rule_fixes_applied": [
                        "junk",
                        {"rule_source": "rule--a", "invalidated_record_ids": ["r1"]},  # no rule_id
                        {"rule_id": "RQ1", "invalidated_record_ids": ["r1", 5, "", "r2"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert fr.load_run_state(str(tmp_path))["rule_fixes_applied"] == [
            {"rule_id": "RQ1", "rule_source": "", "invalidated_record_ids": ["r1", "r2"]}
        ]


# ---------------------------------------------------------------------------
# disregard persists across a re-render (validate-action -> render-review)
# ---------------------------------------------------------------------------


def _finding_class_id_pairs(canvas_html: str) -> list[tuple[str, str]]:
    import re

    # Match any .finding block once, returning (class-token-string, record-id)
    # pairs. Classifying by class tokens means an extra presentation class (e.g.
    # "costly" for a large-fix finding) doesn't hide a finding from the
    # dimmed/plain partition. Both _dimmed_ids and _plain_ids build on this so the
    # HTML shape is matched in exactly one place.
    return re.findall(
        r'<div class="(finding[^"]*)" data-record-id="([^"]+)"', canvas_html
    )


def _dimmed_ids(canvas_html: str) -> set[str]:
    return {
        rid
        for classes, rid in _finding_class_id_pairs(canvas_html)
        if "dimmed" in classes.split()
    }


def _plain_ids(canvas_html: str) -> set[str]:
    return {
        rid
        for classes, rid in _finding_class_id_pairs(canvas_html)
        if "dimmed" not in classes.split()
    }


def _applied_by_rule(run_dir: Path) -> dict[str, list[str]]:
    """Read run-state.json and return ``{rule_id: invalidated_record_ids}``."""
    state = json.loads((run_dir / "run-state.json").read_text(encoding="utf-8"))
    return {e["rule_id"]: e["invalidated_record_ids"] for e in state["rule_fixes_applied"]}


class TestDisregardPersistsAcrossRerender:
    def test_disregard_dims_on_render_and_persists_on_rerender(self, tmp_path: Path) -> None:
        records = _write_records(tmp_path)
        canvas = tmp_path / "canvas.html"

        # 1) Apply a disregard for r1 (writes run-state.json).
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "r1",
                action="focused-review.disregard",
                apply_disregard=True,
                run_dir=str(tmp_path),
            )
        )

        # 2) Render — r1 is dimmed, the others are not.
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html1 = canvas.read_text(encoding="utf-8")
        assert "r1" in _dimmed_ids(html1)
        assert "r2" not in _dimmed_ids(html1)
        assert "r2" in _plain_ids(html1)

        # 3) Re-render from the same records.json — the dim PERSISTS (read back from
        #    run-state.json), even though render-review re-builds the HTML from scratch.
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html2 = canvas.read_text(encoding="utf-8")
        assert "r1" in _dimmed_ids(html2)

        # 4) Disregard another finding; both now persist across the next render.
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "r3",
                action="focused-review.disregard",
                apply_disregard=True,
                run_dir=str(tmp_path),
            )
        )
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html3 = canvas.read_text(encoding="utf-8")
        assert {"r1", "r3"} <= _dimmed_ids(html3)

    def test_no_run_state_means_nothing_dimmed(self, tmp_path: Path) -> None:
        records = _write_records(tmp_path)
        canvas = tmp_path / "canvas.html"
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        assert _dimmed_ids(canvas.read_text(encoding="utf-8")) == set()

    def test_stale_run_state_is_ignored_on_render(self, tmp_path: Path) -> None:
        # run-state.json from a different run must not dim the current run's findings.
        records = _write_records(tmp_path)
        fr.persist_disregard(str(tmp_path), "SOME-OTHER-RUN", ["r1"])
        canvas = tmp_path / "canvas.html"
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        assert _dimmed_ids(canvas.read_text(encoding="utf-8")) == set()


# ---------------------------------------------------------------------------
# rule-fix invalidation persists across a re-render (run-state -> render-review)
# ---------------------------------------------------------------------------


class TestRuleFixInvalidationPersistsAcrossRerender:
    def test_applied_rule_fix_dims_with_reason_and_persists(self, tmp_path: Path) -> None:
        records = _write_records(tmp_path)
        canvas = tmp_path / "canvas.html"

        # 1) Record an applied rule fix that invalidates r1 (writes run-state.json).
        fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--simplicity", "invalidated_record_ids": ["r1"]}],
        )

        # 2) Render — r1 is dimmed AND carries the audit reason pill; r2 is plain.
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html1 = canvas.read_text(encoding="utf-8")
        assert "r1" in _dimmed_ids(html1)
        assert "r2" in _plain_ids(html1)
        assert '<span class="dim-reason">invalidated — rule RQ1 fixed</span>' in html1

        # 3) Re-render from the same records.json — the invalidation dim PERSISTS.
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html2 = canvas.read_text(encoding="utf-8")
        assert "r1" in _dimmed_ids(html2)
        assert '<span class="dim-reason">invalidated — rule RQ1 fixed</span>' in html2

    def test_disregard_and_rule_fix_coexist_on_render(self, tmp_path: Path) -> None:
        # The two run-state keys are independent: a disregard and a rule-fix
        # invalidation both dim their own rows in the same render.
        records = _write_records(tmp_path)
        canvas = tmp_path / "canvas.html"
        fr.persist_disregard(str(tmp_path), RUN_ID, ["r2"])
        fr.persist_rule_fixes(
            str(tmp_path), RUN_ID,
            [{"rule_id": "RQ1", "rule_source": "rule--simplicity", "invalidated_record_ids": ["r1"]}],
        )
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        html = canvas.read_text(encoding="utf-8")
        assert {"r1", "r2"} <= _dimmed_ids(html)
        # Only the rule-fix row gets an audit reason; the plain disregard does not.
        r2_block = html.split('data-record-id="r2"', 1)[1].split("</details>", 1)[0]
        assert "dim-reason" not in r2_block

    def test_stale_rule_fix_state_is_ignored_on_render(self, tmp_path: Path) -> None:
        records = _write_records(tmp_path)
        fr.persist_rule_fixes(
            str(tmp_path), "SOME-OTHER-RUN",
            [{"rule_id": "RQ1", "rule_source": "rule--simplicity", "invalidated_record_ids": ["r1"]}],
        )
        canvas = tmp_path / "canvas.html"
        fr.render_review(_render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas)))
        assert _dimmed_ids(canvas.read_text(encoding="utf-8")) == set()

    def test_multi_rule_finding_dies_across_separate_apply_actions(
        self, tmp_path: Path
    ) -> None:
        """Finding C-11: a two-rule finding fixed in SEPARATE applies still dies.

        The user fixes the rules one at a time — RQ1 in one action, RQ2 in a second.
        ``rule_fixes_applied`` is an add-only accumulator and each ``validate-action``
        call only sees its own posted batch, so the batch-local computation alone
        would leave ``r2`` (which needs BOTH rules) permanently visible — never a
        subset {RQ1,RQ2} in either single-rule call. The apply path re-derives
        invalidation against the ACCUMULATED union, so after both actions ``r2`` is
        persisted as invalidated and the re-render dims it with a reason naming both
        rules (Decision 12).
        """
        records = _write_records(tmp_path, _two_rule_renderable_envelope())
        canvas = tmp_path / "canvas.html"

        # Action 1 — fix RQ1 alone. r1 (single rule) dies; r2 still needs RQ2.
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "RQ1",
                action="focused-review.fix",
                apply_rule_fixes=True, run_dir=str(tmp_path),
            )
        )
        inv1 = _applied_by_rule(tmp_path)
        assert inv1["RQ1"] == ["r1"]
        assert all("r2" not in ids for ids in inv1.values())  # r2 NOT yet invalidated

        # Render after action 1 — r1 is dimmed, r2 stays plain (only one rule fixed).
        fr.render_review(
            _render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas), rules_dir="review/")
        )
        html1 = canvas.read_text(encoding="utf-8")
        assert "r1" in _dimmed_ids(html1)
        assert "r2" in _plain_ids(html1)

        # Action 2 — fix RQ2 alone. Now BOTH of r2's rules are applied → r2 dies.
        fr.validate_action_command(
            _action_args(
                records, RUN_ID, "RQ2",
                action="focused-review.fix",
                apply_rule_fixes=True, run_dir=str(tmp_path),
            )
        )
        inv2 = _applied_by_rule(tmp_path)
        # r2 is attributed to EVERY rule it depends on; r1 stays under RQ1 only.
        assert inv2["RQ1"] == ["r1", "r2"]
        assert inv2["RQ2"] == ["r2"]

        # Re-render — r2 is now dimmed with a reason naming BOTH fixed rules.
        fr.render_review(
            _render_args(records, run_dir=str(tmp_path), canvas_out=str(canvas), rules_dir="review/")
        )
        html2 = canvas.read_text(encoding="utf-8")
        assert {"r1", "r2"} <= _dimmed_ids(html2)
        assert '<span class="dim-reason">invalidated — rules RQ1, RQ2 fixed</span>' in html2
