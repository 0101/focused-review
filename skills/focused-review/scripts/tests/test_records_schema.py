"""Tests for the records.json envelope: semantic validation + display finalize.

The reporter emits a **semantic** envelope (verdicts, severities, provenance,
prose, and the canonical ``introduced_by`` / ``rule_sources`` labels). Python
then owns the whole **display layer**: ``finalize_records`` derives each
finding's ``display_bucket``, orders the findings, assigns the gap-free ``f#``
finding ids and ``rq#`` note ids, derives each note's ``rule`` label, and
computes the ``run.*`` tally counts. ``validate_finalized_records`` re-checks
those assigned invariants.

This module covers four contracts:

* ``validate_records`` — SEMANTIC validation (reporter output). Lenient about the
  display layer: it neither requires nor rejects ``record_id`` / ``display_bucket``
  / note ``id`` / ``run`` counts, so an already-enriched file re-validates clean.
* ``finalize_records`` — the deterministic, idempotent, non-mutating display
  assignment.
* ``validate_finalized_records`` — the post-finalize invariant self-check.
* ``load_and_validate_records`` + the ``validate-records`` CLI handler.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

# Import the module under test via its hyphenated filename.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
fr = importlib.import_module("focused-review")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _semantic_envelope() -> dict:
    """A minimal, fully valid SEMANTIC records.json envelope (reporter output).

    Carries only the fields the reporter emits — NO ``record_id`` /
    ``display_number`` / ``display_bucket`` on findings, NO ``id`` / ``rule`` on
    notes, and NO tally counts on ``run``. Exercises both provenance encodings
    (object form + bare string), a Confirmed finding with a detail sidecar, an
    Invalid finding with a null assessment_id / line, a rebuttal override keyed by
    ``assessment_id``, and a rule-quality note carrying a ``rule_sources`` list.
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": "20260101-000000",
            "scope": "branch",
            "date": "2026-01-01T00:00:00Z",
            "rule_count": 3,
            "concern_count": 2,
        },
        "rebuttal_overrides": [
            {
                "assessment_id": "A-01",
                "original_severity": "Medium",
                "severity": "High",
                "reasoning": "Reinstated after rebuttal — the concern was correct.",
            }
        ],
        "rule_quality_notes": [
            {
                "rule_sources": ["rule--no-foo"],
                "rule_file": "review/no-foo.md",
                "observation": "noisy on tests",
                "suggestion": "scope to src/",
            }
        ],
        "findings": [
            {
                "assessment_id": "A-01",
                "title": "Null deref in handler",
                "file": "src/app.py",
                "line": 42,
                "original_severity": "Medium",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
                "introduced_by": "diff",
                "description": "x may be None here.",
                "assessment": "Verified by reading the call site.",
                "suggestion": "Guard with an is-None check.",
                "provenance": [
                    {"source": "concern--bugs--opus", "original_severity": "Medium"}
                ],
                "has_detail": True,
            },
            {
                "assessment_id": None,
                "title": "Unused import",
                "file": "src/util.py",
                "line": None,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": "Invalid",
                "type": "rule",
                "introduced_by": "pre-existing",
                "description": "Imports os but never uses it.",
                "assessment": "Not introduced by the diff — out of scope.",
                "suggestion": "",
                "provenance": ["rule--no-unused-imports"],
                "has_detail": False,
            },
        ],
    }


def _finalized_envelope() -> dict:
    """The semantic fixture run through ``finalize_records`` (display-enriched)."""
    return fr.finalize_records(_semantic_envelope())


def _multi_bucket_semantic() -> dict:
    """A semantic envelope whose findings span all four buckets, scrambled.

    Used by the finalize/ordering tests: the input order is deliberately NOT the
    display order so the reorder + f#-assignment can be observed. Each finding's
    ``assessment_id`` doubles as a stable tag for asserting its assigned f# id.
    """
    def f(aid: str, verdict: str, file: str, line, introduced_by: str = "diff") -> dict:
        return {
            "assessment_id": aid,
            "title": f"finding {aid}",
            "file": file,
            "line": line,
            "original_severity": "High",
            "severity": "High",
            "fix_complexity": "quickfix",
            "verdict": verdict,
            "type": "rule",
            "introduced_by": introduced_by,
            "description": "",
            "assessment": "",
            "suggestion": "",
            "provenance": ["rule--x"],
            "has_detail": False,
        }

    env = _semantic_envelope()
    env["rebuttal_overrides"] = []
    env["rule_quality_notes"] = []
    env["findings"] = [
        f("A-inv", "Invalid", "z.py", 1),                                  # hidden
        f("A-cb", "Confirmed", "b.py", 10),                                # confirmed
        f("A-ca0", "Confirmed", "a.py", None),                             # confirmed, null line
        f("A-q", "Questionable", "a.py", 5),                               # needs-decision
        f("A-pre", "Confirmed", "a.py", 2, introduced_by="pre-existing"),  # pre-existing
        f("A-ca3", "Confirmed", "a.py", 3),                                # confirmed
    ]
    return env


def _paths(errors: list[dict]) -> set[str]:
    return {e["path"] for e in errors}


def _by_field(errors: list[dict], field: str) -> list[dict]:
    return [e for e in errors if e.get("field") == field]


def _id_to_record_id(finalized: dict) -> dict[str, str]:
    """Map each finding's stable assessment_id tag to its assigned record_id."""
    return {f["assessment_id"]: f["record_id"] for f in finalized["findings"]}


