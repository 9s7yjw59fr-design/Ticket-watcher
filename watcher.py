"""Theatre ticket watcher for Linnateater and Draamateater.

  python watcher.py            normal run (scheduled every 5 min)
  python watcher.py calibrate  saves page snapshots into the repo (calibration/)
  python watcher.py test       sends a test alert
"""
import base64, html, json, os, random, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests, yaml

TZ = ZoneInfo("Europe/Tallinn")
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DEBUG = ROOT / "debug"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
SET = CFG["settings"]
MODE = (sys.argv[1] if len(sys.argv) > 1 else "watch").strip().lower()
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

ET_DAYS = {"E": 0, "T": 1, "K": 2, "N": 3, "R": 4, "L": 5, "P": 6}
EN_DAYS = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- helpers ----------

def now():
    return datetime.now(TZ)


def parse(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def load_state():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    for k in ("sessions", "checked", "warned", "burst_done"):
        st.setdefault(k, {})
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2, ensure_ascii=False))


def notify(title, text, url=None, urgent=True):
    print(f"[ALERT] {title} | {text} | {url}")
    if TG_TOKEN and TG_CHAT:
        msg = f"{title}\n{text}" + (f"\n{url}" if url else "")
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": msg,
                                "disable_notification": not urgent}, timeout=15)
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
    if NTFY_TOPIC:
        body = {"topic": NTFY_TOPIC, "title": title, "message": text,
                "priority": 5 if urgent else 3}
        if url:
            body["click"] = url
        requests.post("https://ntfy.sh", json=body, timeout=10)


def commit_file(repo_path, data):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        old = requests.get(url, headers=h, timeout=20)
        body = {"message": f"calibration: {repo_path}", "content": base64.b64encode(data).decode()}
        if old.status_code == 200:
            body["sha"] = old.json()["sha"]
        print(f"Committed {repo_path}: {requests.put(url, headers=h, json=body, timeout=60).status_code}")
    except Exception as e:
        print(f"Commit {repo_path} failed: {e}")


def show_date(day, month, hhmm):
    """Build the show datetime, rolling into next year when the month has passed."""
    t = now()
    year = t.year + (1 if month < t.month - 1 else 0)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(year, month, day, h, m, tzinfo=TZ)


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ").strip()


# ---------- page parsers: return list of sessions ----------
# session = {"title", "when" (datetime), "available" (bool), "link"}

def parse_linnateater(page, base_url):
    out = []
    for b in re.split(r'class="schedule__session-content', page)[1:]:
        t = re.search(r'schedule__session-title[^>]*>([^<]+)<', b)
        d = re.search(r'schedule__session-date">\s*(\w)\s+(\d{1,2})\.(\d{1,2})', b)
        tm = re.search(r'schedule__session-time">\s*(\d{1,2}:\d{2})', b)
        btn = re.search(r'<a href="([^"]*)" class="button schedule__session-button([^"]*)"', b)
        if not (t and d and tm and btn):
            continue
        out.append({"title": clean(t.group(1)),
                    "when": show_date(int(d.group(2)), int(d.group(3)), tm.group(1)),
                    "available": "is-disabled" not in btn.group(2),
                    "link": btn.group(1) if btn.group(1).startswith("http") else base_url})
    return out


def parse_draamateater(page, base_url):
    out = []
    title = re.search(r"<title>([^<]+?)\s*-\s*Draamateater", page)
    for row in re.findall(r"<tr>(.*?)</tr>", page, flags=re.S):
        d = re.search(r"performances-table__date--short[^>]*>\s*<span>\w+&nbsp;</span>\s*(\d{1,2})\.(\d{1,2})", row)
        tm = re.search(r"cell--time\">\s*(\d{1,2}:\d{2})", row)
        if not (d and tm):
            continue
        actions = row.split("cell--actions", 1)[-1]
        buy = re.search(r'href="([^"]+)"', actions)
        out.append({"title": clean(title.group(1)) if title else "",
                    "when": show_date(int(d.group(1)), int(d.group(2)), tm.group(1)),
                    "available": bool(buy) and "Sold out" not in actions,
                    "link": html.unescape(buy.group(1)) if buy else base_url})
    return out


PARSERS = {"linnateater": parse_linnateater, "draamateater": parse_draamateater}
_cache = {}


def fetch(url):
    if url not in _cache:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        _cache[url] = r.text
    return _cache[url]


