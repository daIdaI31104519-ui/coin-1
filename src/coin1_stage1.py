"""coin-1 implementation 1: synthetic BTC research -> replayable JSON.

Scope: the adopted `設計/実装01_最小作業仕様.md` v0.1 only.
No exchange connectivity, real trades, Telegram, diary or live monitoring.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import uuid

RECORD_VERSION = "0.1"
ONE_HOUR = timedelta(hours=1)
ZERO = Decimal("0")
ONE = Decimal("1")
N = 3
H_HOURS = 2
STARTING_CASH = Decimal("1000")
BUY_NOTIONAL = Decimal("100")
FEE_RATE = Decimal("0.001")
SLIPPAGE_RATE = Decimal("0.0005")
HEADERS = ("open_time_utc", "open", "high", "low", "close", "volume")


class Stage1Error(Exception):
    """Failure with a stage and the as-of instant, if available."""

    def __init__(self, message: str, *, stage: str, at: str | None = None):
        super().__init__(message)
        self.stage = stage
        self.at = at


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_now() -> str:
    return iso(datetime.now(timezone.utc))


def as_utc(s: str) -> datetime:
    try:
        result = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise Stage1Error("Invalid UTC candle timestamp", stage="input_timestamp", at=s) from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise Stage1Error("Candle timestamps must explicitly be UTC", stage="input_timestamp", at=s)
    return result.astimezone(timezone.utc)


def number(value: str, *, label: str, instant: datetime) -> Decimal:
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Stage1Error(f"Invalid {label}: {value!r}", stage="data_quality", at=iso(instant)) from exc
    if not d.is_finite():
        raise Stage1Error(f"Nonfinite {label}", stage="data_quality", at=iso(instant))
    return d


def data_rows(filename: Path) -> tuple[list[dict], str]:
    raw = filename.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != list(HEADERS):
            raise Stage1Error(f"Expected CSV columns: {', '.join(HEADERS)}", stage="input_format")
        entries = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise Stage1Error("Invalid CSV input", stage="input_format") from exc
    if not entries:
        raise Stage1Error("Empty input", stage="input_format")
    # No as-of validation here: even future timestamps are parsed only when replay reaches them.
    return entries, sha


def validate_closed(row: dict) -> dict[str, Decimal]:
    """Call only at or after row open_time + one hour."""
    t = row["_time"]
    vals = {k: number(row[k], label=k, instant=t) for k in HEADERS[1:]}
    if any(vals[k] <= ZERO for k in ("open", "high", "low", "close")):
        raise Stage1Error("OHLC prices must be positive", stage="data_quality", at=iso(t))
    if vals["volume"] < ZERO:
        raise Stage1Error("Volume must be nonnegative", stage="data_quality", at=iso(t))
    if vals["high"] < max(vals["open"], vals["close"]) or vals["low"] > min(vals["open"], vals["close"]) or vals["low"] > vals["high"]:
        raise Stage1Error("OHLC contradiction", stage="data_quality", at=iso(t))
    return vals


def open_only(row: dict) -> Decimal:
    """At the bar opening instant its OHLCV close/high/low/volume aren't known."""
    result = number(row["open"], label="open", instant=row["_time"])
    if result <= ZERO:
        raise Stage1Error("Open price must be positive", stage="data_quality", at=iso(row["_time"]))
    return result


def source_revision() -> dict:
    return {
        "file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "git_commit": None,  # Unknown until a reproducible Git commit is verified.
        "git_status": "unverified",
    }


