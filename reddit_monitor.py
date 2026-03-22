#!/usr/bin/env python3
"""
Reddit Monitor for Maquete.ai
Scans architecture/design subreddits for potential customers,
scores posts with Claude, drafts replies, and sends a daily email digest.
"""

import json
import os
import time
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import resend
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
RESEND_FROM = os.environ.get("RESEND_FROM", "Maquete Monitor <monitor@maquete.ai>")

SEEN_POSTS_FILE = Path(os.environ.get("SEEN_POSTS_FILE", "seen_posts.json"))
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "7"))

SUBREDDITS = [
    "SketchUp",
    "architecture",
    "ArchiCAD",
    "revit",
    "Blender",
    "InteriorDesign",
    "architects",
    "archviz",
    "Rhino",
]

KEYWORDS = [
    "render photorealistic",
    "AI render architecture",
    "SketchUp render",
    "vray enscape lumion alternative",
    "render cost time slow",
    "interior exterior render",
    "archviz visualization",
    "twinmotion d5 corona",
]

USER_AGENT = "MaqueteMonitor/1.0 (monitoring architecture subreddits)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("reddit_monitor")

# ── Seen-posts persistence ───────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_POSTS_FILE.exists():
        data = json.loads(SEEN_POSTS_FILE.read_text())
        return set(data)
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_POSTS_FILE.write_text(json.dumps(sorted(seen), indent=2))


# ── Reddit search (public JSON API, no auth needed) ─────────────────────────

def search_subreddit(sub_name: str, keyword: str) -> list[dict]:
    """Search a subreddit using Reddit's public JSON endpoint."""
    params = urllib.parse.urlencode({
        "q": keyword,
        "sort": "new",
        "t": "day",
        "limit": 25,
        "restrict_sr": "on",
    })
    url = f"https://www.reddit.com/r/{sub_name}/search.json?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return [child["data"] for child in data.get("data", {}).get("children", [])]
    except Exception as e:
        log.warning("Error searching r/%s for '%s': %s", sub_name, keyword, e)
        return []


def fetch_posts(seen: set[str]) -> list[dict]:
    """Search each subreddit for keyword matches from the last 24 hours."""
    posts = {}
    cutoff = time.time() - 86400  # 24 hours ago

    for sub_name in SUBREDDITS:
        for keyword in KEYWORDS:
            results = search_subreddit(sub_name, keyword)
            for post in results:
                post_id = post.get("id", "")
                if post_id in seen or post_id in posts:
                    continue
                if post.get("created_utc", 0) < cutoff:
                    continue
                posts[post_id] = {
                    "id": post_id,
                    "subreddit": sub_name,
                    "title": post.get("title", ""),
                    "selftext": (post.get("selftext") or "")[:2000],
                    "url": f"https://reddit.com{post.get('permalink', '')}",
                    "author": post.get("author", "[deleted]"),
                    "created_utc": post.get("created_utc", 0),
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0),
                }

            # Rate limit: Reddit allows ~1 req/sec for unauthenticated
            time.sleep(1.5)

    log.info("Found %d unique new posts across %d subreddits", len(posts), len(SUBREDDITS))
    return list(posts.values())


# ── Claude scoring & reply drafting ──────────────────────────────────────────

SCORING_PROMPT = """\
You are a marketing analyst for Maquete.ai — an AI rendering tool that transforms \
SketchUp/CAD model screenshots into photorealistic architectural renders in seconds.

Analyze this Reddit post and return JSON (no markdown fences):

{
  "score": <1-10>,
  "reason": "<1-2 sentence explanation>",
  "reply": "<drafted reply or null>"
}

Scoring guide:
- 10: Actively asking for an AI rendering tool or comparing rendering solutions
- 8-9: Asking how to create photorealistic renders, struggling with rendering workflow
- 7: Discussing rendering pain points, slow renders, expensive tools
- 5-6: General rendering discussion but not seeking a solution
- 3-4: Tangentially related (showing finished renders, general architecture)
- 1-2: Unrelated to rendering needs

For posts scoring 7+, draft a reply that:
- References their specific words/situation
- Answers their actual question helpfully (like a real architect would)
- Naturally mentions Maquete.ai near the end as something worth checking out
- Is NOT spammy — genuinely helpful first, promotional second
- Keeps it under 150 words

For posts below 7, set "reply" to null.

Post from r/{subreddit}:
Title: {title}
Body: {body}
"""


