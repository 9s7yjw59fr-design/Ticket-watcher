"""Login test for Piletilevi and Piletimaailm.

Logs in with the PILETILEVI_* and PILETIMAAILM_* secrets, reports the result
to Telegram and commits a short text log (no passwords, no emails, no page
content after login) to calibration/login-<site>.txt so the steps can be tuned.
"""
import base64, os, re, time
import requests
from playwright.sync_api import sync_playwright

SITES = [
    ("piletilevi", "https://www.piletilevi.ee/", "PILETILEVI_USER", "PILETILEVI_PASS"),
]
COOKIE_WORDS = ["Luban kõik küpsised", "Nõustun", "Nõustu", "Luba kõik", "Aktsepteeri", "Accept all", "Accept", "Allow all", "OK"]
LOGIN_WORDS = ["Logi sisse", "Sisene", "Sisselogimine", "Log in", "Login", "Sign in", "Minu konto", "My account"]
STEP_WORDS = ["E-post", "E-mail", "Email", "Parool", "Parooliga", "Password", "Jätka", "Continue", "Edasi"]
LOGGED_IN_WORDS = ["Logi välja", "Välju", "Log out", "Logout", "Sign out"]
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


def tg(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print("[TG]", text)
    if tok and chat:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=15)


def commit(path, text):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    old = requests.get(url, headers=h, timeout=20)
    body = {"message": f"login test: {path}", "content": base64.b64encode(text.encode()).decode()}
    if old.status_code == 200:
        body["sha"] = old.json()["sha"]
    print("commit", path, requests.put(url, headers=h, json=body, timeout=60).status_code)


def safe(s):
    s = re.sub(r"\S+@\S+", "[email]", s or "")
    return re.sub(r"\s+", " ", s).strip()[:80]


def snapshot(page, log, label):
    """Record what is on screen: URL path, inputs and button/link texts (before login only)."""
    try:
        log.append(f"\n## {label}\nurl: {page.url.split('?')[0]}\ntitle: {safe(page.title())}")
        for el in page.locator("input:visible").all()[:15]:
            log.append(f"input type={el.get_attribute('type')} name={el.get_attribute('name')} "
                       f"placeholder={safe(el.get_attribute('placeholder'))}")
        texts = []
        for el in page.locator("button:visible, a:visible, [role=button]:visible").all()[:60]:
            t = safe(el.inner_text(timeout=500)) or safe(el.get_attribute("aria-label"))
            if t and t not in texts:
                texts.append(t)
        log.append("clickables: " + " | ".join(texts[:40]))
    except Exception as e:
        log.append(f"snapshot error: {e}")


EMAIL_SEL = ("input[type=email], input[name*=mail i], input[autocomplete=username], "
             "input[name*=user i], input[name*=login i]")
OPENER_RE = re.compile(r"login|logi|sisene|account|konto|profile|profiil|kasutaja|user|minu", re.I)


def header_items(page):
    items = page.locator("header a, header button, nav a, nav button, [class*=header i] a, "
                         "[class*=header i] button, [class*=user i], [class*=account i], [class*=profile i]")
    out = []
    for i in range(min(items.count(), 60)):
        el = items.nth(i)
        try:
            if not el.is_visible():
                continue
            desc = " ".join(f"{a}={safe(el.get_attribute(a))}" for a in ("href", "aria-label", "title", "class")
                            if el.get_attribute(a))
            out.append((el, desc))
        except Exception:
            pass
    return out


def login_email(page):
    """A visible login email box, skipping the footer newsletter signup."""
    loc = page.locator(EMAIL_SEL)
    for i in range(min(loc.count(), 10)):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            bad = el.evaluate("e => !!e.closest('footer, [class*=newsletter i], [class*=subscribe i], [id*=newsletter i]')")
            if not bad:
                return el
        except Exception:
            pass
    return None


def click_text(scope, words, timeout=2500):
    for w in words:
        loc = scope.get_by_role("button", name=re.compile(re.escape(w), re.I))
        if loc.count() == 0:
            loc = scope.get_by_text(re.compile(rf"^\s*{re.escape(w)}\s*$", re.I))
        for i in range(min(loc.count(), 3)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click(timeout=timeout)
                    return w
            except Exception:
                pass
    return None


def visible(page, selector):
    loc = page.locator(selector)
    return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 5)))


def login(p, site, home, user, pw, log):
    b = p.chromium.launch()
    ctx = b.new_context(user_agent=UA, viewport={"width": 390, "height": 844}, locale="et-EE")
    page = ctx.new_page()
    try:
        page.goto(home, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        log.append(f"cookie banner: {click_text(page, COOKIE_WORDS)}")
        snapshot(page, log, "home")

        try:
            page.locator("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll").click(timeout=3000)
            log.append("cookiebot accepted")
        except Exception:
            pass
        time.sleep(1)
        pw_sel = "input[type=password]"
        items = header_items(page)
        log.append("\n## header items\n" + "\n".join(d for _, d in items[:40]))
        hit = None
        for el, desc in items:
            if OPENER_RE.search(desc):
                try:
                    el.click(timeout=3000)
                    hit = desc
                    break
                except Exception:
                    pass
        if not hit:
            hit = click_text(page, LOGIN_WORDS)
        log.append(f"login opener: {hit}")
        time.sleep(3)
        snapshot(page, log, "after opening login")

        if not login_email(page) and not visible(page, pw_sel):
            log.append(f"step button: {click_text(page, STEP_WORDS)}")
            time.sleep(2)
            snapshot(page, log, "after step button")

        email = login_email(page)
        if not email:
            return "no login email field found (newsletter box ignored)"
        email.fill(user)
        if not visible(page, pw_sel):
            email.press("Enter")
            time.sleep(3)
            log.append(f"after email, step: {click_text(page, STEP_WORDS)}")
            time.sleep(3)
            snapshot(page, log, "after email step")
        if not visible(page, pw_sel):
            return "no password field after email (site may offer only Smart-ID or code login)"
        page.locator(pw_sel).locator("visible=true").first.fill(pw)
        page.locator(pw_sel).locator("visible=true").first.press("Enter")
        time.sleep(6)

        # After login: record only yes/no facts, never page content.
        still_pw = visible(page, pw_sel)
        out = any(page.get_by_text(re.compile(re.escape(w), re.I)).count() for w in LOGGED_IN_WORDS)
        err = page.get_by_text(re.compile(r"vale|incorrect|invalid|ebaõnnestus|wrong|vigane", re.I)).count() > 0
        log.append(f"\n## after submit\nurl: {page.url.split('?')[0]}\npassword field still visible: {still_pw}\n"
                   f"logout text found: {out}\nerror text found: {err}")
        if err:
            return "site shows an error (check email/password in the secrets)"
        if out or not still_pw:
            return "OK"
        return "unclear, password form still showing"
    except Exception as e:
        return f"error: {type(e).__name__}: {str(e)[:120]}"
    finally:
        b.close()


def main():
    results = []
    with sync_playwright() as p:
        for site, home, uk, pk in SITES:
            user, pw = os.environ.get(uk), os.environ.get(pk)
            log = [f"# {site} login test"]
            if not (user and pw):
                res = f"secret {uk} or {pk} missing"
            else:
                res = login(p, site, home, user, pw, log)
            log.append(f"\nRESULT: {res}")
            commit(f"calibration/login-{site}.txt", "\n".join(log))
            print("\n".join(log))
            results.append(f"{site}: {res}")
            time.sleep(5)
    tg("Ticket watcher login test\n" + "\n".join(results))


if __name__ == "__main__":
    main()
