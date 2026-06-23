"""Tests for the records.json envelope schema + validation (Phase 2).

Covers the pure ``validate_records`` contract (accept a well-formed envelope,
reject many malformed variants with structured per-record errors), the
``load_and_validate_records`` loader, and the ``validate-records`` CLI handler.
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


def _valid_envelope() -> dict:
    """A minimal, fully valid records.json envelope.

    Exercises both provenance encodings (object form + bare-string form), a
    Confirmed finding with a detail sidecar, and an Invalid finding with a null
    assessment_id / display_number / line.
    """
    return {
        "schema_version": 1,
        "run": {
            "run_id": "20260101-000000",
            "scope": "branch",
            "date": "2026-01-01T00:00:00Z",
            "rule_count": 3,
            "concern_count": 2,
            "consolidated_count": 2,
            "confirmed": 1,
            "questionable": 0,
            "invalid": 1,
        },
        "rebuttal_overrides": [
            {
                "record_id": "r1",
                "original_severity": "Medium",
                "severity": "High",
                "reasoning": "Reinstated after rebuttal — the concern was correct.",
            }
        ],
        "rule_quality_notes": [
            {
                "id": "RQ1",
                "rule": "no-foo",
                "rule_source": "rule--no-foo",
                "rule_file": "review/no-foo.md",
                "observation": "noisy on tests",
                "suggestion": "scope to src/",
            }
        ],
        "findings": [
            {
                "record_id": "r1",
                "assessment_id": "A-01",
                "display_number": 1,
                "display_bucket": "confirmed",
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
                "record_id": "r2",
                "assessment_id": None,
                "display_number": None,
                "display_bucket": "hidden",
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


def _paths(errors: list[dict]) -> set[str]:
    return {e["path"] for e in errors}


def _by_field(errors: list[dict], field: str) -> list[dict]:
    return [e for e in errors if e.get("field") == field]


# ---------------------------------------------------------------------------
# _derive_display_bucket — scope normalization (the pure routing function)
# ---------------------------------------------------------------------------


class TestDeriveDisplayBucket:
    """Direct unit tests for the ``(verdict, introduced_by) → display_bucket`` map.

    The assessor emits a four-value ``introduced_by`` vocabulary — ``diff`` |
    ``pre-existing`` | ``reclassified-pre-existing`` | ``reclassified-diff``
    (``agents/review-assessor.agent.md``) — and the validator/renderer must route
    the two pre-existing spellings identically. Exact-string matching used to leak
    a ``reclassified-pre-existing`` Confirmed into the gating ``confirmed`` bucket
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
# Accept
# ---------------------------------------------------------------------------