def new_record(run_id: str, filename: Path) -> dict:
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "run_status": "running",
        "execution": {"started_at": utc_now(), "ended_at": None, "python": platform.python_version()},
        "data": {"source": "synthetic_fixture", "market": "synthetic_spot_reference", "symbol": "BTC/USDT", "timeframe": "1h", "timezone": "UTC", "price_unit": "USDT", "volume_unit": "synthetic_BTC", "input_path": str(filename), "sha256": None, "start_at": None, "last_bar_open_at": None,
                 "availability_assumption": "Closed candle available at its UTC hour end; next bar open used for hypothetical fill. Actual delivery timing not verified."},
        "research": {"name": "breakout_connection_test", "version": "0.1", "N": N, "H_hours": H_HOURS, "starting_cash_usdt": str(STARTING_CASH), "buy_notional_usdt": str(BUY_NOTIONAL), "fee_rate_each_side": str(FEE_RATE), "adverse_price_adjustment_each_side": str(SLIPPAGE_RATE), "entry_wait_bars": 1,
                     "same_instant_policy": "exit_before_signal; signal_recorded_but_no_reentry", "simulation_only": True, "source_revision": source_revision()},
        "events": [], "result": None, "errors": [],
    }


def record_event(record: dict, event_type: str, at: datetime | str, **details) -> None:
    record["events"].append({"event_id": f"evt_{len(record['events'])+1:03d}", "run_id": record["run_id"], "event_type": event_type, "at": iso(at) if isinstance(at, datetime) else at, **details})


def simulate(rows: list[dict], record: dict) -> None:
    """Replay 00:00...07:00 chronologically. Never inspect an open candle's H/L/C/V."""
    if len(rows) != 8:
        raise Stage1Error("Connection fixture must contain exactly eight bars", stage="input_format")
    validated: list[dict[str, Decimal]] = []
    pending: datetime | None = None
    position: dict | None = None
    decision_count = 0
    signal_count = 0
    skipped = 0
    closed_trades: list[dict] = []
    with localcontext() as context:
        context.prec = 34
        for index, bar in enumerate(rows):
            t = as_utc(bar["open_time_utc"])
            bar["_time"] = t
            if index == 0:
                record["data"]["start_at"] = iso(t)
            record["data"]["last_bar_open_at"] = iso(t)
            if index and t - rows[index-1]["_time"] != ONE_HOUR:
                raise Stage1Error("Candle timestamps must increase by exactly one hour", stage="input_timestamp", at=iso(t))

            # Opening bar's OPEN is available at instant t (for modelled fills only).
            today_open = open_only(bar)

            # The previous bar can now be closed and checked. No global future quality gate.
            if index:
                previous = validate_closed(rows[index - 1])
                validated.append(previous)

            exited_at_t = False
            if position is not None and t == position["exit_at"]:
                sell_price = today_open * (ONE - SLIPPAGE_RATE)
                gross = position["quantity"] * sell_price
                sell_fee = gross * FEE_RATE
                net = gross - sell_fee - BUY_NOTIONAL - position["buy_fee"]
                position_result = {
                    "entered_at": iso(position["entered_at"]), "exited_at": iso(t),
                    "buy_price": str(position["buy_price"]), "quantity_btc": str(position["quantity"]),
                    "buy_fee_usdt": str(position["buy_fee"]), "sell_price": str(sell_price),
                    "sell_fee_usdt": str(sell_fee), "net_pnl_usdt": str(net),
                    "close_reason": "fixed_two_hour_holding", "fill_model": "next_bar_open_plus_adverse_slippage",
                }
                closed_trades.append(position_result)
                record_event(record, "virtual_exit", t, **position_result)
                position = None
                exited_at_t = True

            if pending is not None and t == pending:
                if position is not None:
                    raise Stage1Error("Pending entry while position open", stage="internal_state", at=iso(t))
                price = today_open * (ONE + SLIPPAGE_RATE)
                qty = BUY_NOTIONAL / price
                position = {"entered_at": t, "exit_at": t + H_HOURS * ONE_HOUR,
                            "buy_price": price, "quantity": qty, "buy_fee": BUY_NOTIONAL * FEE_RATE}
                record_event(record, "virtual_entry", t, reference_open=str(today_open),
                             buy_price=str(price), quantity_btc=str(qty), buy_fee_usdt=str(position["buy_fee"]),
                             exit_scheduled_at=iso(position["exit_at"]))
                pending = None

            # A signal at t comes from the candle that closed at t (index-1).
            if index >= N + 1:
                decision_count += 1
                closed = validated[-1]
                prior_high = max(v["high"] for v in validated[-N-1:-1])
                close = closed["close"]
                source_start = iso(rows[index - 1]["_time"])
                decision = {"source_bar_start_at": source_start, "knowledge_cutoff": iso(t),
                            "close": str(close), "prior_n_high": str(prior_high), "N": N}
                if close > prior_high:
                    signal_count += 1
                    record_event(record, "upper_breakout", t, **decision)
                    if exited_at_t:
                        skipped += 1
                        record_event(record, "no_trade", t, reason="same_instant_as_exit", **decision)
                    elif position is not None:
                        skipped += 1
                        record_event(record, "no_trade", t, reason="position_already_open", **decision)
                    elif pending is not None:
                        skipped += 1
                        record_event(record, "no_trade", t, reason="entry_pending", **decision)
                    else:
                        pending = t + ONE_HOUR
                        record_event(record, "entry_scheduled", t, intended_open_at=iso(pending), **decision)
                else:
                    record_event(record, "no_trade", t, reason="threshold_not_crossed", **decision)

        if position is not None or pending is not None:
            raise Stage1Error("Fixture ends with unresolved simulated position or entry", stage="simulation", at=iso(rows[-1]["_time"]))
        pnl = sum((Decimal(item["net_pnl_usdt"]) for item in closed_trades), ZERO)
        record["result"] = {
            "evaluation_status": "calculated", "decision_count": decision_count,
            "upper_breakout_count": signal_count, "signal_skipped_count": skipped,
            "closed_trade_count": len(closed_trades), "net_pnl_usdt": str(pnl),
            "final_cash_usdt": str(STARTING_CASH + pnl), "trades": closed_trades,
            "calculation_note": "Synthetic, historical-model hypothetical fills; no real execution or profitability inference.",
            "as_of": iso(rows[-1]["_time"]),
        }