# ---------------------------------------------------------------------------
# _derive_display_bucket — scope normalization (the pure routing function)
# ---------------------------------------------------------------------------


class TestDeriveDisplayBucket:
    """Direct unit tests for the ``(verdict, introduced_by) → display_bucket`` map.

    The assessor emits a four-value ``introduced_by`` vocabulary — ``diff`` |
    ``pre-existing`` | ``reclassified-pre-existing`` | ``reclassified-diff``
    (``agents/review-assessor.agent.md``) — and finalize must route the two
    pre-existing spellings identically. Exact-string matching used to leak a
    ``reclassified-pre-existing`` Confirmed into the gating ``confirmed`` bucket
    (focused-review-7gj.11); these tests pin the normalized routing.
    """

    @pytest.mark.parametrize("introduced_by", ["pre-existing", "reclassified-pre-existing"])
    def test_pre_existing_spellings_route_confirmed_to_pre_existing(
        self, introduced_by: str
    ) -> None:
        assert fr._derive_display_bucket("Confirmed", introduced_by) == "pre-existing"

    @pytest.mark.parametrize("introduced_by", ["pre-existing", "reclassified-pre-existing"])
    def test_pre_existing_spellings_route_questionable_to_hidden(
        self, introduced_by: str
    ) -> None:
        assert fr._derive_display_bucket("Questionable", introduced_by) == "hidden"

    @pytest.mark.parametrize("introduced_by", ["diff", "reclassified-diff", ""])
    def test_in_scope_spellings_route_confirmed_to_confirmed(self, introduced_by: str) -> None:
        assert fr._derive_display_bucket("Confirmed", introduced_by) == "confirmed"

    @pytest.mark.parametrize("introduced_by", ["diff", "reclassified-diff", ""])
    def test_in_scope_spellings_route_questionable_to_needs_decision(
        self, introduced_by: str
    ) -> None:
        assert fr._derive_display_bucket("Questionable", introduced_by) == "needs-decision"

    @pytest.mark.parametrize(
        "introduced_by",
        ["diff", "pre-existing", "reclassified-pre-existing", "reclassified-diff"],
    )
    def test_invalid_is_always_hidden_regardless_of_scope(self, introduced_by: str) -> None:
        assert fr._derive_display_bucket("Invalid", introduced_by) == "hidden"

    @pytest.mark.parametrize("introduced_by", [None, 123, ["pre-existing"]])
    def test_absent_or_non_string_scope_is_in_scope(self, introduced_by: object) -> None:
        # Absent / non-string introduced_by is treated as in-scope and never raises.
        assert fr._derive_display_bucket("Confirmed", introduced_by) == "confirmed"

    def test_unknown_verdict_returns_none(self) -> None:
        # A bucket can't be derived from a bad verdict — the caller reports that error.
        assert fr._derive_display_bucket("Bogus", "diff") is None


# ---------------------------------------------------------------------------
# validate_records — Accept (semantic)
# ---------------------------------------------------------------------------