class TestAccept:
    def test_valid_envelope_has_no_errors(self) -> None:
        assert fr.validate_records(_valid_envelope()) == []

    def test_empty_findings_with_zero_counts(self) -> None:
        env = _valid_envelope()
        env["findings"] = []
        env["rebuttal_overrides"] = []
        env["run"].update(consolidated_count=0, confirmed=0, questionable=0, invalid=0)
        assert fr.validate_records(env) == []

    def test_empty_optional_arrays(self) -> None:
        env = _valid_envelope()
        env["rebuttal_overrides"] = []
        env["rule_quality_notes"] = []
        assert fr.validate_records(env) == []

    def test_invalid_finding_may_omit_display_number(self) -> None:
        env = _valid_envelope()
        del env["findings"][1]["display_number"]  # Invalid finding: optional
        assert fr.validate_records(env) == []

    def test_nullable_fields_accept_null(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["line"] = None
        env["findings"][0]["assessment_id"] = None
        env["findings"][0]["has_detail"] = False  # so null assessment_id is allowed
        assert fr.validate_records(env) == []

    def test_text_fields_may_be_empty_strings(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["description"] = ""
        env["findings"][0]["assessment"] = ""
        env["findings"][0]["suggestion"] = ""
        assert fr.validate_records(env) == []

    def test_introduced_by_optional(self) -> None:
        env = _valid_envelope()
        del env["findings"][0]["introduced_by"]
        assert fr.validate_records(env) == []

    def test_introduced_by_empty_string_accepted(self) -> None:
        # Spec: introduced_by is "type-checked only, no enum" — "" is a valid str.
        env = _valid_envelope()
        env["findings"][0]["introduced_by"] = ""
        assert fr.validate_records(env) == []

    @pytest.mark.parametrize("severity", fr.VALID_SEVERITIES)
    def test_all_severities_accepted(self, severity: str) -> None:
        env = _valid_envelope()
        env["findings"][0]["severity"] = severity
        env["findings"][0]["original_severity"] = severity
        assert fr.validate_records(env) == []

    def test_all_four_buckets_accepted(self) -> None:
        # One finding per display_bucket, each numbered correctly: visible buckets
        # (confirmed / needs-decision / pre-existing) carry their own 1-based
        # number; the hidden finding carries null.
        env = _valid_envelope()
        base = env["findings"][0]
        env["findings"] = [
            {**base, "record_id": "r1", "assessment_id": None, "has_detail": False,
             "verdict": "Confirmed", "introduced_by": "diff",
             "display_bucket": "confirmed", "display_number": 1},
            {**base, "record_id": "r2", "assessment_id": None, "has_detail": False,
             "verdict": "Questionable", "introduced_by": "diff",
             "display_bucket": "needs-decision", "display_number": 1},
            {**base, "record_id": "r3", "assessment_id": None, "has_detail": False,
             "verdict": "Confirmed", "introduced_by": "pre-existing",
             "display_bucket": "pre-existing", "display_number": 1},
            {**base, "record_id": "r4", "assessment_id": None, "has_detail": False,
             "verdict": "Questionable", "introduced_by": "pre-existing",
             "display_bucket": "hidden", "display_number": None},
        ]
        env["rebuttal_overrides"] = []
        # confirmed bucket = 1, needs-decision bucket = 1, invalid verdict = 0.
        env["run"].update(consolidated_count=4, confirmed=1, questionable=1, invalid=0)
        assert fr.validate_records(env) == []

    def test_pre_existing_questionable_is_hidden(self) -> None:
        # A pre-existing Questionable finding routes to the hidden bucket and
        # therefore must carry a null display_number (recorded, not shown).
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Questionable",
            introduced_by="pre-existing",
            display_bucket="hidden",
            display_number=None,
        )
        env["run"].update(invalid=0, questionable=0)
        assert fr.validate_records(env) == []

    def test_reclassified_pre_existing_confirmed_routes_to_pre_existing(self) -> None:
        # The assessor may tag a finding `reclassified-pre-existing` (a discovery
        # misclassification corrected during assessment). It must route exactly like
        # `pre-existing`: a Confirmed lands in the non-gating pre-existing bucket and
        # is excluded from run.confirmed (focused-review-7gj.11).
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="reclassified-pre-existing",
            display_bucket="pre-existing",
            display_number=1,
        )
        # confirmed bucket = 1 (only r1); the pre-existing finding is excluded.
        env["run"].update(invalid=0, confirmed=1)
        assert fr.validate_records(env) == []

    def test_reclassified_diff_confirmed_routes_to_confirmed(self) -> None:
        # `reclassified-diff` is in-scope (a pre-existing tag corrected to diff), so a
        # Confirmed lands in the gating confirmed bucket and IS counted — unlike the
        # reclassified-pre-existing case above.
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="reclassified-diff",
            display_bucket="confirmed",
            display_number=2,
        )
        # Both findings are now in-scope Confirmed: confirmed bucket = 2, contiguous 1,2.
        env["run"].update(invalid=0, confirmed=2)
        assert fr.validate_records(env) == []


# ---------------------------------------------------------------------------
# Reject — envelope level
# ---------------------------------------------------------------------------


