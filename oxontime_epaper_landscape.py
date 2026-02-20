#!/usr/bin/env python3
import os
import time
import datetime as dt
import requests
from PIL import Image, ImageDraw, ImageFont

from waveshare_epd import epd2in13b_V4

# ----------------------------
# Config
# ----------------------------
STOP = os.environ.get("OXON_STOP", "340000022GEO")
URL = f"https://oxontime.com/pwi/departureBoard/{STOP}"

WALK_MIN = int(os.environ.get("WALK_MIN", "5"))

DAY_REFRESH = int(os.environ.get("DAY_REFRESH", "180"))
FAST_REFRESH = int(os.environ.get("FAST_REFRESH", "60"))
FAST_WINDOW_MIN = int(os.environ.get("FAST_WINDOW_MIN", "10"))

QUIET_START = int(os.environ.get("QUIET_START", "22"))
QUIET_END = int(os.environ.get("QUIET_END", "6"))
QUIET_REFRESH = int(os.environ.get("QUIET_REFRESH", "1800"))

# Landscape canvas for 2.13" (250x122)
W, H = 250, 122


def load_fonts():
    try:
        big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        hdr = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        return big, sm, hdr
    except Exception:
        f = ImageFont.load_default()
        return f, f, f


def in_quiet_hours(now: dt.datetime) -> bool:
    # Quiet window may wrap midnight
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


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: float, font):
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    t = text
    while t and draw.textlength(t + ell, font=font) > max_w:
        t = t[:-1]
    return (t + ell) if t else ell


def choose_catchable(top_calls):
    """
    Choose earliest bus whose ETA (minutes) is >= WALK_MIN.
    If none parseable, fall back to first.
    """
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


def draw_departures(epd, font_big, font_sm, font_hdr, stop_obj, top3, catch_idx):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    title = (stop_obj.get("description") or STOP).strip()
    header = f"{title}  {now}"
    db.text((4, 2), fit_text(db, header, W - 8, font_hdr), font=font_hdr, fill=0)

    # Layout
    y_big = 22
    y_sm1 = 60
    y_sm2 = 84
    margin = 4

    def draw_line(call, y, font, emphasize=False):
        route, dest, disp, eta = fmt_call(call)
        route = route[:3]  # compact
        right = disp

        time_w = db.textlength(right, font=font) + 6
        route_w = db.textlength(route + " ", font=font)
        max_dest_w = W - (margin * 2) - time_w - route_w

        dest_txt = fit_text(db, dest, max_dest_w, font)
        left_txt = f"{route} {dest_txt}".strip()

        d = dr if emphasize else db
        d.text((margin, y), left_txt, font=font, fill=0)

        rx = W - margin - int(db.textlength(right, font=font))
        d.text((rx, y), right, font=font, fill=0)

        # Little red dot near time if emphasized + walkable ETA
        if emphasize and eta is not None and eta >= WALK_MIN:
            dr.ellipse((W - margin - 6, y + 6, W - margin - 2, y + 10), fill=0)

    # Big line = catchable; two small = the other two in original order
    big_call = top3[catch_idx]
    others = [top3[i] for i in range(len(top3)) if i != catch_idx]
    while len(others) < 2:
        others.append({})

    draw_line(big_call, y_big, font_big, emphasize=True)
    draw_line(others[0], y_sm1, font_sm, emphasize=False)
    draw_line(others[1], y_sm2, font_sm, emphasize=False)

    footer = f"Walk {WALK_MIN} min"
    db.text((4, 106), fit_text(db, footer, W - 8, font_hdr), font=font_hdr, fill=0)

    # IMPORTANT: do NOT call epd.sleep() here for periodic refresh use
    epd.display(epd.getbuffer(black), epd.getbuffer(red))


def draw_quiet_screen(epd, font_big, font_sm, font_hdr):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4, 2), f"Night mode  {now}", font=font_hdr, fill=0)

    dr.text((4, 30), "Buses are sleeping.", font=font_big, fill=0)
    db.text((4, 68), "So are we. 😴", font=font_sm, fill=0)
    db.text((4, 92), f"Back at {QUIET_END:02d}:00.", font=font_sm, fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))
    epd.sleep()


def main():
    epd = epd2in13b_V4.EPD()
    font_big, font_sm, font_hdr = load_fonts()

    # Init once
    epd.init()

    try:
        while True:
            now_dt = dt.datetime.now()

            if in_quiet_hours(now_dt):
                draw_quiet_screen(epd, font_big, font_sm, font_hdr)
                time.sleep(QUIET_REFRESH)
                # wake for next day update
                epd.init()
                continue

            r = requests.get(URL, timeout=10)
            r.raise_for_status()
            data = r.json()
            stop_obj = data.get(STOP) or next(iter(data.values()))
            calls = (stop_obj.get("calls") or [])[:10]

            top3 = calls[:3]
            if not top3:
                top3 = [{}]

            catch_idx = choose_catchable(top3)
            draw_departures(epd, font_big, font_sm, font_hdr, stop_obj, top3, catch_idx)

            time.sleep(choose_refresh_seconds(top3, catch_idx))

    except KeyboardInterrupt:
        pass

    except OSError:
        # Common recovery for SPI handle issues: re-init and keep going a bit slower
        try:
            epd.init()
        except Exception:
            pass
        time.sleep(DAY_REFRESH)

    finally:
        # Leave the panel in a safe state
        try:
            epd.sleep()
        except Exception:
            pass


if __name__ == "__main__":
    main()
