"""Read a stage-1 synthetic record and render a deterministic Japanese diary."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
import unicodedata

from coin1_stage1 import Stage1Error, load_record


HOUR = timedelta(hours=1)
JST = timezone(timedelta(hours=9))
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
DECIMAL_STRING = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
PROFILE = {
    "source": "synthetic_fixture",
    "market": "synthetic_spot_reference",
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "timezone": "UTC",
    "price_unit": "USDT",
    "volume_unit": "synthetic_BTC",
}
EVIDENCE = ("source_bar_start_at", "knowledge_cutoff", "N", "close", "prior_n_high")
SKIP_REASONS = {"same_instant_as_exit", "position_already_open", "entry_pending"}
NO_TRADE_LABELS = {
    "threshold_not_crossed": "突破条件未達",
    "same_instant_as_exit": "決済優先のため新規購入見送り",
    "position_already_open": "保有中との記録により新規購入見送り（保有状態は独立未検証）",
    "entry_pending": "購入予約済みとの記録により新規購入見送り（予約状態は独立未検証）",
}


class DiaryError(Exception):
    """The record cannot be safely rendered as a supported diary."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DiaryError(message)


def identifier(value: object) -> str:
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None, "識別子が不正です")
    return value


def utc_time(value: object) -> datetime:
    require(isinstance(value, str) and UTC_TIMESTAMP.fullmatch(value) is not None, "UTC時刻が不正です")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise DiaryError("UTC時刻が不正です") from exc
    require(parsed.utcoffset() == timedelta(0), "UTC時刻が不正です")
    return parsed.astimezone(timezone.utc)