class TestRejectEnvelope:
    def test_root_not_object(self) -> None:
        errors = fr.validate_records([1, 2, 3])
        assert len(errors) == 1
        assert errors[0]["scope"] == "envelope"
        assert "must be a JSON object" in errors[0]["message"]

    def test_missing_schema_version(self) -> None:
        env = _valid_envelope()
        del env["schema_version"]
        errors = fr.validate_records(env)
        assert any(e["field"] == "schema_version" for e in errors)

    def test_unsupported_schema_version(self) -> None:
        env = _valid_envelope()
        env["schema_version"] = 2
        errors = fr.validate_records(env)
        assert any("unsupported schema_version" in e["message"] for e in errors)

    def test_schema_version_wrong_type(self) -> None:
        env = _valid_envelope()
        env["schema_version"] = "1"
        errors = fr.validate_records(env)
        assert any(e["field"] == "schema_version" for e in errors)

    def test_missing_run(self) -> None:
        env = _valid_envelope()
        del env["run"]
        errors = fr.validate_records(env)
        assert any(e["field"] == "run" for e in errors)

    def test_missing_findings(self) -> None:
        env = _valid_envelope()
        del env["findings"]
        errors = fr.validate_records(env)
        assert any(e["field"] == "findings" for e in errors)

    def test_findings_not_array(self) -> None:
        env = _valid_envelope()
        env["findings"] = {"r1": {}}
        errors = fr.validate_records(env)
        assert any(e["field"] == "findings" and "array" in e["message"] for e in errors)

    def test_missing_rebuttal_overrides(self) -> None:
        env = _valid_envelope()
        del env["rebuttal_overrides"]
        errors = fr.validate_records(env)
        assert any(e["field"] == "rebuttal_overrides" for e in errors)

    def test_missing_rule_quality_notes(self) -> None:
        env = _valid_envelope()
        del env["rule_quality_notes"]
        errors = fr.validate_records(env)
        assert any(e["field"] == "rule_quality_notes" for e in errors)


# ---------------------------------------------------------------------------
# Reject — run metadata
# ---------------------------------------------------------------------------


class TestRejectRun:
    def test_missing_run_id(self) -> None:
        env = _valid_envelope()
        env["run"]["run_id"] = "   "
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.run_id" for e in errors)

    def test_invalid_scope(self) -> None:
        env = _valid_envelope()
        env["run"]["scope"] = "everything"
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.scope" for e in errors)

    def test_negative_count(self) -> None:
        env = _valid_envelope()
        env["run"]["rule_count"] = -1
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.rule_count" for e in errors)

    def test_count_wrong_type(self) -> None:
        env = _valid_envelope()
        env["run"]["confirmed"] = "1"
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.confirmed" for e in errors)

    def test_bool_is_not_a_valid_count(self) -> None:
        # bool is a subclass of int — must be rejected for integer count fields.
        env = _valid_envelope()
        env["run"]["rule_count"] = True
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.rule_count" for e in errors)


# ---------------------------------------------------------------------------
# Reject — run/findings count cross-checks
# ---------------------------------------------------------------------------


