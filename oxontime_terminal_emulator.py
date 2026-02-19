#!/usr/bin/env python3
"""
Mac terminal emulator for the OxonTime -> ePaper app.

What it does:
- Fetches OxonTime JSON for a stop (OXON_STOP).
- Chooses 3 departures.
- Emphasizes (bigger ASCII + color) the soonest bus you're likely to catch given WALK_MIN.
- Quiet hours (22:00–06:00 by default): shows a funny message and pauses updates.
- Adaptive refresh: normal (DAY_REFRESH) vs fast (FAST_REFRESH) when the catchable bus is soon.

Runs on macOS terminal (no Waveshare libs). Uses ANSI escape codes.

Install:
  python3 -m pip install requests

Run:
  OXON_STOP=340000022GEO WALK_MIN=5 python3 oxontime_terminal_emulator.py
"""

import os
import time
import datetime as dt
import requests
import shutil

STOP = os.environ.get("OXON_STOP", "340000022GEO")
URL = f"https://oxontime.com/pwi/departureBoard/{STOP}"

WALK_MIN = int(os.environ.get("WALK_MIN", "5"))

DAY_REFRESH = int(os.environ.get("DAY_REFRESH", "180"))
FAST_REFRESH = int(os.environ.get("FAST_REFRESH", "60"))
FAST_WINDOW_MIN = int(os.environ.get("FAST_WINDOW_MIN", "10"))

QUIET_START = int(os.environ.get("QUIET_START", "22"))
QUIET_END = int(os.environ.get("QUIET_END", "6"))
QUIET_REFRESH = int(os.environ.get("QUIET_REFRESH", "1800"))

# ANSI helpers
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
WHITE = "\033[97m"
CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def in_quiet_hours(now: dt.datetime) -> bool:
    if QUIET_START < QUIET_END:
        return QUIET_START <= now.hour < QUIET_END
    return (now.hour >= QUIET_START) or (now.hour < QUIET_END)


def parse_minutes(display_time: str):
    if not display_time:
        return None
    t = display_time.strip().lower()
    if "min" in t:
        try:
            return int(t.split()[0])
        except Exception:
            return None
    return None  # e.g. "21:47"


def fmt_call(call):
    route = (call.get("route_code") or "").strip()
    dest = (call.get("destination_name") or "").strip()
    disp = (call.get("display_time") or "").strip()
    eta = parse_minutes(disp)
    return route, dest, disp, eta


def choose_catchable(top_calls):
    best_idx = None
    best_eta = None
    for i, c in enumerate(top_calls):
        _, _, _, eta = fmt_call(c)
        if eta is None:
            continue
        if eta >= WALK_MIN and (best_eta is None or eta < best_eta):
            best_eta, best_idx = eta, i
    return best_idx if best_idx is not None else 0


def choose_refresh_seconds(top_calls, catch_idx):
    if not top_calls:
        return DAY_REFRESH
    _, _, _, eta = fmt_call(top_calls[catch_idx])
    if eta is not None and eta <= FAST_WINDOW_MIN:
        return FAST_REFRESH
    return DAY_REFRESH


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def term_width() -> int:
    try:
        return shutil.get_terminal_size((80, 20)).columns
    except Exception:
        return 80


def big_text(line: str) -> str:
    """
    Simple "bigger" effect: boxed + double line.
    (Not true large font, but visually emphasized in terminal.)
    """
    w = term_width()
    inner = truncate(line, w - 6)
    top = "┏" + "━" * (len(inner) + 2) + "┓"
    mid = "┃ " + inner + " ┃"
    bot = "┗" + "━" * (len(inner) + 2) + "┛"
    return "\n".join([top, mid, bot])


def render_departures(stop_name: str, top3, catch_idx: int, next_sleep: int):
    w = term_width()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = f"{stop_name}  |  {now}"
    header = truncate(header, w)

    # Prepare 3 lines
    lines = []
    for c in top3:
        route, dest, disp, _ = fmt_call(c)
        route = truncate(route, 4).rjust(3)
        # leave space for route + time
        dest_w = max(10, w - 3 - 2 - 8 - 6)
        dest = truncate(dest, dest_w)
        disp = truncate(disp, 8).rjust(8)
        lines.append(f"{route}  {dest}  {disp}")

    while len(lines) < 3:
        lines.append("")

    # Emphasized line
    emph = lines[catch_idx] if 0 <= catch_idx < len(lines) else lines[0]
    others = [lines[i] for i in range(len(lines)) if i != catch_idx]
    while len(others) < 2:
        others.append("")

    footer = f"{DIM}Walk={WALK_MIN}min  |  refresh={next_sleep}s  |  quiet={QUIET_START:02d}:00–{QUIET_END:02d}:00{RESET}"
    footer = truncate(footer, w)

    out = []
    out.append(CLEAR + HIDE_CURSOR)
    out.append(f"{BOLD}{WHITE}{header}{RESET}")
    out.append("")
    out.append(f"{BOLD}{RED}{big_text(emph)}{RESET}")
    out.append("")
    out.append(others[0])
    out.append(others[1])
    out.append("")
    out.append(footer)
    print("\n".join(out), flush=True)


def render_quiet(stop_name: str):
    w = term_width()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = truncate(f"{stop_name}  |  {now}", w)

    msg1 = "Buses are sleeping."
    msg2 = "So are we. 😴"
    msg3 = f"Back at {QUIET_END:02d}:00."

    out = []
    out.append(CLEAR + HIDE_CURSOR)
    out.append(f"{BOLD}{WHITE}{header}{RESET}")
    out.append("")
    out.append(f"{BOLD}{RED}{big_text(msg1)}{RESET}")
    out.append("")
    out.append(truncate(msg2, w))
    out.append(truncate(msg3, w))
    out.append("")
    out.append(f"{DIM}Night mode: no updates{RESET}")
    print("\n".join(out), flush=True)


def main():
    stop_name_cache = STOP

    try:
        while True:
            now_dt = dt.datetime.now()
            if in_quiet_hours(now_dt):
                render_quiet(stop_name_cache)
                time.sleep(QUIET_REFRESH)
                continue

            r = requests.get(URL, timeout=10)
            r.raise_for_status()
            data = r.json()
            stop_obj = data.get(STOP) or next(iter(data.values()))
            stop_name_cache = (stop_obj.get("description") or STOP).strip()

            calls = (stop_obj.get("calls") or [])[:10]
            top3 = calls[:3] if len(calls) >= 3 else calls
            if not top3:
                top3 = [{}]

            catch_idx = choose_catchable(top3)
            next_sleep = choose_refresh_seconds(top3, catch_idx)

            render_departures(stop_name_cache, top3, catch_idx, next_sleep)
            time.sleep(next_sleep)

    except KeyboardInterrupt:
        print(SHOW_CURSOR, end="", flush=True)
        print("\nBye.")
    except Exception as e:
        print(SHOW_CURSOR, end="", flush=True)
        raise


if __name__ == "__main__":
    main()
