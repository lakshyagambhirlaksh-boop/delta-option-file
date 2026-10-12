import datetime as dt
import json
import uuid

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import strategy as bot

st.set_page_config(page_title="Delta Options Strategy", layout="wide")
st.title("Delta options strategy")
st.caption("BTC · ETH · XAUT | India Standard Time (IST) | live Delta Exchange India")
st_autorefresh(interval=5000, key="strategy_refresh")

key, secret = bot.credentials()
armed = bot.get_setting("armed", "0") == "1"
try:
    saved_config = json.loads(bot.get_setting("config", "{}"))
except json.JSONDecodeError:
    saved_config = {}

st.info("Short strangle: sell one call and one put. Choose ATM, ATM ±2, ATM ±3, or ATM ±4. Stop loss is 2× actual combined fill premium; take profit is 0.5×. Starting during a selected IST window enters on the next worker cycle and closes by the window end: 21:30–05:30 or 09:30–15:30.")
st.warning("Live orders are enabled when you start the strategy. Premium exits are monitored by the worker process and require this computer and internet connection to stay online. Stop entries pauses future entries; it keeps managing open positions. Closing open positions sends reduce-only market orders.")
if not key or not secret:
    st.error("Live orders are unavailable until DELTA_API_KEY and DELTA_API_SECRET are set in .env and the app is restarted.")
else:
    st.success("Delta credentials are loaded locally.")

now = dt.datetime.now(bot.TZ)
session_labels = {"night": "Night slot · 9:30 PM–5:30 AM IST", "day": "Day slot · 9:30 AM–3:30 PM IST"}
saved_sessions = saved_config.get("_sessions", list(bot.SESSION_WINDOWS))
session_default = [session_labels[name] for name in saved_sessions if name in session_labels]
selected_session_labels = st.multiselect("Trading time slots (IST)", list(session_labels.values()), default=session_default, disabled=armed, help="Choose either or both of your daily trading windows.")
selected_sessions = [name for name, label in session_labels.items() if label in selected_session_labels]
active_windows = {name: bot.SESSION_WINDOWS[name] for name in (selected_sessions or list(bot.SESSION_WINDOWS))}
schedule = bot.schedule_snapshot(now, active_windows)
if schedule["active_name"]:
    st.write(f"Active window: **{schedule['active_name']}**, ends {schedule['active_end'].strftime('%d %b %Y %H:%M IST')}.")
else:
    st.write(f"Next entry window: **{schedule['next_name']}**, starts {schedule['next_start'].strftime('%d %b %Y %H:%M IST')}.")
heartbeat = bot.get_setting("worker_heartbeat", "")
try:
    heartbeat_dt = dt.datetime.fromisoformat(heartbeat)
    heartbeat_age = (now - heartbeat_dt).total_seconds()
except (ValueError, TypeError):
    heartbeat_age = float("inf")
if heartbeat_age <= 20:
    st.success(f"Trade worker online · last heartbeat {heartbeat_dt.strftime('%H:%M:%S IST')}.")
else:
    st.error("Trade worker is offline. Start `python worker.py` in a second PowerShell window. No entries or exits can be processed until it is running.")

chains, errors = {}, {}
for underlying in bot.UNDERLYINGS:
    try:
        chains[underlying] = bot.fetch_chain(underlying)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        chains[underlying], errors[underlying] = [], str(exc)

st.subheader("Strategy configuration")
st.caption("Choose any one, two, or all three instruments. Quantity is contracts per leg. The displayed option chain contains every contract returned for the selected expiry.")
previous = list(saved_config) if armed else list(saved_config) or list(bot.UNDERLYINGS)
selected = st.multiselect("Instruments", bot.UNDERLYINGS, default=[x for x in previous if x in bot.UNDERLYINGS], disabled=armed)
selections = {}
for underlying in selected:
    st.markdown(f"### {underlying}")
    if errors.get(underlying):
        st.error(f"Market data unavailable: {errors[underlying]}")
        continue
    chain = chains.get(underlying, [])
    expiry_list = sorted({bot.expiry_from_symbol(r.get("symbol", "")) for r in chain if bot.expiry_from_symbol(r.get("symbol", ""))}, key=lambda x: dt.datetime.strptime(x, "%d-%m-%Y"))
    if not expiry_list:
        st.warning("No expiries returned for this instrument.")
        continue
    saved = saved_config.get(underlying, {})
    expiry_default = saved.get("expiry", expiry_list[0])
    expiry = st.selectbox(f"{underlying} expiry", expiry_list, index=expiry_list.index(expiry_default) if expiry_default in expiry_list else 0, key=f"exp_{underlying}", disabled=armed)
    controls = st.columns(2)
    strike_choices = {"ATM": 0, "ATM +2 call / −2 put": 2, "ATM +3 call / −3 put": 3, "ATM +4 call / −4 put": 4}
    saved_offset = int(saved.get("strike_offset", 2))
    saved_choice = next((label for label, offset in strike_choices.items() if offset == saved_offset), "ATM +2 call / −2 put")
    mode = controls[0].selectbox("Strike selection", list(strike_choices), index=list(strike_choices).index(saved_choice), key=f"mode_{underlying}", disabled=armed)
    quantity = int(controls[1].number_input("Contracts per leg", min_value=1, max_value=100000, value=max(1, int(saved.get("quantity", 1))), step=1, key=f"qty_{underlying}", disabled=armed))
    expiry_chain = [r for r in chain if bot.expiry_from_symbol(r.get("symbol", "")) == expiry]
    call_strikes = sorted({bot.number(r.get("strike_price")) for r in expiry_chain if r.get("contract_type") == "call_options" and bot.number(r.get("strike_price")) > 0})
    put_strikes = sorted({bot.number(r.get("strike_price")) for r in expiry_chain if r.get("contract_type") == "put_options" and bot.number(r.get("strike_price")) > 0})
    selection = {"expiry": expiry, "quantity": quantity, "strike_offset": strike_choices[mode]}
    selections[underlying] = selection
    pair = bot.option_pair(chain, expiry, selection["strike_offset"])
    if pair:
        call, put = pair
        cq, pq = call.get("quotes") or {}, put.get("quotes") or {}
        spot = bot.number(call.get("spot_price"))
        st.caption(f"Spot {spot:g} · selected pair: {call.get('symbol')} (bid {bot.number(cq.get('best_bid')):g} × {bot.number(cq.get('bid_size')):g}) + {put.get('symbol')} (bid {bot.number(pq.get('best_bid')):g} × {bot.number(pq.get('bid_size')):g}); combined ask {bot.number(cq.get('best_ask')) + bot.number(pq.get('best_ask')):g}")
    else:
        st.warning("The selected strike pair is not available in the current chain.")
    display = []
    for row in expiry_chain:
        quote = row.get("quotes") or {}
        display.append({"Type": "Call" if row.get("contract_type") == "call_options" else "Put", "Strike": bot.number(row.get("strike_price")), "Symbol": row.get("symbol"), "Bid": bot.number(quote.get("best_bid")), "Bid size": bot.number(quote.get("bid_size")), "Ask": bot.number(quote.get("best_ask")), "Ask size": bot.number(quote.get("ask_size")), "Mark": bot.number(row.get("mark_price")), "Volume": bot.number(row.get("volume")), "Open interest": bot.number(row.get("oi"))})
    st.dataframe(pd.DataFrame(display).sort_values(["Strike", "Type"]), use_container_width=True, hide_index=True)