class TestRejectCountConsistency:
    def test_confirmed_count_mismatch(self) -> None:
        env = _valid_envelope()
        env["run"]["confirmed"] = 5  # actually 1 finding in the confirmed bucket
        errors = fr.validate_records(env)
        msgs = [e["message"] for e in errors if e["path"] == "run.confirmed"]
        assert msgs and "'confirmed' display bucket" in msgs[0]

    def test_invalid_count_mismatch(self) -> None:
        env = _valid_envelope()
        env["run"]["invalid"] = 0  # actually 1 Invalid finding
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.invalid" for e in errors)

    def test_consolidated_count_mismatch_detects_truncation(self) -> None:
        env = _valid_envelope()
        env["run"]["consolidated_count"] = 30  # but findings[] has 2
        errors = fr.validate_records(env)
        assert any(e["path"] == "run.consolidated_count" for e in errors)

    def test_bad_verdict_does_not_trigger_spurious_count_error(self) -> None:
        # A finding with an invalid verdict drops out of the tally; the per-bucket
        # count cross-check must be suppressed so the reporter is pointed at the
        # real (verdict) problem, not a misleading run.confirmed mismatch.
        env = _valid_envelope()
        env["findings"][0]["verdict"] = "Bogus"  # was the single Confirmed
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].verdict" for e in errors)
        assert not any(
            e["path"] in ("run.confirmed", "run.questionable", "run.invalid")
            for e in errors
        )

    def test_pre_existing_confirmed_excluded_from_confirmed_count(self) -> None:
        # A pre-existing Confirmed finding lives in the 'pre-existing' bucket and is
        # excluded from run.confirmed (the gating main tally). Counting it inflates
        # confirmed and is rejected.
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="pre-existing",
            display_bucket="pre-existing",
            display_number=1,
        )
        env["run"].update(invalid=0, confirmed=2)  # confirmed bucket is really 1
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "run.confirmed" and "'confirmed' display bucket" in e["message"]
            for e in errors
        )

    def test_needs_decision_count_excludes_hidden_pre_existing(self) -> None:
        # A pre-existing Questionable finding is hidden, so it does NOT count toward
        # run.questionable (the visible needs-decision tally).
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Questionable",
            introduced_by="pre-existing",
            display_bucket="hidden",
            display_number=None,
        )
        env["run"].update(invalid=0, questionable=1)  # hidden → really 0
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "run.questionable"
            and "'needs-decision' display bucket" in e["message"]
            for e in errors
        )


# ---------------------------------------------------------------------------
# Reject — findings
# ---------------------------------------------------------------------------


