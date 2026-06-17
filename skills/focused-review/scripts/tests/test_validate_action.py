"""Tests for the ``validate-action`` subcommand + disregard run state (Phase 6).

Covers the canvas action-bar round-trip:

* ``validate_action`` — the pure validate/expand contract: a posted
  ``{run_id, record_ids[], instructions}`` is accepted only when the ``run_id``
  matches the rendered run and every ``record_id`` exists, resolving each to
  file/line/title/suggestion. A forged ``run_id`` or any unknown ``record_id`` is
  rejected with the same structured-error shape as ``validate_records``.
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
                "title": "Null deref in request handler",
                "file": "src/a.py",
                "line": 10,
                "original_severity": "High",
                "severity": "High",
                "fix_complexity": "moderate",
                "verdict": "Confirmed",
                "type": "concern",
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
                "title": "Duplicated parsing logic",
                "file": "src/b.py",
                "line": 20,
                "original_severity": "Medium",
                "severity": "Medium",
                "fix_complexity": "complex",
                "verdict": "Confirmed",
                "type": "rule",
                "description": "Same parse block in three methods.",
                "assessment": "Real duplication.",
                "suggestion": "Extract a shared parse helper.",
                "provenance": ["rule--simplicity"],
                "has_detail": False,
            },
            {
                "record_id": "r3",
                "assessment_id": "A-05",
                "display_number": 3,
                "title": "Magic number in retry loop",
                "file": "src/c.py",
                "line": None,
                "original_severity": "Low",
                "severity": "Low",
                "fix_complexity": "quickfix",
                "verdict": "Questionable",
                "type": "concern",
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
                "title": "Section divider comment",
                "file": "src/d.py",
                "line": 5,
                "original_severity": "Low",
                "severity": "Medium",
                "fix_complexity": "quickfix",
                "verdict": "Invalid",
                "type": "rule",
                "description": "A // ---- divider.",
                "assessment": "Navigational aid.",
                "suggestion": "",
                "provenance": ["rule--no-comments"],
                "has_detail": False,
            },
        ],
    }


RUN_ID = "20260617-120000"


def _write_records(tmp_path: Path, env: dict | None = None) -> Path:
    records = tmp_path / "records.json"
    records.write_text(json.dumps(env if env is not None else _envelope()), encoding="utf-8")
    return records


def _action_args(records: Path, run_id: str, record_ids: str, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        records=str(records),
        run_id=run_id,
        record_ids=record_ids,
        action=None,
        instructions="",
        apply_disregard=False,
        run_dir=None,
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
        assert err["path"] == "record_ids[1]"
        assert "unknown record_id" in err["message"]

    def test_forged_run_id_and_unknown_id_both_reported(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), "FORGED", ["nope"])
        assert expanded is None
        fields = {e["field"] for e in errors}
        assert fields == {"run_id", "record_id"}

    def test_empty_record_ids_rejected(self) -> None:
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, [])
        assert expanded is None
        assert any(e["field"] == "record_ids" for e in errors)

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

    def test_blank_record_id_token_rejected(self) -> None:
        # A non-empty list carrying a blank token is reported per-id, not resolved.
        expanded, errors = fr.validate_action(_envelope(), RUN_ID, [""])
        assert expanded is None
        assert any(e["field"] == "record_id" for e in errors)


# ---------------------------------------------------------------------------
# _split_record_ids
# ---------------------------------------------------------------------------


class TestSplitRecordIds:
    def test_splits_strips_and_dedupes_preserving_order(self) -> None:
        assert fr._split_record_ids(" r1, r2 ,r1,, r3 ") == ["r1", "r2", "r3"]

    def test_empty_and_none(self) -> None:
        assert fr._split_record_ids("") == []
        assert fr._split_record_ids(None) == []
        assert fr._split_record_ids("  ,  ,") == []


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
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": []}

    def test_load_run_state_malformed_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text("{ broken", encoding="utf-8")
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": []}

    def test_load_run_state_non_dict_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text("[1, 2]", encoding="utf-8")
        assert fr.load_run_state(str(tmp_path)) == {"disregarded": []}

    def test_load_run_state_stale_run_id_ignored(self, tmp_path: Path) -> None:
        fr.persist_disregard(str(tmp_path), "OLD-RUN", ["r1"])
        # A different expected run_id => treat the state as stale (empty).
        assert fr.load_run_state(str(tmp_path), expected_run_id="NEW-RUN") == {"disregarded": []}
        # Matching run_id => the state is honoured.
        assert fr.load_run_state(str(tmp_path), expected_run_id="OLD-RUN")["disregarded"] == ["r1"]

    def test_load_run_state_missing_run_id_ignored_when_expected(self, tmp_path: Path) -> None:
        # A state file without a run_id must not be applied to a named run.
        (tmp_path / "run-state.json").write_text(
            json.dumps({"disregarded": ["r1"]}), encoding="utf-8"
        )
        assert fr.load_run_state(str(tmp_path), expected_run_id="RID") == {"disregarded": []}
        # ...but a raw read (no expected run_id) still surfaces it.
        assert fr.load_run_state(str(tmp_path))["disregarded"] == ["r1"]

    def test_load_run_state_drops_non_string_ids(self, tmp_path: Path) -> None:
        (tmp_path / "run-state.json").write_text(
            json.dumps({"run_id": RUN_ID, "disregarded": ["r1", 5, "", None, "r2"]}),
            encoding="utf-8",
        )
        assert fr.load_run_state(str(tmp_path))["disregarded"] == ["r1", "r2"]


# ---------------------------------------------------------------------------
# disregard persists across a re-render (validate-action -> render-review)
# ---------------------------------------------------------------------------


def _dimmed_ids(canvas_html: str) -> set[str]:
    import re

    return set(re.findall(r'<div class="finding dimmed" data-record-id="([^"]+)"', canvas_html))


def _plain_ids(canvas_html: str) -> set[str]:
    import re

    return set(re.findall(r'<div class="finding" data-record-id="([^"]+)"', canvas_html))


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
