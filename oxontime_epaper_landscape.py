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

MODE = os.environ.get("MODE", "grid").lower()  # grid (default) or list
WALK_MIN = int(os.environ.get("WALK_MIN", "5"))
LEAVE_BUFFER_MIN = int(os.environ.get("LEAVE_BUFFER_MIN", "0"))  # add 1 if you want margin

# Refresh policy
DAY_REFRESH = int(os.environ.get("DAY_REFRESH", "180"))          # normal
FAST_REFRESH = int(os.environ.get("FAST_REFRESH", "60"))         # “minute tick”
FAST_WINDOW_MIN = int(os.environ.get("FAST_WINDOW_MIN", "15"))   # use FAST_REFRESH if catchable ETA <= this

QUIET_START = int(os.environ.get("QUIET_START", "22"))
QUIET_END = int(os.environ.get("QUIET_END", "6"))
QUIET_REFRESH = int(os.environ.get("QUIET_REFRESH", "1800"))

# Panel (landscape)
W, H = 250, 122


# ----------------------------
# Helpers
# ----------------------------
def load_font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

def load_fonts():
    # Use DejaVu; present on Raspberry Pi OS
    base = "/usr/share/fonts/truetype/dejavu/"
    return {
        "hdr":      load_font(base + "DejaVuSans.ttf", 12),
        "small":    load_font(base + "DejaVuSans.ttf", 14),
        "bold":     load_font(base + "DejaVuSans-Bold.ttf", 16),

        # Huge digit sizes — we’ll auto-fit down if needed.
        "digit_96": load_font(base + "DejaVuSans-Bold.ttf", 96),
        "digit_88": load_font(base + "DejaVuSans-Bold.ttf", 88),
        "digit_80": load_font(base + "DejaVuSans-Bold.ttf", 80),
        "digit_72": load_font(base + "DejaVuSans-Bold.ttf", 72),
        "digit_64": load_font(base + "DejaVuSans-Bold.ttf", 64),
        "digit_56": load_font(base + "DejaVuSans-Bold.ttf", 56),
        "digit_48": load_font(base + "DejaVuSans-Bold.ttf", 48),

        # List mode fonts
        "list_big": load_font(base + "DejaVuSans-Bold.ttf", 22),
        "list_sm":  load_font(base + "DejaVuSans.ttf", 14),
    }

def in_quiet_hours(now: dt.datetime) -> bool:
    # window may wrap midnight
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
    return None