class TestRejectFinding:
    def test_finding_not_object(self) -> None:
        env = _valid_envelope()
        env["findings"][0] = "not an object"
        env["run"]["confirmed"] = 0  # avoid an unrelated count error
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0]" and e["scope"] == "finding" for e in errors)

    def test_missing_record_id(self) -> None:
        env = _valid_envelope()
        del env["findings"][0]["record_id"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].record_id" for e in errors)

    def test_duplicate_record_id(self) -> None:
        env = _valid_envelope()
        env["findings"][1]["record_id"] = "r1"
        errors = fr.validate_records(env)
        dupes = [e for e in errors if e["field"] == "record_id" and "duplicate" in e["message"]]
        assert dupes

    @pytest.mark.parametrize("bad", ["x1", "R1", "r", "1", "rr1", "r1a", "r 1", "record-1"])
    def test_record_id_must_match_r_number_format(self, bad: str) -> None:
        # The r# format lets the unified canvas action bar disambiguate ids by
        # prefix (findings r#, rule-quality notes RQ#).
        env = _valid_envelope()
        env["findings"][0]["record_id"] = bad
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[0].record_id" and "^r[0-9]+$" in e["message"]
            for e in errors
        )

    def test_record_id_format_accepts_multi_digit(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["record_id"] = "r1024"
        env["rebuttal_overrides"][0]["record_id"] = "r1024"
        assert fr.validate_records(env) == []

    def test_display_bucket_required(self) -> None:
        env = _valid_envelope()
        del env["findings"][0]["display_bucket"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].display_bucket" for e in errors)

    def test_display_bucket_bad_enum(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["display_bucket"] = "visible"
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].display_bucket" for e in errors)

    def test_display_bucket_must_match_derivation(self) -> None:
        # An Invalid finding always derives to 'hidden'; tagging it 'confirmed'
        # is an illegal state and is rejected.
        env = _valid_envelope()
        env["findings"][1]["display_bucket"] = "confirmed"  # finding 1 is Invalid
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[1].display_bucket" and "inconsistent" in e["message"]
            for e in errors
        )

    def test_display_bucket_confirmed_pre_existing_mismatch(self) -> None:
        # A pre-existing Confirmed finding must be 'pre-existing', not 'confirmed'.
        env = _valid_envelope()
        env["findings"][0]["introduced_by"] = "pre-existing"
        # display_bucket stays "confirmed" → inconsistent with derived "pre-existing".
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[0].display_bucket" and "inconsistent" in e["message"]
            for e in errors
        )

    def test_display_bucket_reclassified_pre_existing_confirmed_mismatch(self) -> None:
        # Regression (focused-review-7gj.11): a `reclassified-pre-existing` Confirmed
        # now derives to 'pre-existing', so the old exact-match 'confirmed' label is an
        # illegal state. Before the fix it derived to 'confirmed' (exact-string match),
        # the label was accepted, and the finding leaked into the gating Confirmed tally.
        env = _valid_envelope()
        env["findings"][0]["introduced_by"] = "reclassified-pre-existing"
        # display_bucket stays "confirmed" → inconsistent with derived "pre-existing".
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[0].display_bucket" and "inconsistent" in e["message"]
            for e in errors
        )

    def test_error_carries_finding_identity(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["severity"] = "Nope"
        errors = fr.validate_records(env)
        err = _by_field(errors, "severity")[0]
        assert err["record_id"] == "r1"
        assert err["assessment_id"] == "A-01"
        assert err["display_number"] == 1

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("severity", "Severe"),
            ("original_severity", "x"),
            ("fix_complexity", "trivial"),
            ("verdict", "Maybe"),
            ("type", "heuristic"),
        ],
    )
    def test_bad_enum_values(self, field: str, bad: str) -> None:
        env = _valid_envelope()
        env["findings"][0][field] = bad
        errors = fr.validate_records(env)
        assert any(e["path"] == f"findings[0].{field}" for e in errors)

    def test_missing_title(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["title"] = ""
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].title" for e in errors)

    def test_description_must_be_string(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["description"] = None
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].description" for e in errors)

    def test_line_negative(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["line"] = -5
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].line" for e in errors)

    def test_line_wrong_type(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["line"] = "42"
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].line" for e in errors)

    def test_has_detail_must_be_bool(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["has_detail"] = "true"
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].has_detail" for e in errors)

    def test_has_detail_true_requires_assessment_id(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["has_detail"] = True
        env["findings"][0]["assessment_id"] = None
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[0].assessment_id" and "has_detail" in e["message"]
            for e in errors
        )

    def test_assessment_id_empty_string_rejected(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["assessment_id"] = ""
        env["findings"][0]["has_detail"] = False
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].assessment_id" for e in errors)

    def test_confirmed_finding_requires_display_number(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["display_number"] = None  # visible bucket → required
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].display_number" for e in errors)

    def test_display_number_must_be_positive(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["display_number"] = 0
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].display_number" for e in errors)

    def test_duplicate_display_number(self) -> None:
        env = _valid_envelope()
        # A second finding in the SAME visible bucket reusing finding 0's number.
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="diff",
            display_bucket="confirmed",
            display_number=1,  # collides within the 'confirmed' bucket
        )
        env["run"].update(confirmed=2, invalid=0)
        errors = fr.validate_records(env)
        assert any(
            e["field"] == "display_number" and "duplicate" in e["message"] for e in errors
        )

    def test_numbers_are_independent_across_buckets(self) -> None:
        # Each visible bucket numbers from 1, so a confirmed #1 and a pre-existing
        # #1 coexist without collision (uniqueness is per-bucket, not global).
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="pre-existing",
            display_bucket="pre-existing",
            display_number=1,
        )
        env["run"].update(confirmed=1, invalid=0)
        assert fr.validate_records(env) == []

    def test_hidden_finding_must_not_carry_a_number(self) -> None:
        # finding 1 is hidden (Invalid); a non-null display_number is rejected —
        # hidden findings are recorded, not shown, so they have no number.
        env = _valid_envelope()
        env["findings"][1]["display_number"] = 1
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings[1].display_number" and "null" in e["message"]
            for e in errors
        )

    def test_contiguous_display_numbers_pass(self) -> None:
        # Two findings in the same visible bucket forming a gap-free 1..N run.
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="diff",
            display_bucket="confirmed",
            display_number=2,
        )
        env["run"].update(confirmed=2, invalid=0)
        assert fr.validate_records(env) == []

    def test_noncontiguous_display_numbers_rejected(self) -> None:
        # A gap in a visible bucket's sequence ({1, 3}) would render skipped
        # numbers downstream, so validation rejects it at the envelope level.
        env = _valid_envelope()
        env["findings"][1].update(
            verdict="Confirmed",
            introduced_by="diff",
            display_bucket="confirmed",
            display_number=3,  # gap within 'confirmed': {1, 3}
        )
        env["run"].update(confirmed=2, invalid=0)
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "findings" and "contiguous" in e["message"] for e in errors
        )

    def test_duplicate_assessment_id(self) -> None:
        env = _valid_envelope()
        # Two findings sharing an assessment id would collide in the invalid
        # table / detail-sidecar lookup.
        env["findings"][1]["assessment_id"] = "A-01"  # same as finding 0
        errors = fr.validate_records(env)
        assert any(
            e["field"] == "assessment_id" and "duplicate" in e["message"] for e in errors
        )


