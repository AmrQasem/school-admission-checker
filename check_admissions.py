#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Admission-opening checker for a list of school websites.

Fetches each school's homepage/admissions page, looks for a set of
"admissions open" keywords (Arabic + English), and sends a WhatsApp
message (via CallMeBot) and an email whenever a NEW match is found
(i.e. one that hasn't already triggered a notification before), including
the exact surrounding text found on the page.

State is persisted in state.json so the same match doesn't re-notify
on every run (the GitHub Actions workflow commits this file back to
the repo after each run).
"""

import os
import json
import smtplib
import ssl
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
    """Return a list of (keyword, snippet) tuples for every keyword occurrence."""
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
            matches.append((kw, snippet))
            start = idx + len(kw)
    return matches


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
    number, which reads `short_message` aloud with a robot voice.

    NOTE: CallMeBot's free instant-WhatsApp-text signup is currently closed to
    new users ("This Bot is full"), so we use their phone-call API instead,
    which is still open. Keep `short_message` short, in English, and free of
    newlines/special characters (it's read aloud and passed in a URL).
    """
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

        matches = find_matches(text)
        if not matches:
            print(f"[{name}] No admission keywords found.")
            state[host] = school_state
            continue

        already_notified = set(school_state.get("notified_snippets", []))

        # Multiple keywords can match inside the same (overlapping) chunk of text
        # (e.g. "باب التقديم" is a substring of "فتح باب التقديم"), which would
        # otherwise produce several near-identical notifications for one real
        # announcement. Group by the exact snippet text and send one notification
        # per unique snippet, listing every keyword that matched inside it.
        snippet_to_keywords = {}
        for kw, snippet in matches:
            if snippet in already_notified:
                continue
            snippet_to_keywords.setdefault(snippet, []).append(kw)

        if not snippet_to_keywords:
            print(f"[{name}] Keyword(s) present but already notified for this exact text.")
            state[host] = school_state
            continue

        for snippet, kws in snippet_to_keywords.items():
            kw_label = ", ".join(dict.fromkeys(kws))  # de-dup while preserving order
            print(f"[{name}] NEW MATCH -> keyword(s)='{kw_label}' snippet='{snippet}'")

            subject = f"\U0001F393 تنبيه: باب التقديم مفتوح - {name}"
            email_body = (
                f"تم رصد إشارة إلى فتح باب التقديم على موقع المدرسة التالية:\n\n"
                f"المدرسة: {name}\n"
                f"الرابط: {url}\n"
                f"الكلمة/الكلمات المطابقة: {kw_label}\n\n"
                f"النص كما ظهر بالضبط على الصفحة:\n\"{snippet}\"\n\n"
                f"(تم إرسال هذا التنبيه تلقائياً بواسطة سكريبت مراقبة مواقع المدارس)"
            )
            # Kept short, in English, no newlines: this is read aloud by a robot
            # voice over a phone call, not sent as a chat message.
            call_msg = f"Admissions alert. {name} may have opened admissions. Please check your email now."

            send_email(subject, email_body)
            send_phone_call(call_msg)

            already_notified.add(snippet)
            any_new = True

        school_state["notified_snippets"] = list(already_notified)
        state[host] = school_state

    save_state(state)

    if not any_new:
        print("Run complete - no new admission announcements this time.")


if __name__ == "__main__":
    main()