def decimal_text(value: object, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    require(isinstance(value, str) and DECIMAL_STRING.fullmatch(value) is not None,
            "十進数文字列が不正です")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise DiaryError("十進数文字列が不正です") from exc
    require(amount.is_finite(), "有限でない数値です")
    require(not positive or amount > 0, "正の価格・数量が必要です")
    require(not nonnegative or amount >= 0, "負の手数料です")
    return amount


def safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?![\\/])|\\\\|(?i:file:///)|(?<![A-Za-z0-9/])/(?!/)\S+", value):
        return "パスを含むため詳細非表示"
    escaped = []
    for character in value:
        code = ord(character)
        if (unicodedata.category(character) in ("Cc", "Cf")
                or 0x80 <= code <= 0x9F or code in (0x2028, 0x2029)):
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(character)
    result = "".join(escaped)
    return result[:200] + "…" if len(result) > 200 else result


def timestamp(value: str) -> str:
    instant = utc_time(value)
    return f"{instant.astimezone(JST):%Y-%m-%d %H:%M} JST ({instant:%H:%M} UTC; 元: {value})"


def validate_profile(record: dict) -> None:
    identifier(record["run_id"])
    require(record["record_version"] == "0.1", "未対応の記録版です")
    research = record["research"]
    require(research.get("name") == "breakout_connection_test"
            and research.get("version") == "0.1"
            and research.get("simulation_only") is True
            and type(research.get("entry_wait_bars")) is int
            and research["entry_wait_bars"] == 1
            and research.get("same_instant_policy") == "exit_before_signal; signal_recorded_but_no_reentry",
            "未対応の研究プロフィールです")
    require(all(record["data"].get(field) == expected for field, expected in PROFILE.items()),
            "未対応の入力プロフィールです")
    utc_time(record["execution"]["started_at"])
    if record["execution"].get("ended_at") is not None:
        utc_time(record["execution"]["ended_at"])


def evidence(event: dict, research_n: int) -> tuple:
    require(all(field in event for field in EVIDENCE), "判断根拠が不足しています")
    source = utc_time(event["source_bar_start_at"])
    cutoff = utc_time(event["knowledge_cutoff"])
    instant = utc_time(event["at"])
    require(source + HOUR == cutoff == instant, "直前確定足と判断時刻が不整合です")
    require(type(event["N"]) is int and event["N"] == research_n, "判断期間が不整合です")
    close = decimal_text(event["close"], positive=True)
    prior = decimal_text(event["prior_n_high"], positive=True)
    if event["event_type"] == "upper_breakout":
        require(close > prior, "突破根拠が不整合です")
    elif event["event_type"] == "no_trade" and event.get("reason") == "threshold_not_crossed":
        require(close <= prior, "条件未達の根拠が不整合です")
    return source, cutoff, event["N"], close, prior


def validate_completed(record: dict) -> None:
    data = record["data"]
    result = record["result"]
    require(data.get("start_at") is not None and data.get("last_bar_open_at") is not None,
            "対象期間が不足しています")
    utc_time(data["start_at"])
    utc_time(data["last_bar_open_at"])
    utc_time(result["as_of"])
    decimal_text(result["net_pnl_usdt"])
    decimal_text(result["final_cash_usdt"])
    events = record["events"]
    exit_times = {utc_time(event["at"]) for event in events if event.get("event_type") == "virtual_exit"}
    seen_decisions: set[datetime] = set()
    reservations: dict[datetime, dict] = {}
    entries: dict[datetime, dict] = {}
    for index, event in enumerate(events):
        identifier(event["event_id"])
        require(type(event.get("event_type")) is str, "未知のイベントです")
        event_type = event["event_type"]
        instant = utc_time(event["at"])
        if event_type in ("upper_breakout", "entry_scheduled", "no_trade"):
            current_evidence = evidence(event, record["research"]["N"])
        if event_type == "upper_breakout":
            require(instant not in seen_decisions, "判断が重複しています")
            seen_decisions.add(instant)
            require(index + 1 < len(events), "突破に対応する後続イベントがありません")
            child = events[index + 1]
            require(child.get("event_type") in ("entry_scheduled", "no_trade")
                    and utc_time(child.get("at")) == instant, "突破の後続イベントが不整合です")
            if instant in exit_times:
                require(index > 0 and events[index - 1].get("event_type") == "virtual_exit"
                        and utc_time(events[index - 1]["at"]) == instant
                        and child.get("event_type") == "no_trade"
                        and child.get("reason") == "same_instant_as_exit",
                        "同時刻の決済優先規則が不整合です")
        elif event_type == "entry_scheduled":
            require(index > 0 and events[index - 1].get("event_type") == "upper_breakout"
                    and utc_time(events[index - 1]["at"]) == instant
                    and evidence(events[index - 1], record["research"]["N"]) == current_evidence,
                    "購入予約と突破の根拠が不整合です")
            intended = utc_time(event.get("intended_open_at"))
            require(intended == instant + HOUR and intended not in reservations,
                    "購入予約時刻が不整合です")
            reservations[intended] = event
        elif event_type == "no_trade":
            reason = event.get("reason")
            require(type(reason) is str and reason in NO_TRADE_LABELS, "未知の見送り理由です")
            if reason == "threshold_not_crossed":
                require(instant not in seen_decisions, "条件未達と突破が重複しています")
                seen_decisions.add(instant)
            else:
                require(index > 0 and events[index - 1].get("event_type") == "upper_breakout"
                        and utc_time(events[index - 1]["at"]) == instant
                        and evidence(events[index - 1], record["research"]["N"]) == current_evidence,
                        "見送りと突破の根拠が不整合です")
                if reason == "same_instant_as_exit":
                    require(index > 1 and events[index - 2].get("event_type") == "virtual_exit"
                            and utc_time(events[index - 2]["at"]) == instant,
                            "同時刻の決済優先順が不整合です")
        elif event_type == "virtual_entry":
            for field in ("reference_open", "buy_price", "quantity_btc"):
                decimal_text(event.get(field), positive=True)
            decimal_text(event.get("buy_fee_usdt"), nonnegative=True)
            require(instant in reservations, "購入に対応する先行予約がありません")
            reservations.pop(instant)
            scheduled_exit = utc_time(event.get("exit_scheduled_at"))
            require(scheduled_exit == instant + 2 * HOUR and scheduled_exit not in entries,
                    "仮想決済予定時刻が不整合です")
            entries[scheduled_exit] = event
        elif event_type == "virtual_exit":
            for field in ("sell_price",):
                decimal_text(event.get(field), positive=True)
            decimal_text(event.get("sell_fee_usdt"), nonnegative=True)
            decimal_text(event.get("net_pnl_usdt"))
            require(event.get("close_reason") == "fixed_two_hour_holding"
                    and event.get("fill_model") == "next_bar_open_plus_adverse_slippage",
                    "未対応の決済理由または約定モデルです")
            require(instant in entries, "決済に対応する購入がありません")
            entry = entries.pop(instant)
            require(utc_time(event.get("entered_at")) == utc_time(entry["at"])
                    and utc_time(event.get("exited_at")) == instant,
                    "購入・決済時刻が不整合です")
        else:
            raise DiaryError("未知のイベントです")
    require(not reservations and not entries, "予約または仮想取引が孤立しています")


def extract_facts(record: dict) -> dict:
    validate_profile(record)
    if record["run_status"] == "completed":
        validate_completed(record)
    facts = {key: record[key] for key in
             ("record_version", "run_id", "run_status", "execution", "data", "research", "events", "result", "errors")}
    facts["events"] = tuple(record["events"])
    return facts


def render_diary(record: dict) -> str:
    status = record["run_status"]
    lines = ["coin-1 日本語研究日記：実装①の記録",
             f"実験ID：{record['run_id']}",
             f"記録版：{record['record_version']}／研究：{record['research']['name']} v{record['research']['version']}",
             f"記録状態：{status}",
             "データ：人工BTC/USDT・1時間足（synthetic_fixture／synthetic_spot_reference）",
             f"プログラム実行開始：{timestamp(record['execution']['started_at'])}"]
    if record["execution"].get("ended_at") is not None:
        lines.append(f"プログラム実行終了：{timestamp(record['execution']['ended_at'])}")
    if status == "running":
        lines.extend(("状態：未完了（実行中か中断した可能性がある）", "最終評価・最終損益：なし"))
        return "\n".join(lines) + "\n"
    if status == "failed":
        lines.extend(("状態：実験失敗・最終評価なし", "最終損益：なし"))
        if record["errors"]:
            for error in record["errors"]:
                if not isinstance(error, dict) or not isinstance(error.get("stage"), str) or not isinstance(error.get("reason"), str):
                    lines.append("失敗理由：理由の記録を確認できない")
                else:
                    lines.append(f"失敗理由：{safe_text(error['stage'])}／{safe_text(error['reason'])}")
        else:
            lines.append("失敗理由：理由の記録を確認できない")
        return "\n".join(lines) + "\n"
    result = record["result"]
    lines.extend(("状態：正常完了（研究優位性の証明ではない）",
                  f"対象期間（市場時刻）：{timestamp(record['data']['start_at'])} ～ {timestamp(record['data']['last_bar_open_at'])}",
                  f"評価基準（市場時刻）：{timestamp(result['as_of'])}（開始足の全OHLCV確定を意味しない）",
                  f"集計：判断{result['decision_count']}回／上方向突破{result['upper_breakout_count']}回／シグナル購入見送り{result['signal_skipped_count']}回／仮想決済{result['closed_trade_count']}回",
                  "時系列（市場時刻・原本順）："))
    for event in record["events"]:
        kind = event["event_type"]
        prefix = f"{timestamp(event['at'])} [{event['event_id']}] "
        if kind in ("upper_breakout", "entry_scheduled", "no_trade"):
            source = timestamp(event["source_bar_start_at"])
            cutoff = timestamp(event["knowledge_cutoff"])
            details = (f"根拠：確定足開始{source}／判断時点{cutoff}／終値{event['close']}"
                       f"／過去{event['N']}本の高値{event['prior_n_high']}")
            if kind == "upper_breakout":
                description = "上方向突破"
            elif kind == "entry_scheduled":
                description = f"翌足の仮想購入予約（予定：{timestamp(event['intended_open_at'])}）"
            else:
                description = NO_TRADE_LABELS[event["reason"]]
            lines.append(prefix + description + "。" + details)
        elif kind == "virtual_entry":
            lines.append(prefix + f"仮想購入：基準始値{event['reference_open']}／購入価格{event['buy_price']} USDT"
                         f"／数量{event['quantity_btc']} BTC／購入手数料{event['buy_fee_usdt']} USDT"
                         f"／予定決済{timestamp(event['exit_scheduled_at'])}")
        else:
            lines.append(prefix + f"仮想決済：購入{timestamp(event['entered_at'])}／決済{timestamp(event['exited_at'])}"
                         f"／売却価格{event['sell_price']} USDT／売却手数料{event['sell_fee_usdt']} USDT"
                         f"／純損益{event['net_pnl_usdt']} USDT／理由：2時間の保有期間終了"
                         "／約定モデル：翌足始値と不利な価格補正")
    lines.extend((f"純損益：{result['net_pnl_usdt']} USDT（モデル上の仮想損益）",
                  f"最終仮想資金：{result['final_cash_usdt']} USDT",
                  "注意：人工データ／履歴モデルの仮想約定／手数料・価格補正の仮定／実取引なし／実市場での収益性・約定可能性は未検証"))
    return "\n".join(lines) + "\n"


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="実装①JSONから日本語研究日記を表示")
    parser.add_argument("--record", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        record = extract_facts(load_record(args.record))
        body = render_diary(record)
        sys.stdout.write(body)
        sys.stdout.flush()
        return 0 if record["run_status"] == "completed" else 1
    except (Stage1Error, DiaryError, KeyError, TypeError, ValueError) as exc:
        category = "読取・基本検証" if isinstance(exc, Stage1Error) else "日記表示検証"
        print(f"ERROR: {category}に失敗しました。入力記録を確認してください。", file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        try:
            print("ERROR: 出力または文字コード処理に失敗しました。", file=sys.stderr)
        except (OSError, UnicodeError):
            pass
        return 2


if __name__ == "__main__":
    sys.exit(cli())