# ---------------------------------------------------------------------------
# Reject — provenance
# ---------------------------------------------------------------------------


class TestRejectProvenance:
    def test_provenance_must_be_present(self) -> None:
        env = _valid_envelope()
        del env["findings"][0]["provenance"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].provenance" for e in errors)

    def test_provenance_must_be_nonempty(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["provenance"] = []
        errors = fr.validate_records(env)
        assert any(e["path"] == "findings[0].provenance" for e in errors)

    def test_provenance_object_requires_source(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["provenance"] = [{"original_severity": "High"}]
        errors = fr.validate_records(env)
        assert any("source" in e["message"] for e in errors if e["field"] == "provenance")

    def test_provenance_entry_wrong_type(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["provenance"] = [123]
        errors = fr.validate_records(env)
        assert any(e["field"] == "provenance" for e in errors)

    def test_provenance_empty_string_entry_rejected(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["provenance"] = ["  "]
        errors = fr.validate_records(env)
        assert any(e["field"] == "provenance" for e in errors)


# ---------------------------------------------------------------------------
# Reject — rebuttal_overrides
# ---------------------------------------------------------------------------


class TestRejectRebuttalOverrides:
    def test_override_not_object(self) -> None:
        env = _valid_envelope()
        env["rebuttal_overrides"] = ["r1"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "rebuttal_overrides[0]" for e in errors)

    def test_override_unknown_record_id(self) -> None:
        env = _valid_envelope()
        env["rebuttal_overrides"][0]["record_id"] = "does-not-exist"
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rebuttal_overrides[0].record_id"
            and "does not match" in e["message"]
            for e in errors
        )

    def test_override_bad_severity(self) -> None:
        env = _valid_envelope()
        env["rebuttal_overrides"][0]["severity"] = "Spicy"
        errors = fr.validate_records(env)
        assert any(e["path"] == "rebuttal_overrides[0].severity" for e in errors)

    def test_override_missing_reasoning(self) -> None:
        env = _valid_envelope()
        del env["rebuttal_overrides"][0]["reasoning"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "rebuttal_overrides[0].reasoning" for e in errors)

    def test_override_record_id_check_skipped_when_findings_invalid(self) -> None:
        # When findings isn't a valid list, the cross-reference is skipped (so we
        # don't emit a misleading "does not match any finding") but a present,
        # non-empty record_id is still accepted structurally.
        env = _valid_envelope()
        env["findings"] = "oops"
        errors = fr.validate_records(env)
        assert not any(
            e["scope"] == "rebuttal_override" and "does not match" in e["message"]
            for e in errors
        )


# ---------------------------------------------------------------------------
# Reject — rule_quality_notes
# ---------------------------------------------------------------------------


class TestRejectRuleQualityNotes:
    def test_note_not_object(self) -> None:
        env = _valid_envelope()
        env["rule_quality_notes"] = ["just a string"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "rule_quality_notes[0]" for e in errors)

    @pytest.mark.parametrize(
        "field", ["rule", "rule_source", "observation", "suggestion"]
    )
    def test_note_missing_field(self, field: str) -> None:
        env = _valid_envelope()
        del env["rule_quality_notes"][0][field]
        errors = fr.validate_records(env)
        assert any(e["path"] == f"rule_quality_notes[0].{field}" for e in errors)

    def test_note_missing_id(self) -> None:
        env = _valid_envelope()
        del env["rule_quality_notes"][0]["id"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "rule_quality_notes[0].id" for e in errors)

    @pytest.mark.parametrize("bad", ["rq1", "RQ", "R1", "Q1", "RQ1a", "1", "RQ-1"])
    def test_note_id_must_match_rq_format(self, bad: str) -> None:
        env = _valid_envelope()
        env["rule_quality_notes"][0]["id"] = bad
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[0].id" and "^RQ[0-9]+$" in e["message"]
            for e in errors
        )

    def test_note_id_must_be_unique(self) -> None:
        env = _valid_envelope()
        note = env["rule_quality_notes"][0]
        env["rule_quality_notes"].append({**note})  # same id RQ1
        errors = fr.validate_records(env)
        assert any(
            e["path"] == "rule_quality_notes[1].id" and "duplicate" in e["message"]
            for e in errors
        )

    def test_note_rule_file_required(self) -> None:
        env = _valid_envelope()
        del env["rule_quality_notes"][0]["rule_file"]
        errors = fr.validate_records(env)
        assert any(e["path"] == "rule_quality_notes[0].rule_file" for e in errors)

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
        env = _valid_envelope()
        env["rule_quality_notes"][0]["rule_file"] = bad_path
        errors = fr.validate_records(env)
        assert any(e["path"] == "rule_quality_notes[0].rule_file" for e in errors)

    def test_note_rule_file_accepts_nested_under_rules_dir(self) -> None:
        env = _valid_envelope()
        env["rule_quality_notes"][0]["rule_file"] = "review/sub/no-foo.md"
        assert fr.validate_records(env) == []

    def test_note_rule_file_honours_custom_rules_dir(self) -> None:
        # When the caller passes a configured rules_dir, the under-dir check uses
        # it instead of the default "review/".
        env = _valid_envelope()
        env["rule_quality_notes"][0]["rule_file"] = "custom-rules/no-foo.md"
        # Valid under the configured dir...
        assert fr.validate_records(env, rules_dir="custom-rules/") == []
        # ...but rejected under the default "review/".
        assert any(
            e["path"] == "rule_quality_notes[0].rule_file"
            for e in fr.validate_records(env)
        )


# ---------------------------------------------------------------------------
# Structured-error shape
# ---------------------------------------------------------------------------


class TestErrorShape:
    def test_every_error_has_required_keys(self) -> None:
        env = _valid_envelope()
        env["findings"][0]["severity"] = "Nope"
        env["run"]["scope"] = "bogus"
        errors = fr.validate_records(env)
        assert errors
        required = {
            "scope",
            "index",
            "path",
            "field",
            "record_id",
            "assessment_id",
            "display_number",
            "message",
        }
        for err in errors:
            assert required <= set(err.keys())

    def test_errors_are_json_serializable(self) -> None:
        env = _valid_envelope()
        del env["findings"][0]["record_id"]
        errors = fr.validate_records(env)
        # Must round-trip cleanly so the orchestrator can relay them.
        assert json.loads(json.dumps(errors)) == errors


# ---------------------------------------------------------------------------
# load_and_validate_records
# ---------------------------------------------------------------------------


class TestLoadAndValidate:
    def test_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "records.json"
        p.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
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
        env = _valid_envelope()
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
        p.write_text(json.dumps(_valid_envelope()), encoding="utf-8")
        fr.validate_records_command(_make_args(p))
        out = json.loads(capsys.readouterr().out)
        assert out["valid"] is True
        assert out["findings"] == 2
        assert out["confirmed"] == 1
        assert out["run_id"] == "20260101-000000"

    def test_invalid_exits_1_with_structured_errors_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        env = _valid_envelope()
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
# Guard: the helper does not mutate the input envelope
# ---------------------------------------------------------------------------


def test_validate_does_not_mutate_input() -> None:
    env = _valid_envelope()
    snapshot = copy.deepcopy(env)
    fr.validate_records(env)
    assert env == snapshot