class TestValidateRecordsAccept:
    def test_valid_semantic_envelope_has_no_errors(self) -> None:
        assert fr.validate_records(_semantic_envelope()) == []

    def test_already_finalized_envelope_revalidates_clean(self) -> None:
        # Semantic validation is LENIENT about the display layer: an enriched file
        # (carrying record_id / display_bucket / note id / run counts) must still
        # pass, so SKILL Step 6d's re-render on an enriched records.json works.
        assert fr.validate_records(_finalized_envelope()) == []

    def test_empty_findings(self) -> None:
        env = _semantic_envelope()
        env["findings"] = []
        env["rebuttal_overrides"] = []
        assert fr.validate_records(env) == []

    def test_empty_optional_arrays(self) -> None:
        env = _semantic_envelope()
        env["rebuttal_overrides"] = []
        env["rule_quality_notes"] = []
        assert fr.validate_records(env) == []

    def test_nullable_fields_accept_null(self) -> None:
        env = _semantic_envelope()
        env["rebuttal_overrides"] = []  # the override references findings[0]'s A-01
        env["findings"][0]["line"] = None
        env["findings"][0]["assessment_id"] = None
        env["findings"][0]["has_detail"] = False  # null assessment_id => no detail
        assert fr.validate_records(env) == []

    def test_text_fields_may_be_empty_strings(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["description"] = ""
        env["findings"][0]["assessment"] = ""
        env["findings"][0]["suggestion"] = ""
        assert fr.validate_records(env) == []

    def test_introduced_by_optional(self) -> None:
        env = _semantic_envelope()
        del env["findings"][0]["introduced_by"]
        assert fr.validate_records(env) == []

    def test_introduced_by_empty_string_accepted(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["introduced_by"] = ""
        assert fr.validate_records(env) == []

    @pytest.mark.parametrize("severity", ["Critical", "High", "Medium", "Low"])
    def test_all_severities_accepted(self, severity: str) -> None:
        env = _semantic_envelope()
        env["findings"][0]["original_severity"] = severity
        env["findings"][0]["severity"] = severity
        assert fr.validate_records(env) == []

    @pytest.mark.parametrize("verdict", ["Confirmed", "Questionable", "Invalid"])
    def test_all_verdicts_accepted(self, verdict: str) -> None:
        env = _semantic_envelope()
        env["findings"][0]["verdict"] = verdict
        assert fr.validate_records(env) == []

    def test_run_count_fields_are_ignored_when_present(self) -> None:
        # The reporter must not emit run tallies, but if a stray (even wrong) value
        # slips in, semantic validation ignores it — finalize overwrites it. This
        # keeps validation lenient so a re-validated enriched file passes.
        env = _semantic_envelope()
        env["run"]["consolidated_count"] = 999
        env["run"]["confirmed"] = 999
        env["run"]["questionable"] = -5
        env["run"]["invalid"] = "lots"
        assert fr.validate_records(env) == []

    def test_display_layer_fields_are_ignored_when_present(self) -> None:
        # Stray record_id / display_bucket / note id values are neither required nor
        # rejected by semantic validation (finalize assigns the canonical ones).
        env = _semantic_envelope()
        env["findings"][0]["record_id"] = "whatever"
        env["findings"][0]["display_bucket"] = "bogus-bucket"
        env["rule_quality_notes"][0]["id"] = "NOT-RQ"
        assert fr.validate_records(env) == []


# ---------------------------------------------------------------------------
# validate_records — Reject (envelope level)
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectEnvelope:
    def test_root_not_object(self) -> None:
        errors = fr.validate_records(["not", "an", "object"])
        assert len(errors) == 1
        assert errors[0]["scope"] == "envelope"
        assert errors[0]["path"] == "$"

    def test_missing_schema_version(self) -> None:
        env = _semantic_envelope()
        del env["schema_version"]
        assert any(e["field"] == "schema_version" for e in fr.validate_records(env))

    def test_unsupported_schema_version(self) -> None:
        env = _semantic_envelope()
        env["schema_version"] = 999
        errors = fr.validate_records(env)
        assert any("unsupported schema_version" in e["message"] for e in errors)

    def test_schema_version_wrong_type(self) -> None:
        env = _semantic_envelope()
        env["schema_version"] = "1"
        assert any(e["field"] == "schema_version" for e in fr.validate_records(env))

    def test_missing_run(self) -> None:
        env = _semantic_envelope()
        del env["run"]
        assert any(e["field"] == "run" for e in fr.validate_records(env))

    def test_missing_findings(self) -> None:
        env = _semantic_envelope()
        del env["findings"]
        assert any(e["field"] == "findings" for e in fr.validate_records(env))

    def test_findings_not_array(self) -> None:
        env = _semantic_envelope()
        env["findings"] = {"r1": "x"}
        assert any(e["field"] == "findings" for e in fr.validate_records(env))

    def test_missing_rebuttal_overrides(self) -> None:
        env = _semantic_envelope()
        del env["rebuttal_overrides"]
        assert any(e["field"] == "rebuttal_overrides" for e in fr.validate_records(env))

    def test_missing_rule_quality_notes(self) -> None:
        env = _semantic_envelope()
        del env["rule_quality_notes"]
        assert any(e["field"] == "rule_quality_notes" for e in fr.validate_records(env))


# ---------------------------------------------------------------------------
# validate_records — Reject (run); run counts are NOT required (semantic)
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectRun:
    def test_missing_run_id(self) -> None:
        env = _semantic_envelope()
        del env["run"]["run_id"]
        assert any(e["path"] == "run.run_id" for e in fr.validate_records(env))

    def test_invalid_scope(self) -> None:
        env = _semantic_envelope()
        env["run"]["scope"] = "sideways"
        assert any(e["path"] == "run.scope" for e in fr.validate_records(env))

    def test_missing_rule_count(self) -> None:
        env = _semantic_envelope()
        del env["run"]["rule_count"]
        assert any(e["path"] == "run.rule_count" for e in fr.validate_records(env))

    def test_negative_concern_count(self) -> None:
        env = _semantic_envelope()
        env["run"]["concern_count"] = -1
        assert any(e["path"] == "run.concern_count" for e in fr.validate_records(env))

    def test_rule_count_wrong_type(self) -> None:
        env = _semantic_envelope()
        env["run"]["rule_count"] = "three"
        assert any(e["path"] == "run.rule_count" for e in fr.validate_records(env))

    def test_bool_is_not_a_valid_count(self) -> None:
        env = _semantic_envelope()
        env["run"]["rule_count"] = True
        assert any(e["path"] == "run.rule_count" for e in fr.validate_records(env))

    def test_tally_counts_not_required(self) -> None:
        # The reporter does not emit consolidated_count / confirmed / questionable /
        # invalid; their absence must NOT be an error (Python computes them).
        env = _semantic_envelope()  # already omits them
        run_errors = [e for e in fr.validate_records(env) if e["scope"] == "run"]
        assert run_errors == []


# ---------------------------------------------------------------------------
# validate_records — Reject (finding); semantic fields only
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectFinding:
    def test_finding_not_object(self) -> None:
        env = _semantic_envelope()
        env["findings"][0] = "nope"
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0]" for e in errors)

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("original_severity", "Spicy"),
            ("severity", "Nope"),
            ("fix_complexity", "instant"),
            ("verdict", "Maybe"),
            ("type", "other"),
        ],
    )
    def test_bad_enum_values(self, field: str, bad: str) -> None:
        env = _semantic_envelope()
        env["findings"][0][field] = bad
        assert any(e["path"] == f"findings[0].{field}" for e in fr.validate_records(env))

    def test_missing_title(self) -> None:
        env = _semantic_envelope()
        del env["findings"][0]["title"]
        assert any(e["path"] == "findings[0].title" for e in fr.validate_records(env))

    def test_missing_file(self) -> None:
        env = _semantic_envelope()
        del env["findings"][0]["file"]
        assert any(e["path"] == "findings[0].file" for e in fr.validate_records(env))

    def test_description_must_be_string(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["description"] = 123
        assert any(e["path"] == "findings[0].description" for e in fr.validate_records(env))

    def test_line_negative(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["line"] = -3
        assert any(e["path"] == "findings[0].line" for e in fr.validate_records(env))

    def test_line_wrong_type(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["line"] = "42"
        assert any(e["path"] == "findings[0].line" for e in fr.validate_records(env))

    def test_has_detail_must_be_bool(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["has_detail"] = "yes"
        assert any(e["path"] == "findings[0].has_detail" for e in fr.validate_records(env))

    def test_has_detail_true_requires_assessment_id(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["has_detail"] = True
        env["findings"][0]["assessment_id"] = None
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[0].assessment_id" and "has_detail" in e["message"]
            for e in errors
        )

    def test_assessment_id_empty_string_rejected(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["assessment_id"] = "   "
        assert any(e["path"] == "findings[0].assessment_id" for e in fr.validate_records(env))

    def test_duplicate_assessment_id(self) -> None:
        env = _semantic_envelope()
        env["findings"][1]["assessment_id"] = "A-01"  # collides with findings[0]
        env["findings"][1]["has_detail"] = False
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[1].assessment_id" and "duplicate" in e["message"]
            for e in errors
        )

    def test_record_id_and_display_bucket_not_required(self) -> None:
        # The semantic fixture omits record_id / display_bucket / display_number on
        # every finding, and that must produce NO finding-scope errors (finalize
        # assigns them). Guards against re-introducing a display-layer requirement.
        env = _semantic_envelope()
        finding_errors = [e for e in fr.validate_records(env) if e["scope"] == "finding"]
        assert finding_errors == []

    def test_error_carries_assessment_id_identity(self) -> None:
        # A finding error attributes back to the reporter's stable handle
        # (assessment_id, the A-XX id it knows) — record_id is not yet assigned.
        env = _semantic_envelope()
        env["findings"][0]["severity"] = "Bogus"
        err = next(e for e in fr.validate_records(env) if e["path"] == "findings[0].severity")
        assert err["assessment_id"] == "A-01"
        assert err["record_id"] is None


# ---------------------------------------------------------------------------
# validate_records — Reject (provenance)
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectProvenance:
    def test_provenance_must_be_present(self) -> None:
        env = _semantic_envelope()
        del env["findings"][0]["provenance"]
        assert any(e["path"] == "findings[0].provenance" for e in fr.validate_records(env))

    def test_provenance_must_be_nonempty(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["provenance"] = []
        assert any(e["path"] == "findings[0].provenance" for e in fr.validate_records(env))

    def test_provenance_object_requires_source(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["provenance"] = [{"original_severity": "High"}]
        assert any(e["path"].startswith("findings[0].provenance") for e in fr.validate_records(env))

    def test_provenance_entry_wrong_type(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["provenance"] = [123]
        assert any(e["path"].startswith("findings[0].provenance") for e in fr.validate_records(env))

    def test_provenance_empty_string_entry_rejected(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["provenance"] = ["  "]
        assert any(e["path"].startswith("findings[0].provenance") for e in fr.validate_records(env))


# ---------------------------------------------------------------------------
# validate_records — Reject (rebuttal_overrides); keyed by assessment_id
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectRebuttalOverrides:
    def test_override_not_object(self) -> None:
        env = _semantic_envelope()
        env["rebuttal_overrides"] = ["A-01"]
        assert any(e["path"] == "rebuttal_overrides[0]" for e in fr.validate_records(env))

    def test_override_missing_assessment_id(self) -> None:
        env = _semantic_envelope()
        del env["rebuttal_overrides"][0]["assessment_id"]
        assert any(
            e["path"] == "rebuttal_overrides[0].assessment_id"
            for e in fr.validate_records(env)
        )

    def test_override_unknown_assessment_id(self) -> None:
        env = _semantic_envelope()
        env["rebuttal_overrides"][0]["assessment_id"] = "A-999"
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rebuttal_overrides[0].assessment_id"
            and "does not match" in e["message"]
            for e in errors
        )

    def test_override_bad_severity(self) -> None:
        env = _semantic_envelope()
        env["rebuttal_overrides"][0]["severity"] = "Spicy"
        assert any(e["path"] == "rebuttal_overrides[0].severity" for e in fr.validate_records(env))

    def test_override_missing_reasoning(self) -> None:
        env = _semantic_envelope()
        del env["rebuttal_overrides"][0]["reasoning"]
        assert any(e["path"] == "rebuttal_overrides[0].reasoning" for e in fr.validate_records(env))

    def test_override_assessment_id_check_skipped_when_findings_invalid(self) -> None:
        # When findings isn't a valid list, the cross-reference is skipped (so we
        # don't emit a misleading "does not match any finding") but a present,
        # non-empty assessment_id is still accepted structurally.
        env = _semantic_envelope()
        env["findings"] = "oops"
        errors = fr.validate_records(env)
        assert not any(
            e["scope"] == "rebuttal_override" and "does not match" in e["message"]
            for e in errors
        )


# ---------------------------------------------------------------------------
# validate_records — Reject (rule_quality_notes); rule_sources list, no id/rule
# ---------------------------------------------------------------------------


class TestValidateRecordsRejectRuleQualityNotes:
    def test_note_not_object(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"] = ["just a string"]
        assert any(e["path"] == "rule_quality_notes[0]" for e in fr.validate_records(env))

    @pytest.mark.parametrize("field", ["observation", "suggestion"])
    def test_note_missing_prose_field(self, field: str) -> None:
        env = _semantic_envelope()
        del env["rule_quality_notes"][0][field]
        assert any(e["path"] == f"rule_quality_notes[0].{field}" for e in fr.validate_records(env))

    def test_note_id_and_rule_not_required(self) -> None:
        # The reporter must NOT emit id / rule; their absence is not an error
        # (finalize assigns the rq# id and derives the rule label).
        env = _semantic_envelope()  # note already omits id / rule
        note_errors = [e for e in fr.validate_records(env) if e["scope"] == "rule_quality_note"]
        assert note_errors == []

    def test_note_rule_sources_required(self) -> None:
        env = _semantic_envelope()
        del env["rule_quality_notes"][0]["rule_sources"]
        assert any(
            e["path"] == "rule_quality_notes[0].rule_sources" for e in fr.validate_records(env)
        )

    def test_note_rule_sources_must_be_list(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = "rule--no-foo"
        assert any(
            e["path"] == "rule_quality_notes[0].rule_sources" for e in fr.validate_records(env)
        )

    def test_note_rule_sources_must_be_nonempty(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = []
        assert any(
            e["path"] == "rule_quality_notes[0].rule_sources" for e in fr.validate_records(env)
        )

    def test_note_rule_sources_entry_must_be_nonempty_string(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["  "]
        assert any(
            e["path"] == "rule_quality_notes[0].rule_sources" for e in fr.validate_records(env)
        )

    def test_note_rule_source_must_be_unique_across_notes(self) -> None:
        # _rule_dependency_map keys its source->note map on each rule_sources label,
        # so the same source named by two notes would collapse to the first, leaving
        # the later note's fix unable to invalidate any finding — validation rejects it.
        env = _semantic_envelope()
        note = env["rule_quality_notes"][0]
        env["rule_quality_notes"].append({**note})  # reuses rule--no-foo
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[1].rule_sources" and "duplicate" in e["message"]
            for e in errors
        )

    def test_note_rule_source_unique_check_normalises_whitespace(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"].append(
            {
                "rule_sources": ["  rule--no-foo  "],
                "rule_file": "review/no-foo.md",
                "observation": "o",
                "suggestion": "s",
            }
        )
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[1].rule_sources" and "duplicate" in e["message"]
            for e in errors
        )

    def test_note_rule_source_chunk_labels_clash_across_notes(self) -> None:
        # Finding F1: _rule_dependency_map keys its source->note map on the CANONICAL
        # (chunk-suffix-stripped) label, so two SEPARATE notes naming the same rule
        # via DIFFERENT chunk labels (rule--general-review--1 vs --2) collapse to one
        # key — the later note becomes a phantom whose checkbox invalidates nothing,
        # and chunk-2 findings are mis-attributed to the first note. The uniqueness
        # check canonicalizes the same way, so this cross-note clash is rejected
        # (contrast test_note_rule_file_matches_every_chunk_label_of_one_rule, where
        # a SINGLE note may list both chunks).
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--general-review--1"]
        env["rule_quality_notes"][0]["rule_file"] = "review/rules/general-review.md"
        env["rule_quality_notes"].append(
            {
                "rule_sources": ["rule--general-review--2"],
                "rule_file": "review/rules/general-review.md",
                "observation": "o",
                "suggestion": "s",
            }
        )
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[1].rule_sources" and "duplicate" in e["message"]
            for e in errors
        )

    def test_note_rule_file_required(self) -> None:
        env = _semantic_envelope()
        del env["rule_quality_notes"][0]["rule_file"]
        assert any(e["path"] == "rule_quality_notes[0].rule_file" for e in fr.validate_records(env))

    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/passwd",          # absolute (POSIX)
            "C:/secrets.md",        # absolute (Windows drive)
            "review/../escape.md",  # traversal
            "../review/escape.md",  # traversal at the root
            "other/rule.md",        # outside the rules dir
            "review/rule.txt",      # not a .md file
            "rule.md",              # at repo root, not under rules dir
        ],
    )
    def test_note_rule_file_rejects_unsafe_paths(self, bad_path: str) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["freeform"]  # skip stem cross-check
        env["rule_quality_notes"][0]["rule_file"] = bad_path
        assert any(e["path"] == "rule_quality_notes[0].rule_file" for e in fr.validate_records(env))

    def test_note_rule_file_accepts_nested_under_rules_dir(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--no-foo"]
        env["rule_quality_notes"][0]["rule_file"] = "review/sub/no-foo.md"
        assert fr.validate_records(env) == []

    def test_note_rule_file_honours_custom_rules_dir(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_file"] = "custom-rules/no-foo.md"
        # Valid under the configured dir...
        assert fr.validate_records(env, rules_dir="custom-rules/") == []
        # ...but rejected under the default "review/".
        assert any(
            e["path"] == "rule_quality_notes[0].rule_file"
            for e in fr.validate_records(env)
        )

    def test_note_rule_file_must_match_each_rule_source(self) -> None:
        # Finding C-12: rule_file (the path the agent edits) must name the SAME rule
        # as each rule_sources label (the invalidation key). A safe path whose stem
        # names a different rule is rejected — otherwise the fix edits one rule's
        # file while another rule's findings are marked invalidated.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--no-foo"]
        env["rule_quality_notes"][0]["rule_file"] = "review/simplicity.md"
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[0].rule_file"
            and "does not match rule_source" in e["message"]
            for e in errors
        )

    def test_note_rule_file_match_runs_per_source(self) -> None:
        # A multi-source note must point rule_file at a file matching EVERY source.
        # Here the file matches rule--no-foo but not rule--other → one mismatch error.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--no-foo", "rule--other"]
        env["rule_quality_notes"][0]["rule_file"] = "review/no-foo.md"
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[0].rule_file"
            and "rule--other" in e["message"]
            for e in errors
        )

    def test_note_rule_file_match_skipped_for_non_canonical_source(self) -> None:
        # The stem cross-check only applies to canonical "rule--<name>" sources. A
        # non-"rule--" source has no derivable name, so the cross-check is skipped.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["freeform-label"]
        env["rule_quality_notes"][0]["rule_file"] = "review/anything.md"
        assert fr.validate_records(env) == []

    def test_note_rule_file_matches_chunk_suffixed_source(self) -> None:
        # Finding r10: provenance labels are chunk-suffixed by the dispatch
        # (rule--general-review--1 is chunk 1 of the general-review rule) while the
        # rule file is not (general-review.md). A trailing --<digits> is normalized
        # off before the stem cross-check, so a chunk label resolves to its base file.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--general-review--1"]
        env["rule_quality_notes"][0]["rule_file"] = "review/rules/general-review.md"
        assert fr.validate_records(env) == []

    def test_note_rule_file_matches_every_chunk_label_of_one_rule(self) -> None:
        # A rule split across diff chunks tags each chunk's findings with its own
        # --<digits> label; a SINGLE quality note covers them all and points rule_file
        # at the one base rule file. Every chunk label must accept that file.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = [
            "rule--general-review--1",
            "rule--general-review--2",
        ]
        env["rule_quality_notes"][0]["rule_file"] = "review/rules/general-review.md"
        assert fr.validate_records(env) == []

    def test_note_rule_file_chunk_suffix_still_catches_real_mismatch(self) -> None:
        # Normalizing the chunk suffix forgives ONLY the suffix, never a genuinely
        # different rule name (C-12 retained): a chunk label whose base name differs
        # from the rule_file stem is still rejected. The message names the canonical
        # rule (suffix stripped) so it points at the right .md file.
        env = _semantic_envelope()
        env["rule_quality_notes"][0]["rule_sources"] = ["rule--general-review--1"]
        env["rule_quality_notes"][0]["rule_file"] = "review/rules/simplicity.md"
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[0].rule_file"
            and "does not match rule_source" in e["message"]
            and "'general-review'" in e["message"]
            for e in errors
        )


# ---------------------------------------------------------------------------
# Structured-error shape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_every_error_has_required_keys(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["severity"] = "Nope"
        env["run"]["scope"] = "bogus"
        errors = fr.validate_records(env)
        assert errors
        required = {"scope", "index", "path", "field", "record_id", "assessment_id", "message"}
        for err in errors:
            assert required <= set(err.keys())

    def test_error_struct_has_no_display_number_key(self) -> None:
        # display_number was removed from the schema; the error struct must not
        # resurrect it.
        env = _semantic_envelope()
        env["findings"][0]["severity"] = "Nope"
        errors = fr.validate_records(env)
        assert errors
        assert all("display_number" not in err for err in errors)

    def test_errors_are_json_serializable(self) -> None:
        env = _semantic_envelope()
        env["findings"][0]["verdict"] = "Bogus"
        errors = fr.validate_records(env)
        # Must round-trip cleanly so the orchestrator can relay them.
        assert json.loads(json.dumps(errors)) == errors


# ---------------------------------------------------------------------------
# finalize_records — the deterministic display layer
# ---------------------------------------------------------------------------


class TestFinalizeRecords:
    def test_derives_display_bucket_per_finding(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        by_aid = {f["assessment_id"]: f for f in finalized["findings"]}
        assert by_aid["A-01"]["display_bucket"] == "confirmed"
        assert by_aid[None]["display_bucket"] == "hidden"

    def test_orders_visible_buckets_before_hidden_then_file_line(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        order = [f["assessment_id"] for f in finalized["findings"]]
        # confirmed (a.py null, a.py 3, b.py 10), needs-decision, pre-existing, hidden.
        assert order == ["A-ca0", "A-ca3", "A-cb", "A-q", "A-pre", "A-inv"]

    def test_assigns_gap_free_f_ids_in_display_order(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        ids = [f["record_id"] for f in finalized["findings"]]
        assert ids == ["f1", "f2", "f3", "f4", "f5", "f6"]

    def test_visible_findings_get_leading_ids_hidden_trail(self) -> None:
        mapping = _id_to_record_id(fr.finalize_records(_multi_bucket_semantic()))
        # Five visible findings occupy f1..f5; the single hidden finding trails f6.
        assert mapping["A-inv"] == "f6"
        assert {mapping[a] for a in ("A-ca0", "A-ca3", "A-cb", "A-q", "A-pre")} == {
            "f1", "f2", "f3", "f4", "f5",
        }

    def test_null_line_sorts_before_numbered_line_in_same_file(self) -> None:
        mapping = _id_to_record_id(fr.finalize_records(_multi_bucket_semantic()))
        # Within the confirmed bucket, a.py(null) precedes a.py:3.
        assert mapping["A-ca0"] == "f1"
        assert mapping["A-ca3"] == "f2"

    def test_buckets_are_non_decreasing_after_finalize(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        ranks = [fr._BUCKET_ORDER[f["display_bucket"]] for f in finalized["findings"]]
        assert ranks == sorted(ranks)

    def test_assigns_sequential_note_ids(self) -> None:
        env = _semantic_envelope()
        env["rule_quality_notes"].append(
            {
                "rule_sources": ["rule--bar"],
                "rule_file": "review/bar.md",
                "observation": "o2",
                "suggestion": "s2",
            }
        )
        finalized = fr.finalize_records(env)
        assert [n["id"] for n in finalized["rule_quality_notes"]] == ["rq1", "rq2"]

    def test_derives_note_rule_label_from_rule_file_stem(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        assert finalized["rule_quality_notes"][0]["rule"] == "no-foo"

    def test_computes_run_counts(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        run = finalized["run"]
        assert run["consolidated_count"] == 6
        assert run["confirmed"] == 3       # three in-scope Confirmed
        assert run["questionable"] == 1    # one in-scope Questionable
        assert run["invalid"] == 1         # one Invalid verdict

    def test_empty_findings_yield_zero_counts(self) -> None:
        env = _semantic_envelope()
        env["findings"] = []
        env["rebuttal_overrides"] = []
        run = fr.finalize_records(env)["run"]
        assert run["consolidated_count"] == 0
        assert run["confirmed"] == 0
        assert run["questionable"] == 0
        assert run["invalid"] == 0

    def test_does_not_mutate_input(self) -> None:
        env = _semantic_envelope()
        snapshot = copy.deepcopy(env)
        fr.finalize_records(env)
        assert env == snapshot

    def test_is_idempotent(self) -> None:
        once = fr.finalize_records(_multi_bucket_semantic())
        twice = fr.finalize_records(once)
        assert once == twice

    def test_finalized_output_passes_semantic_validation(self) -> None:
        assert fr.validate_records(fr.finalize_records(_multi_bucket_semantic())) == []

    @pytest.mark.parametrize("bad", [None, "string", 123, ["a", "list"]])
    def test_robust_to_non_dict_input(self, bad: object) -> None:
        # Never raises; a non-dict round-trips unchanged.
        assert fr.finalize_records(bad) == bad

    def test_robust_to_malformed_findings(self) -> None:
        env = _semantic_envelope()
        env["findings"] = "not a list"
        # Does not raise; the malformed findings value is left as-is.
        result = fr.finalize_records(env)
        assert result["findings"] == "not a list"

    def test_robust_to_non_dict_finding_entries(self) -> None:
        env = _semantic_envelope()
        env["findings"].append("a bare string finding")
        # Non-dict entries are skipped for id assignment but don't crash; every dict
        # finding still receives a valid f# id (exact values are unspecified for this
        # malformed input — semantic validation already rejects non-dict findings).
        finalized = fr.finalize_records(env)
        dict_ids = [f["record_id"] for f in finalized["findings"] if isinstance(f, dict)]
        assert len(dict_ids) == 2
        assert all(fr._FINDING_ID_RE.match(rid) for rid in dict_ids)


# ---------------------------------------------------------------------------
# validate_finalized_records — the post-finalize invariant self-check
# ---------------------------------------------------------------------------


class TestValidateFinalizedRecords:
    def test_finalized_output_is_valid(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        assert fr.validate_finalized_records(finalized) == []

    def test_semantic_errors_still_surface(self) -> None:
        # The finalized check runs semantic validation first, so a bad verdict in an
        # otherwise-enriched file is still reported.
        finalized = fr.finalize_records(_semantic_envelope())
        finalized["findings"][0]["verdict"] = "Bogus"
        assert any(e["field"] == "verdict" for e in fr.validate_finalized_records(finalized))

    def test_bad_record_id_format_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        finalized["findings"][0]["record_id"] = "x1"
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["path"] == "findings[0].record_id" and "^f[0-9]+$" in e["message"]
            for e in errors
        )

    def test_out_of_sequence_f_ids_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        # Swap the two ids so neither sits at its expected position.
        finalized["findings"][0]["record_id"] = "f2"
        finalized["findings"][1]["record_id"] = "f1"
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["field"] == "record_id" and "out of sequence" in e["message"] for e in errors
        )

    def test_gap_in_f_ids_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        finalized["findings"][1]["record_id"] = "f3"  # skips f2
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["path"] == "findings[1].record_id" and "out of sequence" in e["message"]
            for e in errors
        )

    def test_bucket_ordering_violation_rejected(self) -> None:
        # A needs-decision finding (rank 1) placed before a confirmed finding
        # (rank 0) — ids stay sequential, so only the bucket-order invariant fires.
        def finding(record_id: str, verdict: str) -> dict:
            return {
                "record_id": record_id,
                "assessment_id": None,
                "title": "t",
                "file": "a.py",
                "line": 1,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": verdict,
                "type": "rule",
                "introduced_by": "diff",
                "description": "",
                "assessment": "",
                "suggestion": "",
                "provenance": ["rule--x"],
                "has_detail": False,
                "display_bucket": fr._derive_display_bucket(verdict, "diff"),
            }

        env = _semantic_envelope()
        env["rebuttal_overrides"] = []
        env["rule_quality_notes"] = []
        env["findings"] = [finding("f1", "Questionable"), finding("f2", "Confirmed")]
        errors = fr.validate_finalized_records(env)
        assert any(
            e["field"] == "display_bucket" and "ordered by display bucket" in e["message"]
            for e in errors
        )

    def test_display_bucket_inconsistent_with_derivation_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        # findings[0] is Confirmed/diff (derived "confirmed"); claim "pre-existing".
        finalized["findings"][0]["display_bucket"] = "pre-existing"
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["field"] == "display_bucket" and "inconsistent" in e["message"] for e in errors
        )

    def test_note_id_out_of_sequence_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        finalized["rule_quality_notes"][0]["id"] = "rq5"
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["scope"] == "rule_quality_note" and "out of sequence" in e["message"]
            for e in errors
        )

    def test_note_id_bad_format_rejected(self) -> None:
        finalized = fr.finalize_records(_semantic_envelope())
        finalized["rule_quality_notes"][0]["id"] = "RQ1"  # uppercase no longer valid
        errors = fr.validate_finalized_records(finalized)
        assert any(
            e["path"] == "rule_quality_notes[0].id" and "^rq[0-9]+$" in e["message"]
            for e in errors
        )

    def test_run_count_tamper_rejected(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        finalized["run"]["confirmed"] += 1
        errors = fr.validate_finalized_records(finalized)
        assert any(e["path"] == "run.confirmed" for e in errors)

    def test_consolidated_count_tamper_rejected(self) -> None:
        finalized = fr.finalize_records(_multi_bucket_semantic())
        finalized["run"]["consolidated_count"] = 99
        errors = fr.validate_finalized_records(finalized)
        assert any(e["path"] == "run.consolidated_count" for e in errors)


# ---------------------------------------------------------------------------
# load_and_validate_records
# ---------------------------------------------------------------------------


class TestLoadAndValidate:
    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "records.json"
        p.write_text(json.dumps(_semantic_envelope()), encoding="utf-8")
        data, errors = fr.load_and_validate_records(p)
        assert errors == []
        assert isinstance(data, dict)

    def test_missing_file(self, tmp_path: Path) -> None:
        data, errors = fr.load_and_validate_records(tmp_path / "nope.json")
        assert data is None
        assert len(errors) == 1
        assert errors[0]["scope"] == "envelope"
        assert "not found" in errors[0]["message"]

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "records.json"
        p.write_text("{ not valid json", encoding="utf-8")
        data, errors = fr.load_and_validate_records(p)
        assert data is None
        assert len(errors) == 1
        assert "not valid JSON" in errors[0]["message"]

    def test_invalid_envelope_reports_errors(self, tmp_path: Path) -> None:
        env = _semantic_envelope()
        env["findings"][0]["verdict"] = "Bogus"
        p = tmp_path / "records.json"
        p.write_text(json.dumps(env), encoding="utf-8")
        data, errors = fr.load_and_validate_records(p)
        assert isinstance(data, dict)
        assert any(e["field"] == "verdict" for e in errors)


# ---------------------------------------------------------------------------
# validate-records CLI handler
# ---------------------------------------------------------------------------


def _make_args(records: Path) -> argparse.Namespace:
    # Pin rules_dir to the default "review/" so the rule_file under-rules-dir
    # check is deterministic regardless of any repo/user config on the host.
    return argparse.Namespace(records=str(records), repo=".", rules_dir="review/")


class TestValidateRecordsCommand:
    def test_valid_prints_summary_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        p = tmp_path / "records.json"
        p.write_text(json.dumps(_semantic_envelope()), encoding="utf-8")
        fr.validate_records_command(_make_args(p))
        out = json.loads(capsys.readouterr().out)
        assert out["valid"] is True
        assert out["findings"] == 2
        # Counts come from finalize (the reporter no longer emits them).
        assert out["confirmed"] == 1
        assert out["questionable"] == 0
        assert out["invalid"] == 1
        assert out["run_id"] == "20260101-000000"

    def test_invalid_exits_1_with_structured_errors_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _semantic_envelope()
        env["findings"][0]["severity"] = "Critical?!"
        p = tmp_path / "records.json"
        p.write_text(json.dumps(env), encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            fr.validate_records_command(_make_args(p))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert captured.out == ""  # nothing on stdout for the failure path
        payload = json.loads(captured.err)
        assert payload["valid"] is False
        assert payload["error_count"] >= 1
        assert any(e["field"] == "severity" for e in payload["errors"])

    def test_missing_file_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc_info:
            fr.validate_records_command(_make_args(tmp_path / "nope.json"))
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().err)
        assert payload["valid"] is False


# ---------------------------------------------------------------------------
# Guard: the pure functions do not mutate the input envelope
# ---------------------------------------------------------------------------


def test_validate_does_not_mutate_input() -> None:
    env = _semantic_envelope()
    snapshot = copy.deepcopy(env)
    fr.validate_records(env)
    assert env == snapshot


def test_validate_finalized_does_not_mutate_input() -> None:
    finalized = fr.finalize_records(_multi_bucket_semantic())
    snapshot = copy.deepcopy(finalized)
    fr.validate_finalized_records(finalized)
    assert finalized == snapshot
