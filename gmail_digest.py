#!/usr/bin/env python3
"""
Gmail Subscription Digest Agent
Fetches subscription emails, summarizes them with Gemini, sends a digest, and archives originals.
"""

import argparse
import base64
import os
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path

import markdown as md
import requests
import yaml
import google.generativeai as genai
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
CONFIG_FILE = SCRIPT_DIR / "config.yaml"

def load_dotenv(path: Path):
    """Load key=value pairs from a .env file into os.environ (does not override existing vars)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


# ── HTML stripping ────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def strip_html(html: str) -> str:
    parser = _HTMLStripper()
    parser.feed(html)
    text = parser.get_text()
    # Collapse excessive whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(
                    "ERROR: credentials.json not found.\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials.\n"
                    f"Place it at: {CREDENTIALS_FILE}"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Fetch emails ──────────────────────────────────────────────────────────────

def _decode_body(payload) -> tuple[str, str]:
    """Recursively extract body from a Gmail message payload.
    Returns (plain_text, raw_html). raw_html is empty string if no HTML part found.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime_type == "text/plain" and body_data:
        text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        return text, ""

    if mime_type == "text/html" and body_data:
        html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
        return strip_html(html), html

    # multipart — recurse, prefer HTML part so links are available
    plain, html = "", ""
    for part in payload.get("parts", []):
        p, h = _decode_body(part)
        if h and not html:
            html = h
        if p and not plain:
            plain = p

    return plain or strip_html(html), html


# Domains to exclude when auto-picking a link
_NOISE_DOMAINS = {"twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
                  "youtube.com", "unsubscribe", "mailto:", "tel:"}


def _fetch_url(url: str) -> str:
    """Follow redirects and return stripped page text, or '' on failure."""
    try:
        resp = requests.get(url, allow_redirects=True, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return strip_html(resp.text)
    except Exception as e:
        print(f"  Warning: could not fetch {url}: {e}")
        return ""


def resolve_follow_link(email_body: str, email_html: str, follow_link: str, model_name: str) -> str:
    """Resolve and fetch the content linked from the email.

    follow_link == "auto"  → ask Gemini to pick the main article URL
    follow_link == <text>  → find <a> whose anchor text contains that text (case-insensitive)
    Returns stripped page text, or "" on failure (caller uses email body as fallback).
    """
    soup = BeautifulSoup(email_html, "html.parser")

    if follow_link.lower() == "auto":
        # Collect candidate links, filtering out noise
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(noise in href.lower() for noise in _NOISE_DOMAINS):
                continue
            text = a.get_text(strip=True)
            if href.startswith("http") and text:
                candidates.append(f"{text}: {href}")

        if not candidates:
            return ""

        link_list = "\n".join(candidates[:30])  # cap to avoid huge prompts
        meta_prompt = (
            "Given the email below, identify which single URL leads to the main article or content page. "
            "Reply with ONLY the URL, nothing else.\n\n"
            f"Email:\n{email_body[:3000]}\n\nCandidate links:\n{link_list}"
        )
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(meta_prompt)
            url = response.text.strip().split()[0]  # take first token in case of extra text
            if url.startswith("http"):
                print(f"    Gemini picked: {url}")
                return _fetch_url(url)
        except Exception as e:
            print(f"  Warning: Gemini link selection failed: {e}")
        return ""

    else:
        # Text-match mode
        for a in soup.find_all("a", href=True):
            if follow_link.lower() in a.get_text(strip=True).lower():
                return _fetch_url(a["href"])
        print(f"  Warning: no link found matching '{follow_link}'")
        return ""


def fetch_subscription_emails(service, subscription: dict, look_back_days: int = 2) -> list[dict]:
    """Return up to max_emails unread messages from the given sender."""
    sender = subscription["sender"]
    max_emails = subscription.get("max_emails", 3)
    query = f"from:{sender} is:unread newer_than:{look_back_days}d"

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_emails
        ).execute()
    except HttpError as e:
        print(f"  Gmail API error for {sender}: {e}")
        return []

    messages = result.get("messages", [])
    if not messages:
        return []

    emails = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "(no subject)")
        body, html = _decode_body(msg["payload"])
        emails.append({"id": msg_ref["id"], "subject": subject, "body": body, "html": html})

    return emails


# ── Summarize ─────────────────────────────────────────────────────────────────

def summarize_with_gemini(body: str, prompt: str, model_name: str) -> str:
    full_prompt = f"{prompt.strip()}\n\n---\n\n{body[:30000]}"  # cap at 30k chars
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(full_prompt)
    return response.text.strip()


# ── Build digest email ────────────────────────────────────────────────────────

