"""Theatre ticket watcher for Linnateater (Piletilevi) and Draamateater (Piletimaailm).

  python watcher.py            normal run (scheduled every 5 min)
  python watcher.py calibrate  saves screenshots + HTML of every page to debug/
  python watcher.py test       sends a test alert to your phone
"""
import json, os, random, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests, yaml
from playwright.sync_api import sync_playwright

TZ = ZoneInfo("Europe/Tallinn")
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DEBUG = ROOT / "debug"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
SET = CFG["settings"]
MODE = sys.argv[1] if len(sys.argv) > 1 else "watch"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- helpers ----------

def now():
    return datetime.now(TZ)


def parse(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def load_state():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for k in ("status", "checked", "warned", "burst_done"):
        st.setdefault(k, {})
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2))


def notify(title, text, url=None, urgent=True):
    print(f"[ALERT] {title} | {text} | {url}")
    if NTFY_TOPIC:
        body = {"topic": NTFY_TOPIC, "title": title, "message": text,
                "priority": 5 if urgent else 3, "tags": ["performing_arts"]}
        if url:
            body["click"] = url
        requests.post("https://ntfy.sh", json=body, timeout=10)
    if TG_TOKEN and TG_CHAT:
        msg = f"{title}\n{text}" + (f"\n{url}" if url else "")
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg}, timeout=10)


def send_file(path, caption):
    if TG_TOKEN and TG_CHAT:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": TG_CHAT, "caption": caption[:1000]},
                          files={"document": f}, timeout=60)


def pause():
    time.sleep(random.uniform(*SET["delay_seconds"]))


def targets(watch):
    for u in watch["urls"]:
        if isinstance(u, str):
            yield u, None
        else:
            yield u["url"], parse(u["show"]) if u.get("show") else None


# ---------- browser ----------

def run_steps(page, steps, pkey):
    user = os.environ.get(f"{pkey.upper()}_USER", "")
    pwd = os.environ.get(f"{pkey.upper()}_PASS", "")
    for step in steps:
        if "goto" in step:
            page.goto(step["goto"], wait_until="domcontentloaded")
        elif "click" in step:
            page.locator(step["click"]).first.click(timeout=10000)
        elif "fill" in step:
            page.locator(step["fill"]).first.fill(
                step["value"].replace("$USER", user).replace("$PASS", pwd))
        page.wait_for_timeout(1500)


class Session:
    """One logged-in browser tab per ticket platform."""

    def __init__(self, browser, basket_on):
        self.browser, self.basket_on, self.pages = browser, basket_on, {}

    def page(self, pkey):
        if pkey not in self.pages:
            p = self.browser.new_context(locale="et-EE").new_page()
            steps = CFG["platforms"][pkey]["login_steps"]
            if self.basket_on and steps:
                try:
                    run_steps(p, steps, pkey)
                except Exception as e:
                    notify("Ticket watcher: login failed", f"{pkey}: {e}", urgent=False)
            self.pages[pkey] = p
        return self.pages[pkey]


def status_of(page, platform):
    text = page.inner_text("body").lower()
    if any(s.lower() in text for s in platform["sold_out_texts"]):
        return "sold_out"
    if page.locator(platform["buy_selector"]).count():
        return "available"
    return "unknown"


def check(session, watch, url, state):
    pkey = watch["platform"]
    platform = CFG["platforms"][pkey]
    page = session.page(pkey)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        print(f"Could not open {url}: {e}")
        return
    status = status_of(page, platform)
    was = state["status"].get(url)
    state["status"][url] = status
    state["checked"][url] = now().isoformat()
    print(f"{now():%H:%M:%S} {watch['name']}: {status}")

    if status == "available" and was != "available":
        text = "Tickets are available."
        link = url
        if session.basket_on and platform["basket_steps"]:
            try:
                run_steps(page, platform["basket_steps"], pkey)
                DEBUG.mkdir(exist_ok=True)
                page.screenshot(path=DEBUG / "basket.png")
                text = f"{watch['tickets']} tickets are in your basket. Pay now, the hold is short."
                link = platform["checkout_url"]
            except Exception as e:
                text = f"Available, but adding to basket failed ({e}). Buy from the link."
        notify(watch["name"], text, link)


