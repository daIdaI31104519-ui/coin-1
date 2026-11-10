"""Stage-2 diary checks against the adopted D01–D10 acceptance categories."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import coin1_stage1 as stage1
import research_diary as diary


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "btc_synthetic_1h.csv"
STAGE1_CLI = ROOT / "src" / "coin1_stage1.py"
DIARY_CLI = ROOT / "src" / "research_diary.py"


class ResearchDiaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = tempfile.TemporaryDirectory()
        cls.base = Path(cls.base_dir.name)
        cls.original_path = stage1.execute_experiment(FIXTURE, cls.base / "stage1")
        cls.original = stage1.load_record(cls.original_path)

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.folder = Path(self.workspace.name)

    def save(self, record, name="modified.json"):
        path = self.folder / name
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def run_diary(self, path):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-B", str(DIARY_CLI), "--record", str(path)],
                              capture_output=True, text=True, encoding="utf-8", env=environment, check=False)

    def assert_rejected(self, record, *, stage1_accepts=False):
        path = self.save(record)
        if stage1_accepts:
            self.assertEqual(stage1.load_record(path), record)
        completed = self.run_diary(path)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("ERROR:", completed.stderr)
        self.assertNotIn(str(path), completed.stderr)

    def event(self, record, kind, occurrence=0):
        return [event for event in record["events"] if event["event_type"] == kind][occurrence]

    def test_D01_real_stage1_then_separate_stage2_process(self):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        produced = subprocess.run([sys.executable, "-B", str(STAGE1_CLI), "run", "--data", str(FIXTURE),
                                   "--out-dir", str(self.folder / "outputs")], capture_output=True,
                                  text=True, encoding="utf-8", env=environment, check=False)
        self.assertEqual(produced.returncode, 0, produced.stderr)
        record_path = Path(json.loads(produced.stdout)["record_path"])
        before = record_path.read_bytes()
        described = self.run_diary(record_path)
        self.assertEqual(described.returncode, 0, described.stderr)
        self.assertIn("人工BTC/USDT", described.stdout)
        self.assertIn(json.loads(produced.stdout)["run_id"], described.stdout)
        self.assertIn("状態：正常完了", described.stdout)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(list((self.folder / "outputs").iterdir()), [record_path])

    def test_D02_summary_uses_recorded_exact_values(self):
        output = self.run_diary(self.original_path)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("判断4回／上方向突破2回／シグナル購入見送り1回／仮想決済1回", output.stdout)
        self.assertIn("純損益：3.4309966512071067270103266123947 USDT", output.stdout)
        self.assertIn("最終仮想資金：1003.430996651207106727010326612395 USDT", output.stdout)
        self.assertNotIn("研究優位性を証明", output.stdout)

    def test_D03_all_eight_events_order_and_evidence(self):
        output = self.run_diary(self.original_path)
        self.assertEqual(len(self.original["events"]), 8)
        ids = [event["event_id"] for event in self.original["events"]]
        positions = [output.stdout.index(f"[{event_id}]") for event_id in ids]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(re.findall(r"\[evt_\d+\]", output.stdout)), 8)
        self.assertIn("終値108／過去3本の高値105", output.stdout)
        self.assertIn("翌足の仮想購入予約", output.stdout)
        self.assertIn("突破条件未達", output.stdout)
        self.assertIn("決済優先のため新規購入見送り", output.stdout)
        self.assertLess(output.stdout.index("[evt_006]"), output.stdout.index("[evt_007]"))

    def test_D03_missing_duplicate_and_orphan_reservation(self):
        for mutation in ("missing", "duplicate", "orphan"):
            with self.subTest(mutation=mutation):
                record = copy.deepcopy(self.original)
                reservation = self.event(record, "entry_scheduled")
                if mutation == "missing":
                    record["events"].remove(reservation)
                elif mutation == "duplicate":
                    extra = copy.deepcopy(reservation)
                    extra["event_id"] = "evt_extra"
                    record["events"].insert(record["events"].index(reservation) + 1, extra)
                else:
                    reservation["intended_open_at"] = "2026-01-01T06:00:00Z"
                self.assert_rejected(record, stage1_accepts=True)

    def test_D03_linked_evidence_mismatch(self):
        for kind, field, value in (("entry_scheduled", "close", "109"),
                                   ("entry_scheduled", "prior_n_high", "106"),
                                   ("entry_scheduled", "N", 2),
                                   ("no_trade", "close", "112"),
                                   ("no_trade", "source_bar_start_at", "2026-01-01T05:00:00Z")):
            with self.subTest(kind=kind, field=field):
                record = copy.deepcopy(self.original)
                target = self.event(record, kind, -1)
                target[field] = value
                self.assert_rejected(record, stage1_accepts=field != "source_bar_start_at")

    def test_D03_missing_or_wrong_related_times(self):
        for kind, field, value in (("entry_scheduled", "intended_open_at", None),
                                   ("virtual_entry", "exit_scheduled_at", None),
                                   ("entry_scheduled", "knowledge_cutoff", "2026-01-01T05:00:00Z")):
            with self.subTest(kind=kind, field=field):
                record = copy.deepcopy(self.original)
                target = self.event(record, kind)
                if value is None:
                    del target[field]
                else:
                    target[field] = value
                self.assert_rejected(record, stage1_accepts=value is None)

    def test_D03_decimal_meaning_not_spelling(self):
        record = copy.deepcopy(self.original)
        self.event(record, "entry_scheduled")["close"] = "1.08E+2"
        path = self.save(record)
        self.assertEqual(self.run_diary(path).returncode, 0)

    def test_D03_wrong_entry_and_exit_intervals(self):
        for kind, field, value in (("entry_scheduled", "intended_open_at", "2026-01-01T04:30:00Z"),
                                   ("virtual_entry", "exit_scheduled_at", "2026-01-01T08:00:00Z")):
            with self.subTest(kind=kind):
                record = copy.deepcopy(self.original)
                self.event(record, kind)[field] = value
                self.assert_rejected(record, stage1_accepts=True)

    def test_D03_exit_priority_order_reversed(self):
        record = copy.deepcopy(self.original)
        exit_index = next(i for i, event in enumerate(record["events"]) if event["event_type"] == "virtual_exit")
        record["events"][exit_index], record["events"][exit_index + 1] = record["events"][exit_index + 1], record["events"][exit_index]
        self.assert_rejected(record, stage1_accepts=True)

    def test_D03_exit_priority_cannot_claim_another_skip_reason(self):
        record = copy.deepcopy(self.original)
        self.event(record, "no_trade", -1)["reason"] = "position_already_open"
        self.assert_rejected(record, stage1_accepts=True)

    def test_D04_utc_jst_rollover_and_reproducibility(self):
        first = self.run_diary(self.original_path)
        second = self.run_diary(self.original_path)
        self.assertEqual(first.stdout, second.stdout)
        for phrase in ("2026-01-01 13:00 JST (04:00 UTC", "2026-01-01 14:00 JST (05:00 UTC",
                       "2026-01-01 16:00 JST (07:00 UTC", "プログラム実行開始", "評価基準（市場時刻）"):
            self.assertIn(phrase, first.stdout)
        self.assertEqual(diary.timestamp("2026-01-01T18:00:00Z"),
                         "2026-01-02 03:00 JST (18:00 UTC; 元: 2026-01-01T18:00:00Z)")
        diary.render_diary(diary.extract_facts(self.original)).encode("cp932")
        if os.name == "nt":
            environment = dict(os.environ, PYTHONUTF8="0")
            environment.pop("PYTHONIOENCODING", None)
            default_encoding = subprocess.run([sys.executable, "-B", str(DIARY_CLI), "--record",
                                               str(self.original_path)], capture_output=True,
                                              env=environment, check=False)
            self.assertEqual(default_encoding.returncode, 0, default_encoding.stderr)

    def test_D04_control_characters_in_utc_are_rejected_by_cli(self):
        for label, separator in (("escape", "\x1b"), ("newline", "\n"),
                                 ("bidi", "\u202e")):
            with self.subTest(label=label):
                record = copy.deepcopy(self.original)
                record["execution"]["started_at"] = record["execution"]["started_at"].replace("T", separator, 1)
                path = self.save(record)
                self.assertEqual(stage1.load_record(path), record)
                with self.assertRaises(diary.DiaryError):
                    diary.timestamp(record["execution"]["started_at"])
                output = self.run_diary(path)
                self.assertEqual(output.returncode, 2)
                self.assertEqual(output.stdout, "")
                self.assertIn("ERROR:", output.stderr)
                self.assertNotIn(separator, output.stderr.rstrip("\n"))

    def test_D04_old_or_unclosed_source_bar(self):
        for changed in ("2026-01-01T02:00:00Z", "2026-01-01T04:00:00Z"):
            with self.subTest(changed=changed):
                record = copy.deepcopy(self.original)
                self.event(record, "upper_breakout")["source_bar_start_at"] = changed
                self.assert_rejected(record, stage1_accepts=changed.endswith("02:00:00Z"))

    def test_D05_failed_with_safe_reasons_and_missing_reason(self):
        bad_csv = self.folder / "bad.csv"
        bad_csv.write_bytes(FIXTURE.read_bytes().replace(b"104,110,103,108,16", b"104,107,103,108,16"))
        with self.assertRaises(stage1.Stage1Error):
            stage1.execute_experiment(bad_csv, self.folder / "failed")
        failed_path = next((self.folder / "failed").glob("*.json"))
        output = self.run_diary(failed_path)
        self.assertEqual(output.returncode, 1, output.stderr)
        self.assertIn("実験失敗・最終評価なし", output.stdout)
        self.assertNotIn("純損益：", output.stdout)
        record = stage1.load_record(failed_path)
        for error in ({"stage": "quality", "reason": "bad\x1b[31m\n\u202e"},
                      {"stage": "quality", "reason": "A" * 250},
                      {"stage": "quality", "reason": "C:\\Users\\secret\\file.json"},
                      {"stage": "quality"}, "not-an-object"):
            with self.subTest(error=error):
                changed = copy.deepcopy(record)
                changed["errors"] = [error]
                result = self.run_diary(self.save(changed))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertNotIn("純損益：", result.stdout)
                self.assertNotIn("\x1b", result.stdout)
                self.assertNotIn("\u202e", result.stdout)
                self.assertNotIn("C:\\Users\\secret", result.stdout)
                if isinstance(error, dict) and "\x1b" in error.get("reason", ""):
                    self.assertIn("\\u001b", result.stdout)
                    self.assertIn("\\u000a", result.stdout)
                    self.assertIn("\\u202e", result.stdout)
                self.assertTrue("理由の記録を確認できない" in result.stdout or "失敗理由：" in result.stdout)

    def test_D06_running_is_not_zero_pnl(self):
        running = stage1.new_record("run_incomplete", FIXTURE)
        result = self.run_diary(self.save(running))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("未完了（実行中か中断した可能性がある）", result.stdout)
        self.assertNotIn("純損益：0", result.stdout)
        self.assertNotIn("状態：正常完了", result.stdout)

    def test_D07_extra_evidence_missing_invalid_or_false_signal(self):
        for field, value in (("close", None), ("close", "not-a-number"),
                             ("close", "Infinity"), ("close", "108\n\x1b[31m"),
                             ("prior_n_high", "109")):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(self.original)
                target = self.event(record, "upper_breakout")
                if value is None:
                    del target[field]
                else:
                    target[field] = value
                self.assert_rejected(record, stage1_accepts=True)

    def test_D07_displayed_result_number_cannot_contain_controls(self):
        record = copy.deepcopy(self.original)
        record["result"]["net_pnl_usdt"] += "\n"
        self.assert_rejected(record)

    def test_D07_profile_and_entry_wait_bool(self):
        cases = (("research", "entry_wait_bars", True),
                 ("data", "symbol", "ETH/USDT"), ("data", "timeframe", "5m"),
                 ("data", "timezone", "JST"), ("data", "market", "live"),
                 ("data", "price_unit", "BTC"), ("data", "volume_unit", "BTC"),
                 ("data", "source", "exchange"))
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                record = copy.deepcopy(self.original)
                record[section][field] = value
                self.assert_rejected(record, stage1_accepts=True)

    def test_D08_unsupported_version_research_event_reasons(self):
        cases = (("record", "record_version", "0.2"),
                 ("research", "name", "other_method"),
                 ("research", "version", "0.2"),
                 ("event", "event_type", "other_event"),
                 ("exit", "close_reason", "other_reason"),
                 ("exit", "fill_model", "other_model"),
                 ("skip", "reason", "other_reason"))
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                record = copy.deepcopy(self.original)
                target = (record if section == "record" else record["research"] if section == "research"
                          else self.event(record, "virtual_exit") if section == "exit"
                          else self.event(record, "no_trade", -1) if section == "skip"
                          else record["events"][-1])
                target[field] = value
                self.assert_rejected(record)

    def test_D09_identifier_length_controls_and_original_bytes(self):
        before = self.original_path.read_bytes()
        result = self.run_diary(self.original_path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, self.original_path.read_bytes())
        for field, value in (("run_id", "a" * 200 + "X"),
                             ("run_id", "a" * 200 + "Y"),
                             ("run_id", "bad\nname"),
                             ("event_id", "a" * 200 + "X"),
                             ("event_id", "a" * 200 + "Y"),
                             ("event_id", "bad\u202e")):
            with self.subTest(field=field, value=value[-5:]):
                record = copy.deepcopy(self.original)
                if field == "run_id":
                    record["run_id"] = value
                    for event in record["events"]:
                        event["run_id"] = value
                else:
                    record["events"][0]["event_id"] = value
                self.assert_rejected(record, stage1_accepts=True)

    def test_D09_corrupt_missing_and_nonfinite_json_no_path_leak(self):
        missing = self.run_diary(self.folder / "private" / "missing.json")
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")
        self.assertNotIn(str(self.folder), missing.stderr)
        for name, contents in (("broken.json", "{"), ("nonfinite.json", '{"bad": NaN}')):
            path = self.folder / name
            path.write_text(contents, encoding="utf-8")
            output = self.run_diary(path)
            self.assertEqual(output.returncode, 2)
            self.assertEqual(output.stdout, "")
            self.assertNotIn(str(path), output.stderr)

    def test_D09_failed_reason_absolute_paths_are_redacted(self):
        reasons = (
            "/home/alice/secret/coins.json",
            "入力失敗 /home/alice/secret/coins.json",
            "入力:/home/alice/secret/coins.json",
            "入力(/home/alice/secret/coins.json)",
            "C:\\Users\\alice\\secret\\coins.json",
            "入力失敗 C:\\Users\\alice\\secret\\coins.json",
            "入力:C:\\Users\\alice\\secret\\coins.json",
            "入力(C:\\Users\\alice\\secret\\coins.json)",
        )
        for reason in reasons:
            with self.subTest(reason=reason):
                record = stage1.new_record("run_failed", FIXTURE)
                record["run_status"] = "failed"
                record["errors"] = [{"stage": "input", "reason": reason}]
                path = self.save(record)
                before = path.read_bytes()
                self.assertEqual(stage1.load_record(path), record)
                self.assertEqual(diary.safe_text(reason), "パスを含むため詳細非表示")
                output = self.run_diary(path)
                self.assertEqual(output.returncode, 1, output.stderr)
                self.assertEqual(output.stderr, "")
                self.assertIn("パスを含むため詳細非表示", output.stdout)
                self.assertNotIn(reason.split("/")[-1], output.stdout)
                self.assertNotIn("C:\\Users", output.stdout)
                self.assertEqual(hashlib.sha256(before).digest(), hashlib.sha256(path.read_bytes()).digest())
                self.assertEqual(before, path.read_bytes())

    def test_D09_failed_reason_adjacent_japanese_and_file_uri_paths(self):
        for reason in ("入力は/home/alice/secret.json", "file:///home/alice/secret.json"):
            with self.subTest(reason=reason):
                record = stage1.new_record("run_failed", FIXTURE)
                record["run_status"] = "failed"
                record["errors"] = [{"stage": "input", "reason": reason}]
                path = self.save(record)
                before = path.read_bytes()
                self.assertEqual(stage1.load_record(path), record)
                self.assertEqual(diary.safe_text(reason), "パスを含むため詳細非表示")
                output = self.run_diary(path)
                self.assertEqual(output.returncode, 1, output.stderr)
                self.assertEqual(output.stderr, "")
                self.assertIn("パスを含むため詳細非表示", output.stdout)
                self.assertNotIn("/home/alice/secret.json", output.stdout)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(hashlib.sha256(before).digest(), hashlib.sha256(path.read_bytes()).digest())

    def test_D09_failed_reason_normal_and_control_text_preserved_safely(self):
        normal_reasons = ("人工価格の整合検査に失敗", "通常の比率 1/2", "参照 https://example.com/help")
        reasons = (*normal_reasons, "bad\x1b[31m\n\u202e", "A" * 250)
        for reason in reasons:
            with self.subTest(reason=reason[:20]):
                record = stage1.new_record("run_failed", FIXTURE)
                record["run_status"] = "failed"
                record["errors"] = [{"stage": "input", "reason": reason}]
                path = self.save(record)
                output = self.run_diary(path)
                self.assertEqual(output.returncode, 1, output.stderr)
                self.assertEqual(output.stderr, "")
                self.assertIn(diary.safe_text(reason), output.stdout)
                if reason in normal_reasons:
                    self.assertEqual(diary.safe_text(reason), reason)
                self.assertNotIn("\x1b", output.stdout)
                self.assertNotIn("\u202e", output.stdout)
                if len(reason) > 200:
                    self.assertNotIn(reason, output.stdout)


if __name__ == "__main__":
    unittest.main()