def sessions_for(watch):
    page = fetch(watch["url"])
    found = PARSERS[watch["theatre"]](page, watch["url"])
    match = [m.lower() for m in watch.get("match", [])]
    exact = [m.lower() for m in watch.get("match_exact", [])]
    if not (match or exact):
        return found
    return [s for s in found
            if any(m in s["title"].lower() for m in match) or s["title"].lower() in exact]


# ---------- checking ----------

def check(watch, state, t):
    try:
        sessions = [s for s in sessions_for(watch) if s["when"] > t]
    except Exception as e:
        print(f"{watch['name']}: could not read page ({e})")
        return
    preferred = SET.get("preferred_days", ["Thu", "Fri", "Sat"])
    avail = sum(s["available"] for s in sessions)
    print(f"{t:%H:%M:%S} {watch['name']}: {len(sessions)} dates, {avail} with tickets")

    new_dates = []
    for s in sessions:
        key = f"{watch['name']}|{s['when'].isoformat()}"
        was = state["sessions"].get(key)
        state["sessions"][key] = "available" if s["available"] else "sold_out"
        day = DAY_NAMES[s["when"].weekday()]
        label = f"{day} {s['when']:%d.%m %H:%M}"
        if was is None:
            new_dates.append(label)
        if s["available"] and was != "available":
            star = "Thu-Sat" if day in preferred else "other day"
            notify(f"Tickets: {watch['name']}",
                   f"{label} ({star}) has tickets. Buy {SET.get('tickets', 2)} now.",
                   s["link"], urgent=day in preferred or SET.get("alert_all_days", True))

    first_run = not any(k.startswith(watch["name"] + "|") for k in state.get("seen_before", {}))
    if new_dates and not first_run:
        notify(f"New dates: {watch['name']}", "Listed: " + ", ".join(new_dates[:10]),
               watch["url"], urgent=False)
    state.setdefault("seen_before", {})[f"{watch['name']}|"] = True
    state["checked"][watch["name"]] = t.isoformat()


def due(watch, state, t):
    keys = [k for k, v in state["sessions"].items() if k.startswith(watch["name"] + "|")]
    soon = any(t <= parse(k.split("|", 1)[1]) <= t + timedelta(hours=SET["near_show_hours"]) for k in keys)
    interval = SET["near_show_interval_min"] if soon else SET["normal_interval_min"]
    last = state["checked"].get(watch["name"])
    return not last or (t - parse(last)).total_seconds() >= interval * 60 - 60


def handle_openings(state):
    for op in CFG.get("sale_openings") or []:
        start = parse(op["opens"])
        end = start + timedelta(minutes=op.get("duration_min", 30))
        key = f"{op['opens']}|{op['theatre']}"
        t = now()
        if start - timedelta(minutes=20) <= t < start and key not in state["warned"]:
            notify("Ticket sale opens soon",
                   f"{op['theatre']} sale opens at {start:%H:%M}. The watcher checks every "
                   f"{SET['burst_interval_s']} s from then.", op.get("url"))
            state["warned"][key] = t.isoformat()
            save_state(state)
        if start - timedelta(minutes=12) <= t <= end and key not in state["burst_done"]:
            wait = (start - timedelta(minutes=1) - now()).total_seconds()
            if wait > 0:
                time.sleep(wait)
            watches = [w for w in CFG["watches"] if w["theatre"] == op["theatre"]]
            while now() <= end:
                t0 = time.time()
                _cache.clear()
                for w in watches:
                    check(w, state, now())
                save_state(state)
                time.sleep(max(0, SET["burst_interval_s"] - (time.time() - t0)))
            state["burst_done"][key] = now().isoformat()
            save_state(state)


# ---------- main ----------

def main():
    if MODE == "test":
        notify("Ticket watcher test", "Alerts are working.")
        return

    if MODE == "calibrate":
        DEBUG.mkdir(exist_ok=True)
        for n, w in enumerate(CFG["watches"], 1):
            try:
                page = fetch(w["url"])
            except Exception as e:
                print(f"Could not open {w['url']}: {e}")
                continue
            slug = f"{n:02d}-{w['theatre']}"
            (DEBUG / f"{slug}.html").write_text(page)
            commit_file(f"calibration/{slug}.html", page.encode())
            found = sessions_for(w)
            print(f"{w['name']}: {len(found)} dates, {sum(s['available'] for s in found)} with tickets")
        return

    if SET.get("paused"):
        print("Watcher paused in config.yaml (settings: paused: true)")
        return

    state = load_state()
    handle_openings(state)
    t = now()
    for w in CFG["watches"]:
        if due(w, state, t):
            check(w, state, t)
            time.sleep(random.uniform(*SET["delay_seconds"]))
    save_state(state)


if __name__ == "__main__":
    main()
