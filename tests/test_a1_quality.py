import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from a1_quality import RunResult, Settings, run
from mock_b import RejectedReport, receive


FIXTURE = (Path(__file__).parent / "fixtures" / "synthetic_btc_1m.csv").read_bytes()
LINES = FIXTURE.decode().splitlines()
START = "2026-10-01T12:00:00Z"
END = "2026-10-01T12:03:00Z"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.csv_path = self.workspace / "input.csv"
        self.csv_path.write_bytes(FIXTURE)
        self.counter = 0

    def settings(self, **changes):
        values = dict(workspace=self.workspace, csv_reference="input.csv",
                      output=self.workspace / f"report-{self.counter}.json",
                      dataset_id="synthetic-btc-1m-001", instrument_id="BTC_SYNTHETIC",
                      source_kind="SYNTHETIC", timeframe_seconds=60,
                      expected_start_utc=START, expected_end_utc=END)
        values.update(changes)
        return Settings(**values)

    def execute(self, raw=None, **changes):
        if raw is not None:
            self.csv_path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
        self.counter += 1
        settings = self.settings(**changes)
        result = run(settings)
        report = json.loads(result.report_path.read_text(encoding="utf-8")) if result.report_path else None
        return result, report, settings

    def invalid(self, raw, code, row_number=None, bar_start=None):
        result, report, _ = self.execute(raw)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(report["quality_passed"])
        matching = [item for item in report["issues"] if item["code"] == code]
        self.assertTrue(matching, report["issues"])
        if row_number is not None:
            self.assertTrue(any(item["row_number"] == row_number for item in matching))
        if bar_start is not None:
            self.assertTrue(any(item["bar_start_utc"] == bar_start for item in matching))
        self.assertEqual(report["raw_sha256"], hashlib.sha256(self.csv_path.read_bytes()).hexdigest())
        return report

    @staticmethod
    def csv_lines(lines):
        return "\n".join(lines) + "\n"

    def test_T01_normal(self):
        before = self.csv_path.read_bytes()
        result, report, _ = self.execute()
        self.assertEqual((result.exit_code, report["quality_passed"], report["rows_seen"], report["issues"]),
                         (0, True, 3, []))
        self.assertEqual(self.csv_path.read_bytes(), before)
        self.assertEqual(report["raw_sha256"], hashlib.sha256(before).hexdigest())

    def test_T02_middle_missing(self):
        self.invalid(self.csv_lines([LINES[0], LINES[1], LINES[3]]), "MISSING_BAR",
                     bar_start="2026-10-01T12:01:00Z")

    def test_T03_duplicate_adjacent_and_nonadjacent(self):
        for rows in ([LINES[1], LINES[2], LINES[2], LINES[3]],
                     [LINES[1], LINES[2], LINES[3], LINES[2]]):
            with self.subTest(rows=rows):
                self.invalid(self.csv_lines([LINES[0], *rows]), "DUPLICATE_BAR", 4 if rows[2] == LINES[2] else 5)

    def test_T04_order(self):
        self.invalid(self.csv_lines([LINES[0], LINES[1], LINES[3], LINES[2]]), "OUT_OF_ORDER", 4)

    def test_T05_ohlc(self):
        self.invalid(self.csv_lines([LINES[0], LINES[1], LINES[2].replace(",106,", ",101,"), LINES[3]]),
                     "INVALID_OHLC", 3)

    def test_T06_price(self):
        for value in ("0", "-1", "NaN"):
            with self.subTest(value=value):
                self.invalid(self.csv_lines([LINES[0], LINES[1].replace(",100,", f",{value},"),
                                             LINES[2], LINES[3]]), "INVALID_PRICE", 2)

    def test_T07_volume(self):
        for value in ("-1", "Infinity"):
            with self.subTest(value=value):
                self.invalid(self.csv_lines([LINES[0], LINES[1], LINES[2][:-1] + value,
                                             LINES[3]]), "INVALID_VOLUME", 3)

    def test_T08_zero_volume(self):
        result, report, _ = self.execute()
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(report["quality_passed"])

    def test_T09_timestamps(self):
        variants = [LINES[1].replace("12:00:00Z", "12:00:00"),
                    LINES[1].replace("12:00:00Z", "12:00:00+09:00"),
                    LINES[1].replace("12:00:00Z", "12:00:01Z"),
                    LINES[1].replace("12:01:02Z", "invalid")]
        for row in variants:
            with self.subTest(row=row):
                self.invalid(self.csv_lines([LINES[0], row, LINES[2], LINES[3]]),
                             "INVALID_TIMESTAMP", 2)

    def test_T10_premature(self):
        self.invalid(self.csv_lines([LINES[0], LINES[1].replace("12:01:02Z", "12:00:59Z"),
                                     LINES[2], LINES[3]]), "PREMATURE_AVAILABILITY", 2)

    def test_T11_edge_missing(self):
        for omitted, stamp in ((1, START), (3, "2026-10-01T12:02:00Z")):
            with self.subTest(omitted=omitted):
                self.invalid(self.csv_lines([line for index, line in enumerate(LINES) if index != omitted]),
                             "MISSING_BAR", bar_start=stamp)

    def test_T12_outside(self):
        extra = "2026-10-01T12:03:00Z,2026-10-01T12:04:02Z,101,103,100,102,1"
        self.invalid(self.csv_lines([*LINES, extra]), "OUT_OF_RANGE", 5)

    def test_T13_schema_variants(self):
        variants = [(self.csv_lines([LINES[0].replace("volume", "vol"), *LINES[1:]]), "SCHEMA_ERROR"),
                    (self.csv_lines([LINES[0]]), "EMPTY_DATASET"),
                    (b"", "SCHEMA_ERROR"),
                    (FIXTURE + b"\xff", "SCHEMA_ERROR"),
                    (b"\xef\xbb\xbf" + FIXTURE, "SCHEMA_ERROR"),
                    (self.csv_lines([LINES[0], LINES[1], "", *LINES[2:]]), "SCHEMA_ERROR"),
                    (self.csv_lines([LINES[0], LINES[1] + ",extra", *LINES[2:]]), "SCHEMA_ERROR")]
        for raw, code in variants:
            with self.subTest(code=code, raw=raw[:30]):
                report = self.invalid(raw, code)
                if code == "EMPTY_DATASET":
                    self.assertEqual(report["rows_seen"], 0)

    def test_T14_invalid_missing_broken_old(self):
        valid_run, _, settings = self.execute()
        bad = self.csv_lines([LINES[0], LINES[1], LINES[3]])
        invalid_run, _, _ = self.execute(bad)
        with self.assertRaises(RejectedReport):
            receive(invalid_run, invalid_run.report_path, self.workspace, settings, START)
        with self.assertRaises(RejectedReport):
            receive(RunResult(0, self.workspace / "missing.json"), self.workspace / "missing.json",
                    self.workspace, settings, START)
        broken = self.workspace / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(RejectedReport):
            receive(RunResult(0, broken), broken, self.workspace, settings, START)
        with self.assertRaises(RejectedReport):
            receive(invalid_run, valid_run.report_path, self.workspace, settings, START)

    def test_T15_hash_metadata_type_and_path(self):
        valid_run, report, settings = self.execute()
        self.csv_path.write_bytes(FIXTURE.replace(b"100,105", b"101,105"))
        with self.assertRaises(RejectedReport):
            receive(valid_run, valid_run.report_path, self.workspace, settings, START)
        self.csv_path.write_bytes(FIXTURE)
        variants = [dict(dataset_id="other"), dict(instrument_id="other"), dict(source_kind="other"),
                    dict(schema_version="other"), dict(validation_rules_version="other"),
                    dict(timeframe_seconds=True), dict(expected_start_utc="other"),
                    dict(rows_seen=True), dict(raw_sha256="bad"), dict(quality_passed="true"),
                    dict(quality_passed=True, issues=[{"code": "INVALID_PRICE"}]),
                    dict(raw_csv_reference="../escape.csv")]
        for changes in variants:
            with self.subTest(changes=changes):
                path = self.workspace / f"tampered-{len(list(self.workspace.glob('tampered-*')))}.json"
                path.write_text(json.dumps({**report, **changes}), encoding="utf-8")
                with self.assertRaises(RejectedReport):
                    receive(RunResult(0, path), path, self.workspace, settings, START)
        escaped = self.workspace / "escaped.json"
        escaped.write_text(json.dumps({**report, "raw_csv_reference": "../outside.csv"}), encoding="utf-8")
        with self.assertRaises(RejectedReport):
            receive(RunResult(0, escaped), escaped, self.workspace,
                    self.settings(csv_reference="../outside.csv"), START)
        missing = dict(report)
        del missing["dataset_id"]
        path = self.workspace / "missing-field.json"
        path.write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaises(RejectedReport):
            receive(RunResult(0, path), path, self.workspace, settings, START)

    def test_T16_decision_times_and_future_invalid(self):
        valid_run, _, settings = self.execute()
        for moment, expected in (("2026-10-01T12:01:30Z", [START]),
                                 ("2026-10-01T12:01:01Z", []),
                                 ("2026-10-01T12:01:02Z", [START])):
            with self.subTest(moment=moment):
                self.assertEqual(receive(valid_run, valid_run.report_path, self.workspace,
                                         settings, moment), expected)
        future_bad = self.csv_lines([LINES[0], LINES[1], LINES[2],
                                     LINES[3].replace(",102,104,", ",0,104,")])
        invalid_run, report, _ = self.execute(future_bad)
        self.assertEqual(invalid_run.exit_code, 1)
        self.assertFalse(report["quality_passed"])
        with self.assertRaises(RejectedReport):
            receive(invalid_run, invalid_run.report_path, self.workspace, settings,
                    "2026-10-01T12:01:30Z")

    def test_T17_reproducibility(self):
        first, report_one, _ = self.execute()
        second, report_two, _ = self.execute()
        self.assertEqual((first.exit_code, report_one["quality_passed"], report_one["issues"], report_one["raw_sha256"]),
                         (second.exit_code, report_two["quality_passed"], report_two["issues"], report_two["raw_sha256"]))

    def test_T18_processing_failures(self):
        valid_run, _, settings = self.execute()
        self.csv_path.unlink()
        absent, _, _ = self.execute()
        self.assertEqual((absent.exit_code, absent.report_path), (2, None))
        self.csv_path.write_bytes(FIXTURE)
        malformed, _, _ = self.execute(expected_end_utc="wrong")
        self.assertEqual((malformed.exit_code, malformed.report_path), (2, None))
        existing, _, _ = self.execute(output=valid_run.report_path)
        self.assertEqual((existing.exit_code, existing.report_path), (2, None))
        with patch("a1_quality.os.link", side_effect=OSError("simulated write failure")):
            failed, _, failed_settings = self.execute()
        self.assertEqual((failed.exit_code, failed.report_path), (2, None))
        self.assertFalse(failed_settings.output.exists())
        for recent in (absent, malformed, existing, failed):
            with self.assertRaises(RejectedReport):
                receive(recent, valid_run.report_path, self.workspace, settings, START)

    def test_published_report_survives_temporary_cleanup_failure(self):
        with patch("a1_quality.Path.unlink", side_effect=OSError("simulated cleanup failure")):
            result, report, settings = self.execute()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.report_path, settings.output.resolve())
        self.assertIn("cleanup failed", result.warning)
        self.assertTrue(settings.output.is_file())
        self.assertTrue(report["quality_passed"])
        self.assertEqual(len(list(self.workspace.glob(".a1-*.tmp"))), 1)
        self.assertEqual(receive(result, result.report_path, self.workspace, settings,
                                 "2026-10-01T12:01:30Z"), [START])

    def test_cleanup_failure_before_publication_remains_processing_failure(self):
        with (patch("a1_quality.os.link", side_effect=OSError("simulated publication failure")),
              patch("a1_quality.Path.unlink", side_effect=OSError("simulated cleanup failure"))):
            result, report, settings = self.execute()
        self.assertEqual((result.exit_code, result.report_path, report), (2, None, None))
        self.assertFalse(settings.output.exists())


if __name__ == "__main__":
    unittest.main()