st.subheader("Run controls")
if armed:
    running_instruments = [name for name in saved_config if name in bot.UNDERLYINGS]
    running_sessions = [session_labels[name] for name in saved_config.get("_sessions", list(bot.SESSION_WINDOWS)) if name in session_labels]
    st.success(f"Strategy is running for {', '.join(running_instruments)} during {', '.join(running_sessions)}. Configuration is locked while active.")
    if st.button("Stop new entries", type="secondary"):
        bot.set_setting("armed", "0")
        st.rerun()
else:
    st.info("Strategy is stopped. Starting during a selected time slot enters on the next worker cycle. Starting outside the selected slots waits until the next slot. Open positions still follow the combined-premium stop loss / take profit and close at that slot's scheduled end.")
    confirm = st.checkbox("I authorize live option orders for the instruments and quantities shown above, including app-managed stop loss, take profit, and session-end closes.")
    can_start = bool(confirm and key and secret and selected_sessions and selected and len(selections) == len(selected) and heartbeat_age <= 20 and all(bot.option_pair(chains[u], s["expiry"], s["strike_offset"]) for u, s in selections.items()))
    if st.button("Start strategy", type="primary", disabled=not can_start):
        if not selected_sessions:
            st.error("Select at least one trading time slot.")
            st.stop()
        selections["_sessions"] = selected_sessions
        bot.set_setting("config", json.dumps(selections))
        bot.set_setting("start_id", uuid.uuid4().hex)
        bot.set_setting("armed", "1")
        st.rerun()

open_runs = [r for r in bot.list_runs() if r["state"] in ("open", "closing")]
if open_runs:
    st.subheader("Open strategy positions")
    st.dataframe(pd.DataFrame([{"Instrument": r["underlying"], "Window": r["window_name"], "State": r["state"], "Call": r["payload"].get("call_symbol", ""), "Put": r["payload"].get("put_symbol", ""), "Qty/leg": r["payload"].get("quantity", ""), "Entry premium": r["payload"].get("entry_credit", ""), "Stop": r["payload"].get("stop", ""), "Target": r["payload"].get("target", ""), "Updated": r["updated_at"]} for r in open_runs]), use_container_width=True, hide_index=True)
    confirm_close = st.checkbox("Confirm: send reduce-only market orders to close all open positions tracked by this strategy.")
    if st.button("Stop strategy and close open positions", type="primary", disabled=not confirm_close):
        bot.set_setting("armed", "0")
        outcomes = bot.close_strategy_positions()
        st.session_state["close_outcomes"] = outcomes
        st.rerun()
if st.session_state.get("close_outcomes"):
    st.info("Close requests: " + ("; ".join(st.session_state["close_outcomes"]) or "No tracked positions were open."))

st.subheader("Strategy history")
runs = bot.list_runs()
if runs:
    st.dataframe(pd.DataFrame([{"Instrument": r["underlying"], "Window": r["window_name"], "Session date (IST)": r["session_date"], "State": r["state"], "Call": r["payload"].get("call_symbol", ""), "Put": r["payload"].get("put_symbol", ""), "Quantity/leg": r["payload"].get("quantity", ""), "Entry premium": r["payload"].get("entry_credit", ""), "SL": r["payload"].get("stop", ""), "TP": r["payload"].get("target", ""), "Message": r["message"], "Updated (IST)": r["updated_at"]} for r in runs]), use_container_width=True, hide_index=True)
else:
    st.caption("No strategy runs saved yet.")
st.caption("The worker monitors combined best ask against actual combined fill credit. If a threshold is reached, it sends reduce-only market buy orders for tracked contracts. Check Delta for fills and positions.")
