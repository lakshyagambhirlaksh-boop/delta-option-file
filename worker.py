"""Persistent scheduler and combined-premium monitor. Run in its own PowerShell window."""

import datetime as dt
import json
import os
import sys
import time

import requests

import strategy as bot


LOCK_PATH = bot.DB_PATH.with_name("worker.lock")


def acquire_single_worker():
    lock_file = open(LOCK_PATH, "a+b")
    lock_file.seek(0)
    if lock_file.read(1) == b"":
        lock_file.seek(0)
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock_file.close()
        raise RuntimeError("Another strategy worker is already running.")
    return lock_file


def main():
    key, secret = bot.credentials()
    if not key or not secret:
        print("Set DELTA_API_KEY and DELTA_API_SECRET in .env, then restart the worker.", flush=True)
        return 2
    try:
        lock_file = acquire_single_worker()
    except RuntimeError as exc:
        print(exc, flush=True)
        return 2

    print("Delta strategy worker running. Keep this window and the computer online.", flush=True)
    last_heartbeat = 0.0
    try:
        while True:
            chains = {}
            for underlying in bot.UNDERLYINGS:
                try:
                    chains[underlying] = bot.fetch_chain(underlying)
                except (requests.RequestException, RuntimeError, ValueError) as exc:
                    chains[underlying] = []
                    print(f"{underlying} market-data error: {exc}", flush=True)
            try:
                selections = json.loads(bot.get_setting("config", "{}"))
            except json.JSONDecodeError:
                selections = {}
            try:
                events = bot.run_cycle(chains, selections)
                for event in events:
                    print(f"{dt.datetime.now(bot.TZ).isoformat(timespec='seconds')} {event}", flush=True)
            except Exception as exc:
                print(f"Strategy cycle error: {exc}", flush=True)
            if time.monotonic() - last_heartbeat >= 10:
                bot.set_setting("worker_heartbeat", dt.datetime.now(bot.TZ).isoformat(timespec="seconds"))
                last_heartbeat = time.monotonic()
            time.sleep(5)
    except KeyboardInterrupt:
        print("Worker stopped. Open positions will no longer be monitored by this process.", flush=True)
        return 0
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