_MD_EXTENSIONS = ["nl2br", "sane_lists"]

_SUMMARY_STYLE = """
<style>
  .summary ul { padding-left: 20px; margin: 8px 0; }
  .summary ol { padding-left: 20px; margin: 8px 0; }
  .summary li { margin-bottom: 6px; line-height: 1.6; }
  .summary h2, .summary h3 { color: #333; margin: 16px 0 6px; font-size: 15px; }
  .summary p  { margin: 6px 0; line-height: 1.6; }
  .summary strong { color: #111; }
</style>
"""

def build_digest_html(results: list[dict]) -> str:
    sections = []
    for item in results:
        name = item["name"]
        emails_html = ""
        for email in item["emails"]:
            summary_html = md.markdown(email["summary"], extensions=_MD_EXTENSIONS)
            emails_html += f"""
            <div style="margin-bottom:20px">
              <p style="color:#888;font-size:12px;margin:0 0 8px">
                <em>{email['subject']}</em>
              </p>
              <div class="summary">{summary_html}</div>
            </div>"""

        sections.append(f"""
        <div style="margin-bottom:36px">
          <h2 style="color:#1a1a1a;border-bottom:2px solid #e0e0e0;padding-bottom:6px;font-size:18px">{name}</h2>
          {emails_html}
        </div>""")

    body = "\n".join(sections)
    today = date.today().strftime("%A, %B %-d, %Y")

    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  {_SUMMARY_STYLE}
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:680px;margin:0 auto;padding:24px;color:#222">
  <h1 style="color:#1a1a1a;margin-bottom:4px">Daily Digest</h1>
  <p style="color:#888;margin-top:0;margin-bottom:32px">{today}</p>
  {body}
  <hr style="border:none;border-top:1px solid #e0e0e0;margin-top:40px">
  <p style="color:#aaa;font-size:12px">Generated by gmail-digest agent</p>
</body>
</html>"""


# ── Send email ────────────────────────────────────────────────────────────────

def send_digest_email(service, to: str, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["From"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ── Archive ───────────────────────────────────────────────────────────────────

def archive_email(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["INBOX"]},
    ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    load_dotenv(SCRIPT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Gmail Subscription Digest Agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and summarize emails but do NOT send digest or archive originals.",
    )
    args = parser.parse_args()

    # Load config
    config = yaml.safe_load(CONFIG_FILE.read_text())
    subscriptions = config["subscriptions"]
    settings = config["settings"]
    my_email = settings["my_email"]
    model_name = settings.get("gemini_model", "gemini-1.5-flash")
    look_back_days = settings.get("look_back_days", 30)
    max_per_sender = settings.get("max_emails_per_sender", 3)
    today_str = date.today().strftime("%Y-%m-%d")


    # Init Gemini
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable not set.")
        sys.exit(1)
    genai.configure(api_key=api_key)

    # Init Gmail
    print("Authenticating with Gmail...")
    service = get_gmail_service()

    # Process each subscription independently
    for sub in subscriptions:
        name = sub["name"]
        prompt = sub["prompt"]
        sub["max_emails"] = max_per_sender
        print(f"\n[{name}] Fetching emails from {sub['sender']}...")

        emails = fetch_subscription_emails(service, sub, look_back_days)
        if not emails:
            print(f"  No new emails.")
            continue

        print(f"  Found {len(emails)} email(s). Summarizing...")
        follow_link = sub.get("follow_link", "")
        processed = []
        email_ids = []
        for email in emails:
            print(f"  → {email['subject'][:60]}")
            content = email["body"]
            if follow_link and email.get("html"):
                print(f"    Resolving follow_link: '{follow_link}'...")
                linked = resolve_follow_link(email["body"], email["html"], follow_link, model_name)
                if linked:
                    content = linked
                    print(f"    Fetched {len(linked)} chars from linked page.")
                else:
                    print(f"    Link not found or fetch failed, using email body.")
            summary = summarize_with_gemini(content, prompt, model_name)
            processed.append({"subject": email["subject"], "summary": summary})
            email_ids.append(email["id"])

        subject = f"{name} — {today_str}"
        html_body = build_digest_html([{"name": name, "emails": processed}])

        print(f"  Sending to {my_email}...")
        send_digest_email(service, my_email, subject, html_body)
        print(f"  Sent.")

        if args.dry_run:
            print(f"  [DRY RUN] Skipping archive of {len(email_ids)} email(s).")
        else:
            print(f"  Archiving {len(email_ids)} original email(s)...")
            for msg_id in email_ids:
                archive_email(service, msg_id)
            print(f"  Done.")


if __name__ == "__main__":
    main()
