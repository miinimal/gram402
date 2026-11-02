"""Screen recording of the owner page during the PayPal demo (headless Chromium, real page, real server).

    python3 demo/record_owner.py create BASE STATE OUT_DIR   # owner creates a mandate in the UI; writes STATE (0600)
    python3 demo/record_owner.py watch  BASE STATE OUT_DIR STOPFILE   # owner watches spending until STOPFILE appears

Each call leaves one .webm in OUT_DIR. Used by the paypal-live workflow; works the same against simulated PayPal.
Author: Aleksandr Shcherbakov (@miinimal).
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

VIEW = {"width": 1920, "height": 1080}
ZOOM = "document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.zoom = '1.6'; });"


def browser(p, out_dir):
    b = p.chromium.launch()
    ctx = b.new_context(viewport=VIEW, color_scheme="dark", record_video_dir=out_dir, record_video_size=VIEW,
                        locale="en-US", timezone_id=os.environ.get("DEMO_TZ", "America/Los_Angeles"))
    ctx.add_init_script(ZOOM)
    return b, ctx, ctx.new_page()


def typed(pg, sel, text):
    pg.fill(sel, "")
    pg.type(sel, text, delay=90)


def create(base, state, out_dir):
    with sync_playwright() as p:
        b, ctx, pg = browser(p, out_dir)
        pg.goto(base + "/mandates")
        pg.wait_for_timeout(1500)
        typed(pg, "#budget", "0.25")
        typed(pg, "#cap", "0.10")
        typed(pg, "#days", "7")
        pg.wait_for_timeout(600)
        pg.click("#make")
        pg.wait_for_selector("#made:not([hidden])", timeout=60_000)
        m = {"mandate": pg.text_content("#mid"), "secret": pg.text_content("#msec"),
             "approve_url": pg.get_attribute("#approve", "href")}
        pg.locator("#approve").scroll_into_view_if_needed()
        pg.wait_for_timeout(3500)
        ctx.close()
        b.close()
    fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(m, f)
    print(m["approve_url"])


def watch(base, state, out_dir, stopfile, limit_s=1500):
    with open(state) as f:
        m = json.load(f)
    with sync_playwright() as p:
        b, ctx, pg = browser(p, out_dir)
        pg.goto(base + "/mandates")
        pg.fill("#qid", m["mandate"])
        pg.fill("#qsec", m["secret"])
        pg.locator("#look").scroll_into_view_if_needed()
        end = time.time() + limit_s
        while time.time() < end:
            pg.click("#check")
            pg.wait_for_timeout(500)
            pg.locator("#card").scroll_into_view_if_needed() if pg.is_visible("#card") else None
            if os.path.exists(stopfile):
                pg.wait_for_timeout(1500)
                pg.click("#check")
                pg.wait_for_timeout(3000)
                break
            pg.wait_for_timeout(2500)
        ctx.close()
        b.close()


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "create":
        create(*sys.argv[2:5])
    elif mode == "watch":
        watch(*sys.argv[2:6])
    else:
        raise SystemExit(__doc__)
