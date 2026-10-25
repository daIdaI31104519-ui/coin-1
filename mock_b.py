"""Local mock receiver for the A-1 handoff contract; no market judgment."""

import csv
import hashlib
import io
import json
from pathlib import Path
import re

from a1_quality import (HEADER, MINUTE, RULES_VERSION, SCHEMA_VERSION,
                        ProcessingError, RunResult, Settings, parse_timestamp,
                        resolve_csv, validate_settings)


class RejectedReport(ValueError):
    pass


def receive(recent_run: RunResult, report_path: Path, workspace: Path,
            expected: Settings, decision_time_utc: str):
    """Return eligible bar starts from a verified local A-1 run."""
    if (recent_run.exit_code != 0 or recent_run.report_path is None
            or Path(report_path).resolve() != recent_run.report_path.resolve()):
        raise RejectedReport("report is not the successful recent run")
    try:
        validate_settings(expected)
        if Path(workspace).resolve() != expected.workspace.resolve():
            raise RejectedReport("workspace differs from expected settings")
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if type(report) is not dict:
            raise RejectedReport("invalid report object")
        string_fields = ("schema_version", "dataset_id", "instrument_id", "source_kind",
                         "validation_rules_version", "expected_start_utc", "expected_end_utc",
                         "raw_csv_reference", "raw_sha256")
        if any(type(report.get(field)) is not str for field in string_fields):
            raise RejectedReport("invalid or missing string field")
        if (type(report.get("timeframe_seconds")) is not int
                or type(report.get("rows_seen")) is not int or report["rows_seen"] < 0
                or type(report.get("quality_passed")) is not bool
                or type(report.get("issues")) is not list):
            raise RejectedReport("invalid or missing field type")
        if not report["quality_passed"] or report["issues"]:
            raise RejectedReport("quality gate did not pass")
        expected_fields = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": expected.dataset_id,
            "instrument_id": expected.instrument_id,
            "source_kind": expected.source_kind,
            "timeframe_seconds": expected.timeframe_seconds,
            "validation_rules_version": RULES_VERSION,
            "expected_start_utc": expected.expected_start_utc,
            "expected_end_utc": expected.expected_end_utc,
            "raw_csv_reference": expected.csv_reference,
        }
        if any(report.get(field) != value for field, value in expected_fields.items()):
            raise RejectedReport("report identity or window differs")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", report["raw_sha256"]):
            raise RejectedReport("invalid hash format")
        source = resolve_csv(workspace, report["raw_csv_reference"])
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != report["raw_sha256"].lower():
            raise RejectedReport("CSV hash differs")
        decision_time = parse_timestamp(decision_time_utc)
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True))
        if not rows or rows[0] != HEADER or len(rows) - 1 != report["rows_seen"]:
            raise RejectedReport("CSV shape differs")
        eligible = []
        for row in rows[1:]:
            if len(row) != 7:
                raise RejectedReport("CSV shape differs")
            bar_start = parse_timestamp(row[0])
            available_at = parse_timestamp(row[1])
            if bar_start + MINUTE <= decision_time and available_at <= decision_time:
                eligible.append(row[0])
        return eligible
    except (OSError, ValueError, UnicodeError, csv.Error, KeyError, TypeError, ProcessingError) as exc:
        if isinstance(exc, RejectedReport):
            raise
        raise RejectedReport("report or CSV could not be verified") from exc