def validate_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise Stage1Error("JSON record must be an object", stage="record_read")
    for key in ("record_version", "run_id", "run_status", "execution", "data", "research", "events", "result", "errors"):
        if key not in record:
            raise Stage1Error(f"Missing required field: {key}", stage="record_read")
    if record["record_version"] != RECORD_VERSION or record["run_status"] not in ("running", "completed", "failed"):
        raise Stage1Error("Unsupported record version or status", stage="record_read")
    if not isinstance(record["run_id"], str) or not record["run_id"]:
        raise Stage1Error("Invalid run_id", stage="record_read")
    for key in ("execution", "data", "research"):
        if not isinstance(record[key], dict):
            raise Stage1Error(f"Invalid {key} type", stage="record_read")
    for key in ("events", "errors"):
        if not isinstance(record[key], list):
            raise Stage1Error(f"Invalid {key} type", stage="record_read")
    if not record["execution"].get("started_at") or not record["data"].get("source") or not record["research"].get("name"):
        raise Stage1Error("Missing execution, data, or research identity", stage="record_read")
    for field in ("N", "H_hours", "buy_notional_usdt", "fee_rate_each_side", "adverse_price_adjustment_each_side", "source_revision"):
        if field not in record["research"]:
            raise Stage1Error(f"Missing research setting: {field}", stage="record_read")
    if record["run_status"] == "completed":
        if not record["execution"].get("ended_at") or not isinstance(record["result"], dict) or record["result"].get("evaluation_status") != "calculated" or record["result"].get("net_pnl_usdt") is None:
            raise Stage1Error("Incomplete completed record", stage="record_read")
        if record["errors"]:
            raise Stage1Error("Completed run cannot contain unresolved errors", stage="record_read")
        for field in ("sha256", "symbol", "timeframe", "timezone", "availability_assumption"):
            if not record["data"].get(field):
                raise Stage1Error(f"Missing completed data identity: {field}", stage="record_read")
        for field in ("decision_count", "upper_breakout_count", "closed_trade_count", "final_cash_usdt", "trades", "as_of"):
            if field not in record["result"]:
                raise Stage1Error(f"Missing completed result: {field}", stage="record_read")
    if record["run_status"] == "running" and record["result"] is not None:
        raise Stage1Error("Running record cannot have final result", stage="record_read")
    ids = set()
    for event in record["events"]:
        if not isinstance(event, dict) or event.get("run_id") != record["run_id"] or not event.get("event_id") or not event.get("at"):
            raise Stage1Error("Invalid event linkage", stage="record_read")
        if event["event_id"] in ids:
            raise Stage1Error("Duplicate event id", stage="record_read")
        ids.add(event["event_id"])
    if record["run_status"] == "completed":
        try:
            validate_result_numbers(record)
        except DecimalException as exc:
            raise Stage1Error("Decimal arithmetic could not be verified", stage="record_read") from exc
        validate_completed_times(record)
    return record


