"""A-1 quality gate for synthetic one-minute BTC-like CSV files."""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


HEADER = ["bar_start_utc", "available_at_utc", "open", "high", "low", "close", "volume"]
SCHEMA_VERSION = "a1-quality-report-v0.1"
RULES_VERSION = "a1-v0.3"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
MINUTE = timedelta(seconds=60)


class ProcessingError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    workspace: Path
    csv_reference: str
    output: Path
    dataset_id: str
    instrument_id: str
    source_kind: str
    timeframe_seconds: int
    expected_start_utc: str
    expected_end_utc: str


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    report_path: Path | None
    error: str | None = None
    warning: str | None = None


def parse_timestamp(value):
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        raise ValueError("timestamp format")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def resolve_csv(workspace, reference):
    if not isinstance(reference, str) or not reference or Path(reference).is_absolute() or ".." in Path(reference).parts:
        raise ProcessingError("CSV reference must remain inside the workspace")
    root = Path(workspace).resolve(strict=True)
    path = (root / reference).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ProcessingError("CSV reference is outside the workspace or not a file")
    return path


def validate_settings(settings):
    if (settings.dataset_id != "synthetic-btc-1m-001"
            or settings.instrument_id != "BTC_SYNTHETIC"
            or settings.source_kind != "SYNTHETIC"
            or type(settings.timeframe_seconds) is not int
            or settings.timeframe_seconds != 60):
        raise ProcessingError("unsupported synthetic dataset settings")
    try:
        start = parse_timestamp(settings.expected_start_utc)
        end = parse_timestamp(settings.expected_end_utc)
    except ValueError as exc:
        raise ProcessingError("invalid expected window") from exc
    if start >= end or start.second or end.second:
        raise ProcessingError("expected window must have ordered minute boundaries")
    return start, end


def issue(code, row_number=None, bar_start_utc=None):
    return {"code": code, "row_number": row_number,
            "bar_start_utc": bar_start_utc, "message": code.replace("_", " ").lower()}


def validate_csv(raw, start, end):
    issues = []
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, [issue("SCHEMA_ERROR", 1)]
    if content.startswith("\ufeff"):
        return None, [issue("SCHEMA_ERROR", 1)]
    lines = content.splitlines()
    if not lines:
        return 0, [issue("SCHEMA_ERROR", 1)]
    parsed = []
    for number, line in enumerate(lines, 1):
        try:
            fields = next(csv.reader([line], strict=True)) if line else []
        except (csv.Error, StopIteration):
            fields = []
        parsed.append((number, fields))
    if parsed[0][1] != HEADER:
        issues.append(issue("SCHEMA_ERROR", 1))
    rows_seen = len(parsed) - 1
    if rows_seen == 0 and not issues:
        issues.append(issue("EMPTY_DATASET"))
    starts = []
    structure_ok = not issues
    timestamps_ok = True
    for number, fields in parsed[1:]:
        if len(fields) != 7 or not all(fields):
            issues.append(issue("SCHEMA_ERROR", number, fields[0] if fields else None))
            structure_ok = False
            continue
        bar_text, available_text = fields[:2]
        try:
            bar_start = parse_timestamp(bar_text)
            if bar_start.second:
                raise ValueError("minute boundary")
        except ValueError:
            issues.append(issue("INVALID_TIMESTAMP", number, bar_text))
            timestamps_ok = False
            bar_start = None
        try:
            available_at = parse_timestamp(available_text)
        except ValueError:
            issues.append(issue("INVALID_TIMESTAMP", number, bar_text))
            available_at = None
        prices = []
        for value in fields[2:6]:
            try:
                price = Decimal(value)
                if not price.is_finite() or price <= 0:
                    raise InvalidOperation
                prices.append(price)
            except (InvalidOperation, ValueError):
                issues.append(issue("INVALID_PRICE", number, bar_text))
                prices = []
                break
        if len(prices) == 4:
            opening, high, low, close = prices
            if not low <= min(opening, close) <= max(opening, close) <= high:
                issues.append(issue("INVALID_OHLC", number, bar_text))
        try:
            volume = Decimal(fields[6])
            if not volume.is_finite() or volume < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            issues.append(issue("INVALID_VOLUME", number, bar_text))
        if bar_start is not None and available_at is not None and available_at - bar_start < MINUTE:
            issues.append(issue("PREMATURE_AVAILABILITY", number, bar_text))
        if bar_start is not None:
            starts.append((number, bar_start, bar_text))
    if structure_ok and timestamps_ok:
        seen = set()
        previous = None
        for number, bar_start, bar_text in starts:
            if bar_start in seen:
                issues.append(issue("DUPLICATE_BAR", number, bar_text))
            elif previous is not None and bar_start < previous:
                issues.append(issue("OUT_OF_ORDER", number, bar_text))
            seen.add(bar_start)
            previous = bar_start
            if not start <= bar_start < end:
                issues.append(issue("OUT_OF_RANGE", number, bar_text))
        expected = start
        while expected < end:
            if expected not in seen:
                issues.append(issue("MISSING_BAR", bar_start_utc=expected.strftime("%Y-%m-%dT%H:%M:%SZ")))
            expected += MINUTE
    issues.sort(key=lambda item: (item["row_number"] is None,
                                  item["row_number"] if item["row_number"] is not None else 0,
                                  item["code"], item["bar_start_utc"] or ""))
    return rows_seen, issues


def publish_report(output, report):
    temporary = None
    published = False
    cleanup_warning = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent,
                                         prefix=".a1-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
        published = True
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if not published:
                    raise
                cleanup_warning = f"temporary report cleanup failed: {exc}"
    return cleanup_warning


def run(settings):
    try:
        start, end = validate_settings(settings)
        output = settings.output.resolve()
        if output.exists():
            raise ProcessingError("report destination already exists")
        source = resolve_csv(settings.workspace, settings.csv_reference)
        if source == output:
            raise ProcessingError("input and report paths must differ")
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        rows_seen, issues = validate_csv(raw, start, end)
        report = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": settings.dataset_id,
            "instrument_id": settings.instrument_id,
            "source_kind": settings.source_kind,
            "timeframe_seconds": settings.timeframe_seconds,
            "validation_rules_version": RULES_VERSION,
            "expected_start_utc": settings.expected_start_utc,
            "expected_end_utc": settings.expected_end_utc,
            "rows_seen": rows_seen,
            "raw_csv_reference": settings.csv_reference,
            "raw_sha256": digest,
            "quality_passed": not issues,
            "issues": issues,
        }
        warning = publish_report(output, report)
        return RunResult(0 if not issues else 1, output, warning=warning)
    except (OSError, ProcessingError) as exc:
        return RunResult(2, None, str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--instrument-id", required=True)
    parser.add_argument("--source-kind", required=True)
    parser.add_argument("--timeframe-seconds", type=int, required=True)
    parser.add_argument("--expected-start-utc", required=True)
    parser.add_argument("--expected-end-utc", required=True)
    args = parser.parse_args(argv)
    result = run(Settings(args.workspace, args.csv, args.output, args.dataset_id,
                          args.instrument_id, args.source_kind, args.timeframe_seconds,
                          args.expected_start_utc, args.expected_end_utc))
    if result.error:
        print(result.error, file=sys.stderr)
    elif result.report_path:
        print(result.report_path)
    if result.warning:
        print(result.warning, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