def minutes_until_clock(hhmm: str, now: dt.datetime):
    try:
        hh, mm = map(int, hhmm.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now:
            target += dt.timedelta(days=1)
        return max(0, int((target - now).total_seconds() // 60))
    except Exception:
        return None

def minutes_only(call, now: dt.datetime):
    """
    Always return minutes as display text + numeric eta.
    "5 min" -> ("5", 5)
    "21:47" -> ("12", 12) (computed)
    else -> ("--", None)
    """
    disp = (call.get("display_time") or "").strip()
    if not disp:
        return "--", None

    eta = parse_minutes(disp)
    if eta is not None:
        return ("99+" if eta > 99 else str(eta)), eta

    if ":" in disp:
        eta2 = minutes_until_clock(disp, now)
        if eta2 is not None:
            return ("99+" if eta2 > 99 else str(eta2)), eta2

    return "--", None

def choose_catchable(top3):
    """
    Choose earliest ETA >= WALK_MIN (minutes-only logic).
    Fallback to 0 if none parseable.
    """
    now = dt.datetime.now()
    best_idx = None
    best_eta = None
    for i, c in enumerate(top3):
        _, eta = minutes_only(c, now)
        if eta is None:
            continue
        if eta >= WALK_MIN and (best_eta is None or eta < best_eta):
            best_eta, best_idx = eta, i
    return best_idx if best_idx is not None else 0

def choose_refresh(top3, catch_idx):
    now = dt.datetime.now()
    _, eta = minutes_only(top3[catch_idx], now)
    if eta is not None and eta <= FAST_WINDOW_MIN:
        return FAST_REFRESH
    return DAY_REFRESH

def text_bbox(draw: ImageDraw.ImageDraw, text: str, font):
    return draw.textbbox((0, 0), text, font=font)

def fit_font_to_box(draw: ImageDraw.ImageDraw, text: str, fonts_in_order, box_w: int, box_h: int):
    """
    Pick the largest font that fits within box_w/box_h.
    """
    for f in fonts_in_order:
        b = text_bbox(draw, text, f)
        tw, th = b[2] - b[0], b[3] - b[1]
        if tw <= box_w and th <= box_h:
            return f
    return fonts_in_order[-1]


# ----------------------------
# Rendering: GRID (huge minutes)
# ----------------------------
def draw_grid(epd, fonts, top3, catch_idx):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now()
    now_txt = now.strftime("%H:%M")

    # Minimal header: current time only (small)
    db.text((4, 2), now_txt, font=fonts["hdr"], fill=0)

    # Leave-now logic based on catchable bus ETA
    catch_txt, catch_eta = minutes_only(top3[catch_idx], now)
    leave_now = (catch_eta is not None) and (catch_eta <= (WALK_MIN + LEAVE_BUFFER_MIN))

    if leave_now:
        # Red indicator at top-right
        msg = "LEAVE"
        b = text_bbox(dr, msg, fonts["bold"])
        dr.text((W - 4 - (b[2]-b[0]), 2), msg, font=fonts["bold"], fill=0)

    # 3 columns across, using most of the height
    margin_x = 4
    gap = 4
    col_w = (W - margin_x * 2 - gap * 2) // 3

    # Box area for digits
    top_y = 18
    bottom_pad = 2
    box_h = H - top_y - bottom_pad

    digit_fonts = [
        fonts["digit_96"], fonts["digit_88"], fonts["digit_80"],
        fonts["digit_72"], fonts["digit_64"], fonts["digit_56"], fonts["digit_48"]
    ]

    for i in range(3):
        call = top3[i] if i < len(top3) else {}
        disp_txt, eta = minutes_only(call, now)

        x0 = margin_x + i * (col_w + gap)
        y0 = top_y

        emphasize = (i == catch_idx)
        d = dr if emphasize else db

        # Make emphasized slightly larger by allowing a bigger font list first
        font = fit_font_to_box(d, disp_txt, digit_fonts, col_w, box_h)

        # Center the digits
        b = text_bbox(d, disp_txt, font)
        tw, th = b[2]-b[0], b[3]-b[1]
        cx = x0 + (col_w - tw) // 2
        cy = y0 + (box_h - th) // 2

        d.text((cx, cy), disp_txt, font=font, fill=0)

        # Optional small red dot indicator (walkable) in emphasized column
        if emphasize and eta is not None and eta >= WALK_MIN:
            dr.ellipse((x0 + col_w - 10, y0 + 2, x0 + col_w - 4, y0 + 8), fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))


# ----------------------------
# Rendering: LIST (legacy option)
# ----------------------------
def draw_list(epd, fonts, stop_obj, top3, catch_idx):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    title = (stop_obj.get("description") or STOP).strip()
    hdr = f"{title} {now}"
    db.text((4, 2), hdr[:28], font=fonts["hdr"], fill=0)

    ys = [22, 60, 84]
    for i, y in enumerate(ys):
        call = top3[i] if i < len(top3) else {}
        route = (call.get("route_code") or "")[:3]
        dest = (call.get("destination_name") or "")
        disp = (call.get("display_time") or "")
        line = f"{route} {dest} {disp}"
        if i == catch_idx:
            dr.text((4, y), line[:28], font=fonts["list_big"], fill=0)
        else:
            db.text((4, y), line[:34], font=fonts["list_sm"], fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))


# ----------------------------
# Quiet screen
# ----------------------------
def draw_quiet(epd, fonts):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4, 2), f"Night {now}", font=fonts["hdr"], fill=0)
    dr.text((4, 28), "Buses are sleeping.", font=fonts["list_big"], fill=0)
    db.text((4, 68), "So are we :)", font=fonts["list_sm"], fill=0)
    db.text((4, 92), f"Back {QUIET_END:02d}:00", font=fonts["list_sm"], fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))
    epd.sleep()


# ----------------------------
# Main
# ----------------------------
def main():
    fonts = load_fonts()
    epd = epd2in13b_V4.EPD()
    epd.init()

    try:
        while True:
            now = dt.datetime.now()

            if in_quiet_hours(now):
                draw_quiet(epd, fonts)
                time.sleep(QUIET_REFRESH)
                epd.init()
                continue

            data = requests.get(URL, timeout=10).json()
            stop_obj = data.get(STOP) or next(iter(data.values()))
            calls = (stop_obj.get("calls") or [])[:3]
            if not calls:
                calls = [{}]

            catch_idx = choose_catchable(calls)

            if MODE == "list":
                draw_list(epd, fonts, stop_obj, calls, catch_idx)
            else:
                draw_grid(epd, fonts, calls, catch_idx)

            time.sleep(choose_refresh(calls, catch_idx))

    except KeyboardInterrupt:
        pass
    finally:
        try:
            epd.sleep()
        except Exception:
            pass


if __name__ == "__main__":
    main()