def record_time(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise Stage1Error(f"Invalid timestamp: {field}", stage="record_read")
    try:
        return as_utc(value)
    except Stage1Error as exc:
        raise Stage1Error(f"Invalid UTC timestamp: {field}", stage="record_read") from exc


def validate_completed_times(record: dict) -> None:
    execution = record["execution"]
    started = record_time(execution["started_at"], "execution.started_at")
    ended = record_time(execution["ended_at"], "execution.ended_at")
    if ended < started:
        raise Stage1Error("Execution times are reversed", stage="record_read")
    data = record["data"]
    market_start = record_time(data["start_at"], "data.start_at") if data.get("start_at") is not None else None
    last_open = record_time(data["last_bar_open_at"], "data.last_bar_open_at") if data.get("last_bar_open_at") is not None else None
    as_of = record_time(record["result"]["as_of"], "result.as_of")
    if ((market_start is not None and market_start > as_of)
            or (last_open is not None and last_open > as_of)
            or (market_start is not None and last_open is not None and last_open < market_start)):
        raise Stage1Error("Market time window is reversed", stage="record_read")
    previous_event_time = None
    for event in record["events"]:
        event_time = record_time(event["at"], "event.at")
        if ((market_start is not None and event_time < market_start)
                or event_time > as_of
                or (previous_event_time is not None and event_time < previous_event_time)):
            raise Stage1Error("Market event is outside chronological window", stage="record_read")
        previous_event_time = event_time
        if "knowledge_cutoff" in event:
            cutoff = record_time(event["knowledge_cutoff"], "event.knowledge_cutoff")
            if cutoff != event_time:
                raise Stage1Error("Decision knowledge cutoff differs", stage="record_read")
        if "source_bar_start_at" in event:
            source_start = record_time(event["source_bar_start_at"], "event.source_bar_start_at")
            if source_start + ONE_HOUR > event_time:
                raise Stage1Error("Decision used an unclosed bar", stage="record_read")
        for field in ("intended_open_at", "exit_scheduled_at"):
            if field in event and record_time(event[field], f"event.{field}") < event_time:
                raise Stage1Error("Scheduled event precedes its source", stage="record_read")
    for trade in record["result"]["trades"]:
        entered = record_time(trade.get("entered_at"), "trade.entered_at")
        exited = record_time(trade.get("exited_at"), "trade.exited_at")
        if ((market_start is not None and entered < market_start)
                or exited <= entered or exited > as_of):
            raise Stage1Error("Trade times are outside chronological window", stage="record_read")


def finite_decimal(value, field: str) -> Decimal:
    if not isinstance(value, str):
        raise Stage1Error(f"Invalid decimal field: {field}", stage="record_read")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise Stage1Error(f"Invalid decimal field: {field}", stage="record_read") from exc
    if not amount.is_finite():
        raise Stage1Error(f"Nonfinite decimal field: {field}", stage="record_read")
    return amount


def validate_result_numbers(record: dict) -> None:
    research = record["research"]
    result = record["result"]
    if (type(research["N"]) is not int or research["N"] != N
            or type(research["H_hours"]) is not int or research["H_hours"] != H_HOURS):
        raise Stage1Error("Research settings differ", stage="record_read")
    starting_cash = finite_decimal(research.get("starting_cash_usdt"), "starting_cash_usdt")
    notional = finite_decimal(research["buy_notional_usdt"], "buy_notional_usdt")
    fee_rate = finite_decimal(research["fee_rate_each_side"], "fee_rate_each_side")
    slippage = finite_decimal(research["adverse_price_adjustment_each_side"], "adverse_price_adjustment_each_side")
    if (starting_cash != STARTING_CASH or notional != BUY_NOTIONAL
            or fee_rate != FEE_RATE or slippage != SLIPPAGE_RATE):
        raise Stage1Error("Research monetary settings differ", stage="record_read")
    for field in ("decision_count", "upper_breakout_count", "signal_skipped_count", "closed_trade_count"):
        if type(result.get(field)) is not int or result[field] < 0:
            raise Stage1Error(f"Invalid result count: {field}", stage="record_read")
    trades = result["trades"]
    if not isinstance(trades, list) or result["closed_trade_count"] != len(trades):
        raise Stage1Error("Trade count differs", stage="record_read")
    entries = [event for event in record["events"] if event.get("event_type") == "virtual_entry"]
    exits = [event for event in record["events"] if event.get("event_type") == "virtual_exit"]
    if len(entries) != len(trades) or len(exits) != len(trades):
        raise Stage1Error("Trade events differ", stage="record_read")
    breakout_events = [event for event in record["events"] if event.get("event_type") == "upper_breakout"]
    breakout_keys = {(event["at"], event.get("source_bar_start_at")) for event in breakout_events}
    threshold_count = 0
    skipped_keys = set()
    for event in record["events"]:
        if event.get("event_type") != "no_trade":
            continue
        reason = event.get("reason")
        if reason == "threshold_not_crossed":
            threshold_count += 1
        elif reason in ("same_instant_as_exit", "position_already_open", "entry_pending"):
            key = (event["at"], event.get("source_bar_start_at"))
            if key not in breakout_keys or key in skipped_keys:
                raise Stage1Error("Skipped signal differs from breakout", stage="record_read")
            skipped_keys.add(key)
        else:
            raise Stage1Error("Unknown no-trade reason", stage="record_read")
    if (result["upper_breakout_count"] != len(breakout_events)
            or result["decision_count"] != len(breakout_events) + threshold_count
            or result["signal_skipped_count"] != len(skipped_keys)
            or result["closed_trade_count"] != len(exits)):
        raise Stage1Error("Result counts differ from events", stage="record_read")
    with localcontext() as context:
        context.prec = 34
        total = ZERO
        for trade, entry, exit_event in zip(trades, entries, exits):
            if not isinstance(trade, dict):
                raise Stage1Error("Invalid trade", stage="record_read")
            buy_price = finite_decimal(trade.get("buy_price"), "buy_price")
            quantity = finite_decimal(trade.get("quantity_btc"), "quantity_btc")
            buy_fee = finite_decimal(trade.get("buy_fee_usdt"), "buy_fee_usdt")
            sell_price = finite_decimal(trade.get("sell_price"), "sell_price")
            sell_fee = finite_decimal(trade.get("sell_fee_usdt"), "sell_fee_usdt")
            trade_pnl = finite_decimal(trade.get("net_pnl_usdt"), "trade net_pnl_usdt")
            if (buy_price <= ZERO or quantity <= ZERO or sell_price <= ZERO
                    or buy_fee < ZERO or sell_fee < ZERO
                    or quantity != notional / buy_price
                    or buy_fee != notional * fee_rate):
                raise Stage1Error("Invalid trade quantity or purchase", stage="record_read")
            gross = quantity * sell_price
            if sell_fee != gross * fee_rate or trade_pnl != gross - sell_fee - notional - buy_fee:
                raise Stage1Error("Trade proceeds or PNL differs", stage="record_read")
            if (entry.get("at") != trade.get("entered_at")
                    or exit_event.get("at") != trade.get("exited_at")
                    or any(entry.get(field) != trade.get(field) for field in
                           ("buy_price", "quantity_btc", "buy_fee_usdt"))
                    or any(exit_event.get(field) != trade.get(field) for field in
                           ("buy_price", "quantity_btc", "buy_fee_usdt", "sell_price", "sell_fee_usdt", "net_pnl_usdt"))):
                raise Stage1Error("Trade event values differ", stage="record_read")
            reference_open = finite_decimal(entry.get("reference_open"), "reference_open")
            if reference_open <= ZERO or buy_price != reference_open * (ONE + slippage):
                raise Stage1Error("Entry reference price differs", stage="record_read")
            total += trade_pnl
        if (finite_decimal(result["net_pnl_usdt"], "net_pnl_usdt") != total
                or finite_decimal(result["final_cash_usdt"], "final_cash_usdt") != starting_cash + total):
            raise Stage1Error("Aggregate PNL or final cash differs", stage="record_read")


def reject_json_constant(value: str):
    raise ValueError(f"Nonfinite JSON constant: {value}")


def load_record(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream, parse_constant=reject_json_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Stage1Error(f"Cannot read valid JSON: {exc}", stage="record_read") from exc
    return validate_record(data)


def atomic_save(path: Path, record: dict, *, verify_before_publish: bool = False) -> None:
    """Atomic replacement on same filesystem; NOT a power-loss durability guarantee."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".coin1_", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if verify_before_publish and load_record(Path(temp_name)) != record:
            raise Stage1Error("Completed candidate differs from saved JSON", stage="record_verify")
        os.replace(temp_name, path)
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def execute_experiment(filename: Path, out_dir: Path, *, run_id: str | None = None) -> Path:
    filename = filename.resolve()
    run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"
    if not run_id.isascii() or not run_id.replace("_", "").replace("-", "").isalnum():
        raise Stage1Error("Invalid run_id", stage="setup")
    path = out_dir / (run_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError as exc:
        raise Stage1Error("Existing run must not be overwritten", stage="setup") from exc
    record = new_record(run_id, filename)
    try:
        atomic_save(path, record)
        rows, sha = data_rows(filename)
        record["data"]["sha256"] = sha
        simulate(rows, record)
        record["run_status"] = "completed"
        record["execution"]["ended_at"] = utc_now()
        atomic_save(path, record, verify_before_publish=True)
        return path
    except Exception as exc:
        # Save the failure if possible. A broken storage device may prevent this.
        record["run_status"] = "failed"
        record["result"] = None
        record["execution"]["ended_at"] = utc_now()
        error = {"stage": exc.stage if isinstance(exc, Stage1Error) else "storage_or_runtime",
                 "reason": str(exc), "at": exc.at if isinstance(exc, Stage1Error) else utc_now()}
        record["errors"].append(error)
        try:
            event_time = iso(as_utc(error["at"])) if error["at"] else utc_now()
        except Stage1Error:
            event_time = utc_now()
        record_event(record, "error", event_time, stage=error["stage"], reason=error["reason"], source_time=error["at"])
        try:
            atomic_save(path, record)
        except Exception as save_exc:
            print(f"Unable to persist failure status: {save_exc}", file=sys.stderr)
        raise Stage1Error(f"Experiment failed: {exc}", stage=error["stage"], at=error["at"]) from exc


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="coin-1 implementation 1: synthetic connection test only")
    cmd = parser.add_subparsers(dest="command", required=True)
    run = cmd.add_parser("run", help="Run the synthetic test and save a JSON record")
    run.add_argument("--data", required=True, type=Path)
    run.add_argument("--out-dir", required=True, type=Path)
    read = cmd.add_parser("read", help="Read and validate a saved run JSON")
    read.add_argument("--record", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            path = execute_experiment(args.data, args.out_dir)
            result = load_record(path)
            print(json.dumps({"run_id": result["run_id"], "run_status": result["run_status"],
                              "net_pnl_usdt": result["result"]["net_pnl_usdt"], "record_path": str(path)}, ensure_ascii=False))
        else:
            result = load_record(args.record)
            print(json.dumps({"run_id": result["run_id"], "run_status": result["run_status"],
                              "result": result["result"], "errors": result["errors"]}, ensure_ascii=False))
        return 0
    except (Stage1Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
