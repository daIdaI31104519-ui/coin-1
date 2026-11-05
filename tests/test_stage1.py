"""Independent connection checks for adopted stage 1 specification T01–T08."""
import copy
import csv
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import coin1_stage1 as c

FIXTURE = Path(__file__).parent / "fixtures" / "btc_synthetic_1h.csv"
EXPECTED_NET = Decimal("3.4309966512071067270103266123947")


class Stage1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def run_input(self, input_file=FIXTURE):
        dest = self.base / "outputs"
        path = c.execute_experiment(input_file, dest)
        return path, c.load_record(path)

    def mutate(self, updates):
        with FIXTURE.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            items = list(reader)
            fields = reader.fieldnames
        for index, changes in updates.items():
            items[index].update({key: str(val) for key, val in changes.items()})
        destination = self.base / f"modified_{len(list(self.base.glob('modified_*')))}.csv"
        with destination.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(items)
        return destination

    @staticmethod
    def events(record, event_type):
        return [event for event in record["events"] if event["event_type"] == event_type]

    def test_T01_correct_times_T02_independent_PNL_T03_skip(self):
        path, record = self.run_input()
        self.assertEqual(record["run_status"], "completed")
        self.assertEqual([e["at"] for e in self.events(record, "upper_breakout")],
                         ["2026-01-01T04:00:00Z", "2026-01-01T07:00:00Z"])
        self.assertEqual(self.events(record, "virtual_entry")[0]["at"], "2026-01-01T05:00:00Z")
        self.assertEqual(self.events(record, "virtual_exit")[0]["at"], "2026-01-01T07:00:00Z")
        result = record["result"]
        self.assertEqual(result["decision_count"], 4)
        self.assertEqual(result["closed_trade_count"], 1)
        self.assertEqual(result["signal_skipped_count"], 1)
        trade = result["trades"][0]
        self.assertEqual(Decimal(trade["buy_price"]), Decimal("107.0535"))
        self.assertEqual(Decimal(trade["sell_price"]), Decimal("110.9445"))
        self.assertLess(abs(Decimal(result["net_pnl_usdt"]) - EXPECTED_NET), Decimal("0.000000000001"))
        self.assertEqual(Decimal(result["final_cash_usdt"]).quantize(Decimal("0.0000000001")), Decimal("1003.4309966512"))
        self.assertEqual([e["reason"] for e in self.events(record, "no_trade") if e["at"] == "2026-01-01T07:00:00Z"], ["same_instant_as_exit"])
        self.assertTrue(path.exists())

    def test_T04_invalid_closed_OHLC_rejected(self):
        bad = self.mutate({3: {"high": "107"}})
        with self.assertRaises(c.Stage1Error):
            self.run_input(bad)
        files = list((self.base / "outputs").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = c.load_record(files[0])
        self.assertEqual(record["run_status"], "failed")
        self.assertIsNone(record["result"])
        self.assertEqual(record["errors"][0]["at"], "2026-01-01T03:00:00Z")
        self.assertFalse(self.events(record, "virtual_entry"))

    def test_T05A_future_does_not_change_0400_decision(self):
        _, original = self.run_input()
        changed_data = self.mutate({4: {"high": "121", "close": "119"}})
        _, changed = self.run_input(changed_data)
        def at_0400(r):
            return [(e["event_type"], e.get("close"), e.get("prior_n_high"))
                    for e in r["events"] if e["at"] == "2026-01-01T04:00:00Z"]
        self.assertEqual(at_0400(original), at_0400(changed))
        self.assertNotEqual(original["data"]["sha256"], changed["data"]["sha256"])

    def test_T05B_0700_HLCV_cannot_change_research(self):
        _, original = self.run_input()
        new_input = self.mutate({7: {"high": "130", "low": "110", "close": "126", "volume": "999"}})
        _, changed = self.run_input(new_input)
        self.assertEqual(original["result"], changed["result"])
        def decision_events(r):
            return [(e["event_type"], e["at"], e.get("reason")) for e in r["events"]]
        self.assertEqual(decision_events(original), decision_events(changed))

    def test_future_bad_timestamp_does_not_retroactively_remove_0400_signal(self):
        broken = self.mutate({7: {"open_time_utc": "not-a-timestamp"}})
        with self.assertRaises(c.Stage1Error):
            self.run_input(broken)
        files = list((self.base / "outputs").glob("*.json"))
        record = c.load_record(files[0])
        self.assertEqual(record["run_status"], "failed")
        self.assertEqual(self.events(record, "upper_breakout")[0]["at"], "2026-01-01T04:00:00Z")

    def test_T06_save_failure_never_reported_success(self):
        real_replace = c.os.replace
        count = 0
        def fail_on_completed(src, dst):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Injected disk write failure")
            return real_replace(src, dst)
        with mock.patch.object(c.os, "replace", side_effect=fail_on_completed):
            with self.assertRaises(c.Stage1Error):
                self.run_input()
        files = list((self.base / "outputs").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = c.load_record(files[0])
        self.assertEqual(record["run_status"], "failed")
        self.assertTrue(record["errors"])
        self.assertIsNone(record["result"])

    def test_T07_separate_read_and_provenance(self):
        file, stored = self.run_input()
        loaded = c.load_record(file)
        self.assertEqual(stored, loaded)
        self.assertEqual(loaded["data"]["symbol"], "BTC/USDT")
        self.assertEqual(loaded["data"]["timezone"], "UTC")
        self.assertEqual(len(loaded["data"]["sha256"]), 64)
        self.assertEqual(len(loaded["research"]["source_revision"]["file_sha256"]), 64)
        self.assertEqual(c.cli(["read", "--record", str(file)]), 0)

    def test_T08_incomplete_corrupt_missing_and_duplicate_event(self):
        file, stored = self.run_input()
        running = copy.deepcopy(stored)
        running["run_status"] = "running"
        running["result"] = None
        running["execution"]["ended_at"] = None
        self.assertEqual(c.validate_record(running)["run_status"], "running")
        incomplete = copy.deepcopy(stored)
        incomplete["result"] = None
        with self.assertRaises(c.Stage1Error):
            c.validate_record(incomplete)
        incomplete = copy.deepcopy(stored)
        incomplete.pop("run_id")
        with self.assertRaises(c.Stage1Error):
            c.validate_record(incomplete)
        duplicate = copy.deepcopy(stored)
        duplicate["events"].append(copy.deepcopy(duplicate["events"][0]))
        with self.assertRaises(c.Stage1Error):
            c.validate_record(duplicate)
        file.write_text("{not valid JSON", encoding="utf-8")
        with self.assertRaises(c.Stage1Error):
            c.load_record(file)

    def test_original_completed_record_wont_be_overwritten(self):
        first, saved = self.run_input()
        with self.assertRaises(c.Stage1Error):
            c.execute_experiment(FIXTURE, self.base / "outputs", run_id=saved["run_id"])
        self.assertEqual(c.load_record(first), saved)

    def test_reject_tampered_completed_numbers(self):
        path, original = self.run_input()
        changes = [
            ("result", "net_pnl_usdt", "NaN"),
            ("result", "net_pnl_usdt", "Infinity"),
            ("result", "net_pnl_usdt", "not-a-number"),
            ("result", "net_pnl_usdt", "999999"),
            ("result", "final_cash_usdt", "-5"),
            ("research", "starting_cash_usdt", "900"),
            ("trade", "quantity_btc", "-1"),
            ("trade", "buy_fee_usdt", "Infinity"),
            ("trade", "sell_fee_usdt", "-1"),
            ("trade", "net_pnl_usdt", "999999"),
            ("trade", "sell_price", "1E+999999"),
            ("entry", "quantity_btc", "NaN"),
            ("exit", "net_pnl_usdt", "999999"),
        ]
        for section, key, value in changes:
            with self.subTest(section=section, key=key, value=value):
                record = copy.deepcopy(original)
                if section == "trade":
                    target = record["result"]["trades"][0]
                elif section in ("entry", "exit"):
                    event_type = "virtual_entry" if section == "entry" else "virtual_exit"
                    target = next(event for event in record["events"] if event["event_type"] == event_type)
                else:
                    target = record[section]
                target[key] = value
                path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(c.Stage1Error):
                    c.load_record(path)
        record = copy.deepcopy(original)
        record["result"]["net_pnl_usdt"] = float("nan")
        path.write_text(json.dumps(record, allow_nan=True), encoding="utf-8")
        with self.assertRaises(c.Stage1Error):
            c.load_record(path)

    def test_same_run_id_concurrent_execution_has_one_owner(self):
        first_waiting = threading.Event()
        release_first = threading.Event()
        worker = threading.local()
        real_save = c.atomic_save

        def delayed_save(path, record, **kwargs):
            if record["run_status"] == "running" and worker.name == "first":
                first_waiting.set()
                if not release_first.wait(10):
                    raise TimeoutError("first run was not released")
            return real_save(path, record, **kwargs)

        def attempt(name):
            worker.name = name
            try:
                return c.execute_experiment(FIXTURE, self.base / "same-id", run_id="same_id")
            except c.Stage1Error as exc:
                return exc

        with mock.patch.object(c, "atomic_save", side_effect=delayed_save):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(attempt, "first")
                self.assertTrue(first_waiting.wait(10))
                second = pool.submit(attempt, "second")
                second_result = second.result(timeout=10)
                release_first.set()
                first_result = first.result(timeout=10)
        self.assertIsInstance(first_result, Path)
        self.assertIsInstance(second_result, c.Stage1Error)
        self.assertEqual(c.load_record(first_result)["run_status"], "completed")

    def test_verification_and_failure_save_cannot_leave_completed(self):
        real_save = c.atomic_save

        def fail_failed_save(path, record, **kwargs):
            if record["run_status"] == "failed":
                raise OSError("injected failure-status save error")
            return real_save(path, record, **kwargs)

        with (mock.patch.object(c, "atomic_save", side_effect=fail_failed_save),
              mock.patch.object(c, "load_record", side_effect=c.Stage1Error("injected verification error", stage="record_verify"))):
            with self.assertRaises(c.Stage1Error):
                c.execute_experiment(FIXTURE, self.base / "verify", run_id="verify_fail")
        saved = json.loads((self.base / "verify" / "verify_fail.json").read_text(encoding="utf-8"))
        self.assertNotEqual(saved["run_status"], "completed")

    def test_hash_and_csv_parse_use_same_bytes(self):
        original = FIXTURE.read_bytes()
        changed = original.replace(b"104,110,103,108,16", b"104,111,103,108,16")
        input_path = self.base / "swapped.csv"
        input_path.write_bytes(original)
        real_read = Path.read_bytes

        def swap_after_read(path):
            raw = real_read(path)
            path.write_bytes(changed)
            return raw

        with mock.patch.object(Path, "read_bytes", swap_after_read):
            rows, digest = c.data_rows(input_path)
        self.assertEqual(rows[3]["high"], "110")
        self.assertEqual(digest, hashlib.sha256(original).hexdigest())
        self.assertEqual(input_path.read_bytes(), changed)

    def test_same_id_is_exclusive_across_processes(self):
        output = self.base / "processes"
        marker = self.base / "first_started"
        release = self.base / "release_first"
        script = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import coin1_stage1 as stage
data, output, marker, release = map(Path, sys.argv[2:])
real_save = stage.atomic_save
def delayed_save(path, record, **kwargs):
    if record['run_status'] == 'running':
        marker.touch()
        while not release.exists():
            time.sleep(0.01)
    return real_save(path, record, **kwargs)
stage.atomic_save = delayed_save
stage.execute_experiment(data, output, run_id='process_id')
"""
        arguments = [sys.executable, "-B", "-c", script, str(Path(c.__file__).parent),
                     str(FIXTURE.resolve()), str(output), str(marker), str(release)]
        first = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            second = subprocess.run([sys.executable, "-B", "-c",
                                     "import sys; sys.path.insert(0, sys.argv[1]); import coin1_stage1 as stage; "
                                     "stage.execute_experiment(__import__('pathlib').Path(sys.argv[2]), "
                                     "__import__('pathlib').Path(sys.argv[3]), run_id='process_id')",
                                     str(Path(c.__file__).parent), str(FIXTURE.resolve()), str(output)],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(second.returncode, 0)
        finally:
            release.touch()
            first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(c.load_record(output / "process_id.json")["run_status"], "completed")

    def test_missing_0700_bar_fails_without_virtual_exit(self):
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        input_path = self.base / "seven.csv"
        input_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaises(c.Stage1Error):
            c.execute_experiment(input_path, self.base / "seven", run_id="seven_bars")
        record = c.load_record(self.base / "seven" / "seven_bars.json")
        self.assertEqual(record["run_status"], "failed")
        self.assertIn("exactly eight bars", record["errors"][0]["reason"])
        self.assertFalse(self.events(record, "virtual_exit"))

    def test_interrupted_reservation_is_not_reused(self):
        output = self.base / "interrupted"
        output.mkdir()
        path = output / "stopped.json"
        path.write_bytes(b"")
        with self.assertRaises(c.Stage1Error):
            c.execute_experiment(FIXTURE, output, run_id="stopped")
        with self.assertRaises(c.Stage1Error):
            c.load_record(path)
        c.atomic_save(path, c.new_record("stopped", FIXTURE))
        with self.assertRaises(c.Stage1Error):
            c.execute_experiment(FIXTURE, output, run_id="stopped")
        self.assertEqual(c.load_record(path)["run_status"], "running")

    def test_uncomputed_result_differs_from_calculated_zero(self):
        record = c.new_record("zero_case", FIXTURE)
        self.assertIsNone(c.validate_record(record)["result"])
        record["result"] = {"evaluation_status": "calculated", "decision_count": 0,
                            "upper_breakout_count": 0, "signal_skipped_count": 0,
                            "closed_trade_count": 0, "net_pnl_usdt": "0",
                            "final_cash_usdt": "1000", "trades": [],
                            "as_of": "2026-01-01T07:00:00Z"}
        with self.assertRaises(c.Stage1Error):
            c.validate_record(record)
        record["run_status"] = "completed"
        record["execution"]["ended_at"] = c.utc_now()
        record["data"]["sha256"] = "0" * 64
        self.assertEqual(c.validate_record(record)["result"]["net_pnl_usdt"], "0")

    def test_completed_counts_match_decision_and_trade_events(self):
        path, original = self.run_input()
        for field in ("upper_breakout_count", "decision_count", "signal_skipped_count", "closed_trade_count"):
            with self.subTest(field=field):
                record = copy.deepcopy(original)
                record["result"][field] = 999
                path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(c.Stage1Error):
                    c.load_record(path)
        record = copy.deepcopy(original)
        record["events"] = [event for event in record["events"]
                            if not (event["event_type"] == "upper_breakout"
                                    and event["at"] == "2026-01-01T04:00:00Z")]
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(c.Stage1Error):
            c.load_record(path)
        record = copy.deepcopy(original)
        next(event for event in record["events"]
             if event.get("reason") == "threshold_not_crossed")["reason"] = "same_instant_as_exit"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(c.Stage1Error):
            c.load_record(path)

    def test_completed_market_and_execution_times(self):
        path, original = self.run_input()
        cases = ("future_event", "invalid_as_of", "reversed_trade", "non_utc_start",
                 "reversed_execution", "inverted_data_window", "invalid_last_open", "future_knowledge")
        for case in cases:
            with self.subTest(case=case):
                record = copy.deepcopy(original)
                if case == "future_event":
                    next(event for event in record["events"]
                         if event.get("reason") == "threshold_not_crossed")["at"] = "2099-01-01T00:00:00Z"
                elif case == "invalid_as_of":
                    record["result"]["as_of"] = "not-a-date"
                elif case == "reversed_trade":
                    trade = record["result"]["trades"][0]
                    trade["entered_at"], trade["exited_at"] = trade["exited_at"], trade["entered_at"]
                    next(event for event in record["events"] if event["event_type"] == "virtual_entry")["at"] = trade["entered_at"]
                    next(event for event in record["events"] if event["event_type"] == "virtual_exit")["at"] = trade["exited_at"]
                elif case == "non_utc_start":
                    record["execution"]["started_at"] = "2026-11-05T09:00:00+09:00"
                elif case == "reversed_execution":
                    record["execution"]["ended_at"] = "2000-01-01T00:00:00Z"
                elif case == "inverted_data_window":
                    record["data"]["start_at"] = "2026-01-02T00:00:00Z"
                elif case == "invalid_last_open":
                    record["data"]["last_bar_open_at"] = "2026-01-01T07:00:00+09:00"
                elif case == "future_knowledge":
                    next(event for event in record["events"]
                         if event["event_type"] == "upper_breakout")["knowledge_cutoff"] = "2099-01-01T00:00:00Z"
                path.write_text(json.dumps(record), encoding="utf-8")
                with self.assertRaises(c.Stage1Error):
                    c.load_record(path)


if __name__ == "__main__":
    unittest.main()