# ---------- schedule logic ----------

def due(url, show, state, t):
    if show and show < t:
        return False                       # show is over
    near = show and show - t <= timedelta(hours=SET["near_show_hours"])
    interval = SET["near_show_interval_min"] if near else SET["normal_interval_min"]
    last = state["checked"].get(url)
    return not last or (t - parse(last)).total_seconds() >= interval * 60 - 60


def handle_openings(session, state):
    for op in CFG.get("sale_openings") or []:
        start = parse(op["opens"])
        end = start + timedelta(minutes=op.get("duration_min", 30))
        key = f"{op['opens']}|{op['platform']}"
        t = now()

        if start - timedelta(minutes=20) <= t < start and key not in state["warned"]:
            notify("Ticket sale opens soon",
                   f"{op['platform']} sale opens at {start:%H:%M}. Get your phone ready, "
                   f"the watcher will also try from {start:%H:%M}.",
                   CFG["platforms"][op["platform"]]["checkout_url"])
            state["warned"][key] = t.isoformat()
            save_state(state)

        if start - timedelta(minutes=12) <= t <= end and key not in state["burst_done"]:
            run_burst(session, op, start, end, state)
            state["burst_done"][key] = now().isoformat()
            save_state(state)


def run_burst(session, op, start, end, state):
    wait = (start - timedelta(minutes=1) - now()).total_seconds()
    if wait > 0:
        print(f"Waiting {int(wait)} s for sale opening")
        time.sleep(wait)
    watches = [w for w in CFG["watches"] if w["platform"] == op["platform"]]
    pairs = [(w, u) for w in watches for u, _ in targets(w)]
    if op.get("urls"):
        name = f"New sale ({op['platform']})"
        pairs = [({"name": name, "platform": op["platform"],
                   "tickets": op.get("tickets", 2)}, u) for u in op["urls"]]
    while now() <= end:
        loop_start = time.time()
        for w, u in pairs:
            check(session, w, u, state)
        time.sleep(max(0, SET["burst_interval_s"] - (time.time() - loop_start)))


# ---------- main ----------

def main():
    if MODE == "test":
        notify("Ticket watcher test", "Alerts are working.", "https://linnateater.ee/en/schedule")
        return

    if MODE == "watch" and SET.get("paused"):
        print("Watcher paused in config.yaml (settings: paused: true)")
        return

    state = load_state()
    basket_on = SET["add_to_basket"] and MODE == "watch"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        session = Session(browser, basket_on)

        if MODE == "calibrate":
            DEBUG.mkdir(exist_ok=True)
            n = 0
            for w in CFG["watches"]:
                for u, _ in targets(w):
                    n += 1
                    p = session.page(w["platform"])
                    try:
                        p.goto(u, wait_until="networkidle", timeout=45000)
                        p.wait_for_timeout(3000)
                    except Exception as e:
                        notify("Calibration", f"Could not open {u}: {e}", urgent=False)
                        continue
                    slug = f"{n:02d}-{w['platform']}"
                    p.screenshot(path=DEBUG / f"{slug}.png", full_page=True)
                    (DEBUG / f"{slug}.txt").write_text(f"{w['name']}\n{u}\n\n" + p.inner_text("body"))
                    (DEBUG / f"{slug}.html").write_text(p.content())
                    send_file(DEBUG / f"{slug}.txt", f"{w['name']} (text)")
                    send_file(DEBUG / f"{slug}.png", f"{w['name']} (screenshot)")
                    print(f"Saved {u} as {slug}")
                    pause()
        else:
            handle_openings(session, state)
            t = now()
            for w in CFG["watches"]:
                for u, show in targets(w):
                    if due(u, show, state, t):
                        check(session, w, u, state)
                        pause()
        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()