def score_and_draft(client: anthropic.Anthropic, post: dict) -> dict | None:
    """Use Claude to score the post and optionally draft a reply."""
    prompt = SCORING_PROMPT.format(
        subreddit=post["subreddit"],
        title=post["title"],
        body=post["selftext"] or "(no body text)",
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        result = json.loads(text)
        post["ai_score"] = result["score"]
        post["ai_reason"] = result["reason"]
        post["ai_reply"] = result.get("reply")
        return post
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.warning("Failed to parse Claude response for post %s: %s", post["id"], e)
        return None
    except anthropic.APIError as e:
        log.error("Anthropic API error for post %s: %s", post["id"], e)
        return None


# ── Email digest ─────────────────────────────────────────────────────────────

def build_email_html(leads: list[dict]) -> str:
    """Build the HTML email digest."""
    if not leads:
        return "<p>No high-scoring posts found in the last 24 hours.</p>"

    rows = []
    for lead in sorted(leads, key=lambda x: x["ai_score"], reverse=True):
        reply_html = ""
        if lead.get("ai_reply"):
            escaped_reply = lead["ai_reply"].replace("\n", "<br>")
            reply_html = f"""
            <div style="background:#f0f0f0;padding:12px;border-radius:6px;margin-top:8px;font-size:14px;">
                <strong>Drafted Reply:</strong><br>{escaped_reply}
            </div>"""

        rows.append(f"""
        <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <span style="background:#C4A882;color:white;padding:4px 10px;border-radius:12px;font-weight:bold;font-size:14px;">
                    Score: {lead['ai_score']}/10
                </span>
                <span style="color:#666;font-size:13px;">r/{lead['subreddit']}</span>
            </div>
            <h3 style="margin:10px 0 4px;">
                <a href="{lead['url']}" style="color:#1A1916;text-decoration:none;">{lead['title']}</a>
            </h3>
            <p style="color:#666;font-size:13px;margin:0 0 8px;">
                by u/{lead['author']} · {lead['num_comments']} comments · {lead['score']} upvotes
            </p>
            <p style="font-size:14px;color:#444;margin:0 0 8px;">
                <strong>Why:</strong> {lead['ai_reason']}
            </p>
            {reply_html}
        </div>
        """)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:0 auto;padding:20px;">
        <div style="text-align:center;margin-bottom:24px;">
            <h1 style="color:#1A1916;font-size:24px;margin:0;">Maquete.ai Reddit Monitor</h1>
            <p style="color:#888;font-size:14px;margin:4px 0 0;">
                {len(leads)} potential lead{'s' if len(leads) != 1 else ''} found · {datetime.now(timezone.utc).strftime('%B %d, %Y')}
            </p>
        </div>
        {''.join(rows)}
        <p style="text-align:center;color:#aaa;font-size:12px;margin-top:24px;">
            Generated by Reddit Monitor for Maquete.ai
        </p>
    </div>
    """


def send_digest(leads: list[dict]) -> None:
    """Send the daily digest email via Resend."""
    resend.api_key = RESEND_API_KEY

    count = len(leads)
    subject = f"Maquete Reddit Monitor: {count} lead{'s' if count != 1 else ''} found" if count else "Maquete Reddit Monitor: No leads today"

    resend.Emails.send({
        "from": RESEND_FROM,
        "to": [NOTIFY_EMAIL],
        "subject": subject,
        "html": build_email_html(leads),
    })
    log.info("Digest email sent to %s with %d leads", NOTIFY_EMAIL, count)


# ── Main ─────────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Starting Reddit monitor run")

    seen = load_seen()
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    posts = fetch_posts(seen)

    if not posts:
        log.info("No new posts found")
        send_digest([])
        return

    leads = []
    for i, post in enumerate(posts):
        log.info("Scoring post %d/%d: %s", i + 1, len(posts), post["title"][:60])
        result = score_and_draft(claude, post)
        if result:
            seen.add(result["id"])
            if result["ai_score"] >= SCORE_THRESHOLD:
                leads.append(result)
                log.info("  → Score %d/10 (lead!)", result["ai_score"])
            else:
                log.info("  → Score %d/10", result["ai_score"])

        # Be polite to APIs
        if i < len(posts) - 1:
            time.sleep(0.5)

    save_seen(seen)
    log.info("Processed %d posts, found %d leads (score >= %d)", len(posts), len(leads), SCORE_THRESHOLD)

    send_digest(leads)
    log.info("Run complete")


if __name__ == "__main__":
    run()