#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Admission-opening checker for a list of school websites.

Fetches each school's homepage/admissions page and runs two independent
detectors:

1. HIGH-CONFIDENCE keyword match: specific admission phrases ("فتح باب
   التقديم", "سجل الآن", ...) or "Apply Now" together with a 2027/2028 year
   mention. On a NEW match -> sends an email (full Arabic detail) AND places
   an automated phone call (unless it's currently quiet hours).

2. MEDIUM-CONFIDENCE silent-change watchdog: some schools may flip
   admissions open/closed WITHOUT ever changing the wording to mention the
   year at all (the "Apply Now" button/link area just changes quietly). This
   watchdog snapshots the text around every "Apply Now" occurrence and, if it
   changes from what was last seen, sends an EMAIL ONLY (no phone call,
   lower confidence, worth a manual look) - it never re-fires for the same
   text.

State (both the notified high-confidence snippets and the watchdog
baselines) is persisted in state.json so nothing re-notifies on every run;
the GitHub Actions workflow commits this file back to the repo after
each run.
"""

import os
import json
import smtplib
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from urllib.parse import urlparse

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCHOOLS = [
    {"name": "ISEE (International School for Elite Education)", "url": "https://isee-eg.com"},
    {"name": "ISC - EBIS", "url": "https://www.isc.edu.eg/ebis/"},
    {"name": "NIS (Nile International School)", "url": "https://nis-eg.com"},
]

KEYWORDS = [
    "فتح باب التقديم",
    "باب التقديم",
    "سجل الآن",
    "سجل الان",
    "Apply Now",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

CONTEXT_CHARS = 120  # characters of surrounding text to capture in the snippet
REQUEST_TIMEOUT = 30

# "Apply Now" is very often a *permanent* nav-bar/footer button on school sites
# (it links to a generic, evergreen application form) - it does NOT reliably
# mean a new admissions cycle just opened. To avoid false-positive alerts from
# that permanent button, we only count an "Apply Now" match as HIGH confidence
# if the surrounding text also mentions the academic year we care about. The
# more specific Arabic phrases are left as-is since they're far less likely to
# be permanent site chrome.
GENERIC_KEYWORDS_REQUIRE_YEAR_HINT = {"apply now"}
YEAR_HINTS = ["2027", "2028", "27/28", "27-28"]

# --- Quiet hours for phone calls only (emails are never restricted) ---------
# Doha asked for calls to stop at night. Times are in Egypt/Riyadh local time
# (both are UTC+3 in this period - Egypt is on DST until Oct 29, 2026).
QUIET_HOURS_TZ = "Africa/Cairo"
QUIET_HOURS_START = 22  # 10 PM - calls suppressed from this hour...
QUIET_HOURS_END = 6     # ...through (not including) 6 AM.

# --- Medium-confidence silent-change watchdog -------------------------------
# Catches a school quietly flipping admissions open/closed near an "Apply Now"
# button WITHOUT ever mentioning 2027/2028 anywhere (so the high-confidence
# check above would never fire). This is intentionally noisier but email-only.
WATCH_KEYWORD = "apply now"
WATCH_CONTEXT_CHARS = 400


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Fetching / matching
# ---------------------------------------------------------------------------

def fetch_text(url):
    """Download a page and return its visible text (scripts/styles stripped)."""
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def find_matches(text):
    """Return a list of (keyword, snippet) tuples for every HIGH-confidence keyword occurrence."""
    matches = []
    lowered = text.lower()
    for kw in KEYWORDS:
        kw_lower = kw.lower()
        start = 0
        while True:
            idx = lowered.find(kw_lower, start)
            if idx == -1:
                break
            snippet_start = max(0, idx - CONTEXT_CHARS)
            snippet_end = min(len(text), idx + len(kw) + CONTEXT_CHARS)
            snippet = " ".join(text[snippet_start:snippet_end].split())

            if kw_lower in GENERIC_KEYWORDS_REQUIRE_YEAR_HINT:
                # Look a bit wider than the snippet itself for a year hint,
                # since the year might sit just outside the tight snippet window.
                wide_start = max(0, idx - CONTEXT_CHARS * 3)
                wide_end = min(len(text), idx + len(kw) + CONTEXT_CHARS * 3)
                wide_context = text[wide_start:wide_end]
                if not any(hint in wide_context for hint in YEAR_HINTS):
                    start = idx + len(kw)
                    continue  # generic button/link with no year context - skip

            matches.append((kw, snippet))
            start = idx + len(kw)
    return matches


def get_watch_contexts(text):
    """
    Return a sorted list of normalized wide-context strings around every
    occurrence of WATCH_KEYWORD, regardless of year hints. Used only to
    detect *silent* changes near the Apply Now button/link over time.
    """
    lowered = text.lower()
    contexts = []
    start = 0
    while True:
        idx = lowered.find(WATCH_KEYWORD, start)
        if idx == -1:
            break
        ctx_start = max(0, idx - WATCH_CONTEXT_CHARS)
        ctx_end = min(len(text), idx + len(WATCH_KEYWORD) + WATCH_CONTEXT_CHARS)
        contexts.append(" ".join(text[ctx_start:ctx_end].split()))
        start = idx + len(WATCH_KEYWORD)
    return sorted(contexts)


def in_quiet_hours():
    """True if it's currently within the no-phone-calls window (local Cairo/Riyadh time)."""
    now = datetime.now(ZoneInfo(QUIET_HOURS_TZ))
    h = now.hour
    if QUIET_HOURS_START > QUIET_HOURS_END:
        # window wraps past midnight, e.g. 22 -> 6
        return h >= QUIET_HOURS_START or h < QUIET_HOURS_END
    return QUIET_HOURS_START <= h < QUIET_HOURS_END


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_email(subject, body):
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_APP_PASSWORD")
    recipients = [r.strip() for r in os.environ.get("EMAIL_RECIPIENTS", "").split(",") if r.strip()]

    if not sender or not password or not recipients:
        print("[email] Missing EMAIL_SENDER / EMAIL_APP_PASSWORD / EMAIL_RECIPIENTS - skipping.")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"[email] Sent to {recipients}")
    except Exception as e:
        print(f"[email] Failed to send: {e}")


def send_phone_call(short_message):
    """
    Place an automated phone call via CallMeBot's call API to each configured
    number, which reads `short_message` aloud with a robot voice. Skipped
    entirely during quiet hours (see in_quiet_hours()).

    NOTE: CallMeBot's free instant-WhatsApp-text signup is currently closed to
    new users ("This Bot is full"), so we use their phone-call API instead,
    which is still open. Keep `short_message` short, in English, and free of
    newlines/special characters (it's read aloud and passed in a URL).
    """
    if in_quiet_hours():
        print(f"[call] Suppressed (quiet hours, {QUIET_HOURS_TZ}) - would have said: {short_message}")
        return

    pairs = [
        (os.environ.get("CALLMEBOT_PHONE_1"), os.environ.get("CALLMEBOT_APIKEY_1")),
        (os.environ.get("CALLMEBOT_PHONE_2"), os.environ.get("CALLMEBOT_APIKEY_2")),
    ]
    for phone, apikey in pairs:
        if not phone or not apikey:
            continue
        try:
            resp = requests.get(
                "https://api.callmebot.com/call.php",
                params={"phone": phone, "text": short_message, "apikey": apikey},
                timeout=REQUEST_TIMEOUT,
            )
            print(f"[call] {phone} -> HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[call] Failed for {phone}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    any_new = False

    for school in SCHOOLS:
        name, url = school["name"], school["url"]
        host = urlparse(url).netloc
        school_state = state.get(host, {"notified_snippets": []})

        try:
            text = fetch_text(url)
        except Exception as e:
            print(f"[{name}] Error fetching {url}: {e}")
            state[host] = school_state
            continue

        high_confidence_fired = False

        # --- HIGH confidence: specific phrases / Apply Now + year hint ---
        matches = find_matches(text)
        if not matches:
            print(f"[{name}] No high-confidence admission keywords found.")
        else:
            already_notified = set(school_state.get("notified_snippets", []))

            # Multiple keywords can match inside the same (overlapping) chunk
            # of text (e.g. "باب التقديم" is a substring of "فتح باب التقديم"),
            # which would otherwise produce several near-identical
            # notifications for one real announcement. Group by the exact
            # snippet text and send one notification per unique snippet.
            snippet_to_keywords = {}
            for kw, snippet in matches:
                if snippet in already_notified:
                    continue
                snippet_to_keywords.setdefault(snippet, []).append(kw)

            if not snippet_to_keywords:
                print(f"[{name}] Keyword(s) present but already notified for this exact text.")
            else:
                for snippet, kws in snippet_to_keywords.items():
                    kw_label = ", ".join(dict.fromkeys(kws))  # de-dup, keep order
                    print(f"[{name}] HIGH-CONFIDENCE MATCH -> keyword(s)='{kw_label}' snippet='{snippet}'")

                    subject = f"\U0001F393 تنبيه: باب التقديم مفتوح - {name}"
                    email_body = (
                        f"تم رصد إشارة إلى فتح باب التقديم على موقع المدرسة التالية:\n\n"
                        f"المدرسة: {name}\n"
                        f"الرابط: {url}\n"
                        f"الكلمة/الكلمات المطابقة: {kw_label}\n\n"
                        f"النص كما ظهر بالضبط على الصفحة:\n\"{snippet}\"\n\n"
                        f"(تم إرسال هذا التنبيه تلقائياً بواسطة سكريبت مراقبة مواقع المدارس)"
                    )
                    # Kept short, in English, no newlines: read aloud by a
                    # robot voice over a phone call, not a chat message.
                    call_msg = f"Admissions alert. {name} may have opened admissions. Please check your email now."

                    send_email(subject, email_body)
                    send_phone_call(call_msg)

                    already_notified.add(snippet)
                    any_new = True
                    high_confidence_fired = True

                school_state["notified_snippets"] = list(already_notified)

        # --- MEDIUM confidence: silent change watchdog (email only) ---
        watch_contexts = get_watch_contexts(text)
        baseline = school_state.get("apply_now_watch_baseline")

        if baseline is None:
            # First time watching this school - just establish the baseline.
            school_state["apply_now_watch_baseline"] = watch_contexts
            print(f"[{name}] Watchdog baseline established ({len(watch_contexts)} Apply-Now area(s)).")
        elif watch_contexts != baseline and not high_confidence_fired:
            print(f"[{name}] WATCHDOG: content near 'Apply Now' changed since last check.")
            subject = f"\U0001F440 ملاحظة: حصل تغيير حوالين زرار Apply Now - {name} (يستحق المراجعة اليدوية)"
            new_text_preview = "\n---\n".join(watch_contexts) if watch_contexts else "(لم يعد الزر موجود في الصفحة)"
            email_body = (
                f"لاحظ السكريبت تغييراً في محتوى الصفحة القريب من زرار/رابط \"Apply Now\" في موقع:\n\n"
                f"المدرسة: {name}\n"
                f"الرابط: {url}\n\n"
                f"هذا تنبيه بثقة أقل من التنبيه العادي (مفيش كلمة صريحة زي 2027/2028)، "
                f"يعني ممكن يكون يكون يكون تحديث بسيط أو حقيقي لفتح التقديم - يُنصح تتأكدي بنفسك بزيارة الموقع.\n\n"
                f"النص الحالي حوالين الزرار:\n{new_text_preview}\n\n"
                f"(تنبيه آلي - لن يتكرر لنفس النص)"
            )
            send_email(subject, email_body)
            school_state["apply_now_watch_baseline"] = watch_contexts
            any_new = True
        elif watch_contexts != baseline and high_confidence_fired:
            # Already alerted at high confidence this run - just refresh baseline quietly.
            school_state["apply_now_watch_baseline"] = watch_contexts

        state[host] = school_state

    save_state(state)

    if not any_new:
        print("Run complete - no new admission announcements this time.")


if __name__ == "__main__":
    main()
