# Gmail Subscription Digest

A daily email digest agent that fetches your newsletter subscriptions, summarizes them with Gemini AI, and sends a clean HTML digest back to you — then archives the originals.

## Features

- Per-subscription custom prompts (different summarization style per sender)
- Link-following: fetches full article content for teaser-only newsletters
- One digest email per subscription, sent to yourself
- Archives original emails after processing
- Runs unattended via cron

## Setup

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Get Gmail OAuth credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. Enable the **Gmail API**
3. Go to **Audience** → add your Gmail as a test user
4. Create an **OAuth 2.0 Client ID** (Desktop app) → download JSON → save as `credentials.json`

### 3. Configure

Copy `.env.example` to `.env` and add your Gemini API key:
```
GEMINI_API_KEY=your_key_here
```

Edit `config.yaml` — set your email and add subscriptions:
```yaml
settings:
  my_email: "you@gmail.com"

subscriptions:
  - name: "My Newsletter"
    sender: "newsletter@example.com"
    prompt: |
      Summarize in 5 bullet points.
```

### 4. First run (OAuth)

```bash
source venv/bin/activate
python gmail_digest.py --dry-run
```

A browser window will open for one-time Gmail authorization. The token is saved to `token.json` for all future runs.

## Usage

```bash
# Send digest + archive originals
python gmail_digest.py

# Send digest but skip archiving (safe for testing)
python gmail_digest.py --dry-run
```

## Cron (daily at 7 AM)

```
0 7 * * * /path/to/subscription-digest/venv/bin/python /path/to/subscription-digest/gmail_digest.py >> /path/to/subscription-digest/digest.log 2>&1
```

## Config options

| Field | Description |
|-------|-------------|
| `sender` | Sender email address to fetch from |
| `prompt` | Gemini summarization prompt |
| `follow_link: auto` | Gemini picks the main article URL to fetch full content |
| `follow_link: "text"` | Match `<a>` anchor text to find the link |
| `max_emails_per_sender` | Safety cap on emails processed per run (default: 3) |
| `look_back_days` | How far back to search for unread emails (default: 30) |
