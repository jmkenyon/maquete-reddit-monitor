# Reddit Monitor for Maquete.ai

Scans architecture/design subreddits for potential customers, scores posts with Claude, drafts helpful replies, and sends a daily email digest.

## What it does

1. Searches r/SketchUp, r/architecture, r/ArchiCAD, r/revit, r/Blender, r/InteriorDesign for rendering-related posts
2. Uses Claude to score each post 1–10 for purchase intent
3. For posts scoring 7+, drafts a genuine, helpful reply that naturally mentions Maquete.ai
4. Sends an email digest via Resend with all leads, scores, reasons, and ready-to-paste replies
5. Tracks seen posts in a local JSON file to avoid duplicates

## Setup

### 1. Get API credentials

- **Reddit**: Create an app at https://www.reddit.com/prefs/apps (choose "script" type)
- **Anthropic**: Get a key at https://console.anthropic.com/settings/keys
- **Resend**: Get a key at https://resend.com/api-keys (verify your sending domain)

### 2. Install locally

```bash
cd reddit-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
```

### 3. Test it

```bash
python reddit_monitor.py
```

### 4. Run as a cron job (local)

Run daily at 9 AM:

```bash
crontab -e
```

Add:

```
0 9 * * * cd /path/to/reddit-monitor && /path/to/.venv/bin/python reddit_monitor.py >> /var/log/reddit-monitor.log 2>&1
```

## Deploy on Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Reddit monitor for Maquete.ai"
gh repo create maquete-reddit-monitor --private --push
```

### 2. Create Railway project

1. Go to https://railway.app and create a new project from your GitHub repo
2. Railway will detect the Python project automatically

### 3. Add environment variables

In the Railway dashboard, go to **Variables** and add all values from `.env.example`.

Also set:
```
SEEN_POSTS_FILE=/data/seen_posts.json
```

### 4. Add a persistent volume

In **Settings → Volumes**, add a volume mounted at `/data` so `seen_posts.json` persists across deploys.

### 5. Configure as a cron job

In **Settings → Deploy**, set:
- **Start Command**: `python reddit_monitor.py`
- **Cron Schedule**: `0 9 * * *` (daily at 9 AM UTC)
- **Restart Policy**: Never (it's a one-shot script)

Railway will spin up the worker on schedule, run the script, then shut it down.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `SCORE_THRESHOLD` | Minimum score to include in digest | `7` |
| `SEEN_POSTS_FILE` | Path to dedup file | `seen_posts.json` |
| `RESEND_FROM` | Sender address for digest emails | `Maquete Monitor <monitor@maquete.ai>` |

## Customization

- **Subreddits**: Edit the `SUBREDDITS` list in `reddit_monitor.py`
- **Keywords**: Edit the `KEYWORDS` list
- **Scoring criteria**: Edit the `SCORING_PROMPT` string
- **Score threshold**: Set `SCORE_THRESHOLD` env var (default: 7)
