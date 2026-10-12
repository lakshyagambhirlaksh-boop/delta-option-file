import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


load_dotenv()
API_ROOT = "https://api.india.delta.exchange"
API_V2 = API_ROOT + "/v2"
STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).parent))
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_DIR / "trading_state.db"
TZ = ZoneInfo("Asia/Kolkata")
UNDERLYINGS = ("BTC", "ETH", "XAUT")
SESSION_WINDOWS = {
    "night": (dt.time(21, 30), dt.time(5, 30)),
    "day": (dt.time(9, 30), dt.time(15, 30)),
}


def credentials() -> tuple[str, str]:
    return os.getenv("DELTA_API_KEY", "").strip(), os.getenv("DELTA_API_SECRET", "").strip()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_key TEXT PRIMARY KEY,
            underlying TEXT NOT NULL,
            window_name TEXT NOT NULL,
            session_date TEXT NOT NULL,
            state TEXT NOT NULL,
            payload TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""")
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def save_run(run_key: str, underlying: str, window_name: str, session_date: str, state: str, payload: dict, message: str = "") -> None:
    with db() as conn:
        conn.execute("""INSERT INTO runs(run_key,underlying,window_name,session_date,state,payload,message,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_key) DO UPDATE SET
            state=excluded.state,payload=excluded.payload,message=excluded.message,updated_at=excluded.updated_at""",
            (run_key, underlying, window_name, session_date, state, json.dumps(payload), message, dt.datetime.now(TZ).isoformat(timespec="seconds")))


def update_run(run_key: str, state: str, payload: dict, message: str = "") -> None:
    with db() as conn:
        conn.execute("UPDATE runs SET state=?,payload=?,message=?,updated_at=? WHERE run_key=?",
                     (state, json.dumps(payload), message, dt.datetime.now(TZ).isoformat(timespec="seconds"), run_key))


def list_runs() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY updated_at DESC LIMIT 100").fetchall()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def get_run(run_key: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_key=?", (run_key,)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload"])} if row else None


def close_strategy_positions() -> list[str]:
    """Request reduce-only market closes for every bot-tracked live run."""
    outcomes = []
    for run in list_runs():
        if run["state"] not in ("open", "closing"):
            continue
        data = run["payload"]
        data["exit_reason"] = "Manual dashboard stop"
        update_run(run["run_key"], "closing", data, data["exit_reason"])
        state, _ = _close_remaining({"run_key": run["run_key"], "payload": data}, {})
        outcomes.append(f"{run['underlying']} {run['window_name']}: {state}")
    return outcomes


def delta_request(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    key, secret = credentials()
    if not key or not secret:
        raise RuntimeError("Set DELTA_API_KEY and DELTA_API_SECRET in .env before arming live trading.")
    query = urlencode(params or {})
    signed_path = path + ("?" + query if query else "")
    body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
    timestamp = str(int(time.time()))
    prehash = method.upper() + timestamp + signed_path + body_text
    signature = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "User-Agent": "DeltaOptionsTrader/0.3 Python", "api-key": key,
        "signature": signature, "timestamp": timestamp,
    }
    url = API_ROOT + signed_path
    response = requests.request(method.upper(), url, headers=headers,
                                data=body_text if body is not None else None, timeout=12)
    try:
        result = response.json()
    except ValueError:
        result = {"message": response.text[:400]}
    if not response.ok or not result.get("success", False):
        raise RuntimeError(f"Delta API error ({response.status_code}): {result.get('error', result.get('message', 'request failed'))}")
    return result.get("result", result)


def fetch_chain(underlying: str) -> list[dict]:
    response = requests.get(
        API_V2 + "/tickers",
        params={"contract_types": "call_options,put_options", "underlying_asset_symbols": underlying},
        headers={"Accept": "application/json", "User-Agent": "DeltaOptionsTrader/0.3"},
        timeout=12,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Delta returned an error for {underlying} option data.")
    return payload.get("result", [])


def number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def expiry_from_symbol(symbol: str) -> str:
    try:
        return dt.datetime.strptime(symbol.rsplit("-", 1)[-1], "%d%m%y").strftime("%d-%m-%Y")
    except ValueError:
        return ""


def option_pair(chain: list[dict], expiry: str, mode: int | str = 2,
                call_strike: float | None = None, put_strike: float | None = None) -> tuple[dict, dict] | None:
    options = [r for r in chain if expiry_from_symbol(r.get("symbol", "")) == expiry]
    strikes = sorted({number(r.get("strike_price")) for r in options if number(r.get("strike_price")) > 0})
    # Legacy configurations using "auto" or "manual" continue as ATM±2.
    try:
        offset = int(mode) if str(mode).isdigit() else 2
    except (TypeError, ValueError):
        offset = 2
    if offset not in (0, 2, 3, 4) or len(strikes) <= 2 * offset:
        return None
    spot = next((number(r.get("spot_price")) for r in options if number(r.get("spot_price")) > 0), 0.0)
    if not spot:
        return None
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    call_i, put_i = atm_i + offset, atm_i - offset
    if call_i >= len(strikes) or put_i < 0:
        return None
    selected_call, selected_put = strikes[call_i], strikes[put_i]
    call = next((r for r in options if r.get("contract_type") == "call_options" and number(r.get("strike_price")) == selected_call), None)
    put = next((r for r in options if r.get("contract_type") == "put_options" and number(r.get("strike_price")) == selected_put), None)
    return (call, put) if call and put else None


def _window_for(now: dt.datetime) -> tuple[str, dt.datetime, dt.datetime] | None:
    now = now.astimezone(TZ)
    day_start = dt.datetime.combine(now.date(), SESSION_WINDOWS["day"][0], TZ)
    day_end = dt.datetime.combine(now.date(), SESSION_WINDOWS["day"][1], TZ)
    if day_start <= now < day_end:
        return "day", day_start, day_end
    night_start_today = dt.datetime.combine(now.date(), SESSION_WINDOWS["night"][0], TZ)
    if now >= night_start_today:
        return "night", night_start_today, dt.datetime.combine(now.date() + dt.timedelta(days=1), SESSION_WINDOWS["night"][1], TZ)
    night_start_yesterday = dt.datetime.combine(now.date() - dt.timedelta(days=1), SESSION_WINDOWS["night"][0], TZ)
    night_end_today = dt.datetime.combine(now.date(), SESSION_WINDOWS["night"][1], TZ)
    if night_start_yesterday <= now < night_end_today:
        return "night", night_start_yesterday, night_end_today
    return None


def schedule_snapshot(now: dt.datetime | None = None, windows: dict | None = None) -> dict:
    now = (now or dt.datetime.now(TZ)).astimezone(TZ)
    windows = windows or SESSION_WINDOWS
    active = _window_for(now) if windows == SESSION_WINDOWS else _window_for_selected(now, windows)
    next_starts = []
    for offset in range(3):
        day = now.date() + dt.timedelta(days=offset)
        for name, (start, _) in windows.items():
            candidate = dt.datetime.combine(day, start, TZ)
            if candidate > now:
                next_starts.append((candidate, name))
    next_start, next_name = min(next_starts, key=lambda x: x[0])
    if active:
        name, start, end = active
    else:
        name, start, end = "", None, None
    return {"now": now, "active_name": name, "active_start": start, "active_end": end,
            "next_name": next_name, "next_start": next_start}


def _window_for_selected(now: dt.datetime, windows: dict) -> tuple[str, dt.datetime, dt.datetime] | None:
    now = now.astimezone(TZ)
    for name, (start_time, end_time) in windows.items():
        start = dt.datetime.combine(now.date(), start_time, TZ)
        end = dt.datetime.combine(now.date(), end_time, TZ)
        if end_time <= start_time:
            if now < end:
                start -= dt.timedelta(days=1)
                end = dt.datetime.combine(now.date(), end_time, TZ)
            else:
                end += dt.timedelta(days=1)
        if start <= now < end:
            return name, start, end
    return None


def order_fill(order: dict, expected: int) -> tuple[int, float]:
    remaining = int(number(order.get("unfilled_size"), 0))
    filled = max(0, min(expected, int(number(order.get("size"), expected)) - remaining))
    return filled, number(order.get("average_fill_price"))


def _position_size(product_id: int) -> int:
    result = delta_request("GET", "/v2/positions", params={"product_id": int(product_id)})
    if isinstance(result, list):
        return int(number(result[0].get("size"))) if result else 0
    return int(number(result.get("size"))) if isinstance(result, dict) else 0


def _client_id() -> str:
    return "dstr-" + uuid.uuid4().hex[:24]


def _place_ioc(product_id: int, size: int, side: str, price: float, reduce_only: bool = False) -> dict:
    payload = {
        "product_id": int(product_id), "size": int(size), "side": side,
        "order_type": "limit_order", "limit_price": f"{price:.8f}",
        "time_in_force": "ioc", "post_only": False, "reduce_only": reduce_only,
        "client_order_id": _client_id(),
    }
    return delta_request("POST", "/v2/orders", payload)


def _place_market_close(product_id: int, size: int) -> dict:
    return delta_request("POST", "/v2/orders", {
        "product_id": int(product_id), "size": int(size), "side": "buy",
        "order_type": "market_order", "reduce_only": True,
        "client_order_id": _client_id(),
    })


def _close_remaining(run: dict, chain_by_underlying: dict[str, list[dict]]) -> tuple[str, dict]:
    data = run["payload"]
    notes = []
    legs = (("call", data["call_product_id"]), ("put", data["put_product_id"]))
    for name, product_id in legs:
        try:
            live_size = abs(_position_size(product_id))
            target = min(int(data.get(f"{name}_filled", 0)), live_size)
            if target <= 0:
                data[f"{name}_closed"] = True
                continue
            result = _place_market_close(product_id, target)
            filled, _ = order_fill(result, target)
            data[f"{name}_closed"] = (filled >= target)
            if filled < target:
                notes.append(f"{name} close partial ({filled}/{target})")
        except Exception as exc:
            notes.append(f"{name} close needs attention: {exc}")
    state = "closed" if data.get("call_closed") and data.get("put_closed") else "closing"
    message = (data.get("exit_reason", "Triggered close") + ("; " + "; ".join(notes) if notes else ""))
    update_run(run["run_key"], state, data, message)
    return state, data


def _run_key(underlying: str, window_name: str, start: dt.datetime,
             start_id: str = "") -> tuple[str, str]:
    session_date = start.date().isoformat()
    suffix = f":{start_id}" if start_id else ""
    return f"{session_date}:{window_name}:{underlying}{suffix}", session_date


def _attempt_entry(underlying: str, window_name: str, start: dt.datetime, selection: dict,
                   chain: list[dict], start_id: str = "") -> str:
    run_key, session_date = _run_key(underlying, window_name, start, start_id)
    if get_run(run_key):
        return "Already attempted this instrument in this session."
    expiry, quantity = selection["expiry"], int(selection["quantity"])
    pair = option_pair(chain, expiry, selection.get("strike_offset", 2))
    if not pair:
        save_run(run_key, underlying, window_name, session_date, "skipped", {}, "Could not find ATM±2 contracts for this expiry.")
        return "Skipped: the requested ATM±2 pair is unavailable."
    call, put = pair
    cq, pq = call.get("quotes") or {}, put.get("quotes") or {}
    call_bid, put_bid = number(cq.get("best_bid")), number(pq.get("best_bid"))
    call_bid_size, put_bid_size = number(cq.get("bid_size")), number(pq.get("bid_size"))
    if call_bid <= 0 or put_bid <= 0 or call_bid_size < quantity or put_bid_size < quantity:
        save_run(run_key, underlying, window_name, session_date, "skipped", {},
                 f"Entry skipped: bid/depth insufficient for {quantity} contracts on both legs.")
        return "Skipped: displayed bid price or size is insufficient for one/both legs."
    call_id, put_id = int(call["product_id"]), int(put["product_id"])
    data = {
        "expiry": expiry, "quantity": quantity,
        "call_symbol": call["symbol"], "put_symbol": put["symbol"],
        "call_product_id": call_id, "put_product_id": put_id,
        "call_filled": 0, "put_filled": 0, "call_closed": False, "put_closed": False,
        "session_end": (start + (dt.timedelta(hours=8) if window_name == "night" else dt.timedelta(hours=6))).isoformat(),
        "entry_credit": 0, "stop": 0, "target": 0,
    }
    # Reserve the once-per-window key before sending orders, to prevent duplicate entries on reruns.
    save_run(run_key, underlying, window_name, session_date, "entering", data, "Opening call and put with IOC limit sells.")
    try:
        if _position_size(call_id) != 0 or _position_size(put_id) != 0:
            update_run(run_key, "skipped", data, "Entry skipped: a position already exists in one of these contracts.")
            return "Skipped: existing position in one of the selected contracts."
        corder = _place_ioc(call_id, quantity, "sell", call_bid)
        cfill, cprice = order_fill(corder, quantity)
        data["call_filled"], data["call_entry"] = cfill, cprice
        update_run(run_key, "entering", data, "Call sell submitted; processing put leg.")
        if cfill != quantity:
            data["exit_reason"] = "Entry aborted: call leg was not fully filled"
            update_run(run_key, "closing", data, data["exit_reason"])
            fake_run = {"run_key": run_key, "payload": data}
            _close_remaining(fake_run, {})
            return "Entry aborted: call leg was only partially filled."
        porder = _place_ioc(put_id, quantity, "sell", put_bid)
        pfill, pprice = order_fill(porder, quantity)
        data["put_filled"], data["put_entry"] = pfill, pprice
        if pfill != quantity:
            data["exit_reason"] = "Entry aborted: put leg was not fully filled"
            update_run(run_key, "closing", data, data["exit_reason"])
            _close_remaining({"run_key": run_key, "payload": data}, {})
            return "Entry aborted: put leg was only partially filled; closing filled leg(s)."
        if cprice <= 0 or pprice <= 0:
            data["exit_reason"] = "Entry aborted: Delta did not return fill prices"
            update_run(run_key, "closing", data, data["exit_reason"])
            _close_remaining({"run_key": run_key, "payload": data}, {})
            return "Entry aborted: missing fill prices; closing filled leg(s)."
        credit = cprice + pprice
        data.update({"entry_credit": credit, "stop": 2 * credit, "target": 0.5 * credit,
                     "call_closed": False, "put_closed": False})
        update_run(run_key, "open", data, "Both short legs filled; combined-premium monitor active.")
        return f"Opened {call['symbol']} + {put['symbol']}; combined fill premium {credit:g}, SL {2*credit:g}, TP {0.5*credit:g}."
    except Exception as exc:
        # Resolve a possibly accepted leg by checking live positions, then try to flatten it.
        try:
            data["call_filled"] = min(quantity, abs(_position_size(call_id)))
            data["put_filled"] = min(quantity, abs(_position_size(put_id)))
            data["call_closed"] = data["call_filled"] == 0
            data["put_closed"] = data["put_filled"] == 0
            data["exit_reason"] = "Entry incomplete; flattening any filled leg"
            update_run(run_key, "closing", data, f"Entry request issue: {exc}")
            _close_remaining({"run_key": run_key, "payload": data}, {})
            return f"Entry request issue; checked positions and attempted to flatten filled leg(s): {exc}"
        except Exception as recovery_exc:
            update_run(run_key, "manual_check", data,
                       f"Entry outcome uncertain; inspect Delta positions/orders immediately. Original: {exc}; recovery: {recovery_exc}")
            return f"Urgent: entry outcome is uncertain; inspect Delta positions/orders: {exc}"


def run_cycle(chains: dict[str, list[dict]], selections: dict[str, dict], now: dt.datetime | None = None) -> list[str]:
    now = (now or dt.datetime.now(TZ)).astimezone(TZ)
    messages = []
    try:
        stored_selections = json.loads(get_setting("config", "{}"))
    except json.JSONDecodeError:
        stored_selections = {}
    chosen_sessions = stored_selections.get("_sessions", list(SESSION_WINDOWS))
    windows = {name: SESSION_WINDOWS[name] for name in chosen_sessions if name in SESSION_WINDOWS}
    schedule = schedule_snapshot(now, windows or SESSION_WINDOWS)
    armed = get_setting("armed", "0") == "1"
    active_name, active_start, active_end = schedule["active_name"], schedule["active_start"], schedule["active_end"]

    # Monitor open pairs regardless of whether new entries are armed.
    for run in list_runs():
        if run["state"] not in ("open", "closing"):
            continue
        data = run["payload"]
        rows = {r.get("symbol"): r for r in chains.get(run["underlying"], [])}
        call = rows.get(data["call_symbol"])
        put = rows.get(data["put_symbol"])
        reason = data.get("exit_reason", "")
        if run["state"] == "open" and not reason:
            combined_ask = None
            run_end = dt.datetime.fromisoformat(data["session_end"]).astimezone(TZ)
            if now >= run_end:
                reason = "Session ended"
                data["exit_reason"] = reason
                update_run(run["run_key"], "closing", data, reason)
                run["state"], run["payload"] = "closing", data
            elif call is not None and put is not None:
                call_q, put_q = call.get("quotes") or {}, put.get("quotes") or {}
                call_ask, put_ask = number(call_q.get("best_ask")), number(put_q.get("best_ask"))
                if call_ask > 0 and put_ask > 0:
                    combined_ask = call_ask + put_ask
                    data["last_combined_ask"] = combined_ask
                    if combined_ask >= number(data.get("stop")):
                        reason = "Combined ask reached stop loss"
                    elif combined_ask <= number(data.get("target")):
                        reason = "Combined ask reached take profit"
            if reason:
                data["exit_reason"] = reason
                if combined_ask is not None:
                    data["exit_combined_ask"] = combined_ask
                update_run(run["run_key"], "closing", data, reason)
                run["state"], run["payload"] = "closing", data
        if run["state"] == "closing":
            state, _ = _close_remaining(run, chains)
            messages.append(f"{run['underlying']} {run['window_name']}: {data.get('exit_reason')} → {state}.")

    # Enter once at any point while a selected session is active. The run's
    # session_end remains the configured window end, even when started mid-window.
    if armed and active_name:
        start_id = get_setting("start_id", "")
        for underlying, selection in stored_selections.items():
            if underlying not in UNDERLYINGS:
                continue
            selection = selection or {}
            if not selection.get("expiry") or not selection.get("quantity") or underlying not in chains:
                continue
            run_key, _ = _run_key(underlying, active_name, active_start, start_id)
            if get_run(run_key):
                continue
            message = _attempt_entry(underlying, active_name, active_start, selection, chains[underlying], start_id)
            messages.append(f"{underlying} {active_name}: {message}")
    return messages
