"""coin-1 implementation 1: synthetic BTC research -> replayable JSON.

Scope: the adopted `設計/実装01_最小作業仕様.md` v0.1 only.
No exchange connectivity, real trades, Telegram, diary or live monitoring.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
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
        with filename.open("r", newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
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
    return record


def load_record(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Stage1Error(f"Cannot read valid JSON: {exc}", stage="record_read") from exc
    return validate_record(data)


def atomic_save(path: Path, record: dict) -> None:
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
    if path.exists():
        raise Stage1Error("Existing run must not be overwritten", stage="setup")
    record = new_record(run_id, filename)
    try:
        atomic_save(path, record)
        rows, sha = data_rows(filename)
        record["data"]["sha256"] = sha
        simulate(rows, record)
        record["run_status"] = "completed"
        record["execution"]["ended_at"] = utc_now()
        atomic_save(path, record)
        saved = load_record(path)
        if saved != record:
            raise Stage1Error("Saved record differs from in-memory record", stage="record_verify")
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
