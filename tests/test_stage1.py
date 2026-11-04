"""Independent connection checks for adopted stage 1 specification T01–T08."""
import copy
import csv
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
