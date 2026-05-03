"""
Multi-source tech news digest.

Sources:
  - Hacker News (top stories, official Firebase API)
  - Lobsters (hottest, official JSON endpoint)
  - dev.to (top articles, official API)
  - GitHub Trending (HTML scrape — no official API)
  - arXiv (cs.* categories, Atom feed via the export API)

Designed to be run on a schedule via GitHub Actions. Two cron entries fire at
23:00 and 00:00 UTC; the TIMEZONE/TARGET_HOUR gate makes sure exactly one email
is sent per day at the right local hour, regardless of DST.

Required environment variables:
  SMTP_HOST       e.g. smtp.gmail.com
  SMTP_PORT       e.g. 587
  SMTP_USER       sender email address
  SMTP_PASSWORD   app password (NOT your real password — see README)
  EMAIL_TO        where to send the digest

Optional (all have sensible defaults):
  TIMEZONE           IANA tz name (default America/Edmonton — Calgary)
  TARGET_HOUR        local hour 0-23 (default 17 = 5pm). Skipped on manual runs.
  HOURS_WINDOW       only include items from the last N hours (default 24)
  HN_COUNT           how many HN stories (default 15)
  LOBSTERS_COUNT     how many Lobsters stories (default 12)
  DEVTO_COUNT        how many dev.to articles (default 10)
  GITHUB_COUNT       how many GitHub trending repos (default 10)
  GITHUB_LANGUAGE    filter trending by language, e.g. "python" (default: all)
  ARXIV_COUNT        how many arXiv papers (default 10)
  ARXIV_CATEGORIES   comma-separated, e.g. "cs.AI,cs.LG,cs.CL" (default: cs.AI,cs.LG,cs.CL,cs.SE)
  SOURCES            comma-separated subset to enable, e.g. "hn,lobsters" (default: all)
"""

import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import json

USER_AGENT = "tech-digest-bot/1.0 (personal news digest)"


def http_get(url, accept=None, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, timeout=20):
    return json.loads(http_get(url, accept="application/json", timeout=timeout))


# ---------- Hacker News ----------

HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"


def fetch_hn(count, hours_window):
    ids = http_get_json(HN_TOP)
    cutoff = time.time() - (hours_window * 3600)
    items = []
    # Look at more IDs since we have a wider 24h window now.
    for story_id in ids[:200]:
        if len(items) >= count:
            break
        try:
            s = http_get_json(HN_ITEM.format(story_id))
        except Exception:
            continue
        if not s or s.get("type") != "story":
            continue
        if s.get("time", 0) < cutoff:
            continue
        items.append({
            "title": s.get("title", "(no title)"),
            "url": s.get("url") or f"https://news.ycombinator.com/item?id={s['id']}",
            "discussion_url": f"https://news.ycombinator.com/item?id={s['id']}",
            "meta": f"{s.get('score', 0)} points · {s.get('descendants', 0)} comments · by {s.get('by', '')}",
        })
    return items


# ---------- Lobsters ----------

def fetch_lobsters(count, hours_window):
    data = http_get_json("https://lobste.rs/hottest.json")
    cutoff = time.time() - (hours_window * 3600)
    items = []
    for s in data:
        if len(items) >= count:
            break
        # Lobsters timestamps look like "2024-01-15T12:34:56.000-05:00"
        try:
            created = datetime.fromisoformat(s["created_at"]).timestamp()
        except Exception:
            created = 0
        if created < cutoff:
            continue
        tags = ", ".join(s.get("tags", []))
        items.append({
            "title": s.get("title", "(no title)"),
            "url": s.get("url") or s.get("comments_url"),
            "discussion_url": s.get("comments_url"),
            "meta": f"{s.get('score', 0)} points · {s.get('comment_count', 0)} comments · {tags}",
        })
    return items


# ---------- dev.to ----------

def fetch_devto(count, hours_window):
    # dev.to API: top articles. `top=1` means "top from the last 1 day".
    data = http_get_json(f"https://dev.to/api/articles?top=1&per_page={count * 2}")
    cutoff = time.time() - (hours_window * 3600)
    items = []
    for s in data:
        if len(items) >= count:
            break
        try:
            published = datetime.fromisoformat(
                s["published_at"].replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            published = 0
        if published < cutoff:
            continue
        author = s.get("user", {}).get("name", "")
        items.append({
            "title": s.get("title", "(no title)"),
            "url": s.get("url"),
            "discussion_url": s.get("url"),
            "meta": f"{s.get('public_reactions_count', 0)} reactions · {s.get('comments_count', 0)} comments · by {author}",
        })
    return items


# ---------- GitHub Trending ----------
# GitHub doesn't have an official trending API, so we scrape the HTML.

GITHUB_TRENDING_URL = "https://github.com/trending"


def fetch_github_trending(count, language=None):
    url = GITHUB_TRENDING_URL
    if language:
        url += f"/{quote(language)}"
    url += "?since=daily"
    html = http_get(url, accept="text/html")

    items = []
    # Each trending repo lives in <article class="Box-row">. Pull them out.
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL)
    for art in articles:
        if len(items) >= count:
            break
        # Repo path is in the h2 a href, like "/owner/name"
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', art)
        if not m:
            continue
        path = m.group(1).strip()
        repo = path.lstrip("/")
        # Description in a <p>
        desc_m = re.search(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', art, re.DOTALL)
        desc = unescape(re.sub(r"\s+", " ", desc_m.group(1)).strip()) if desc_m else ""
        # Stars today — last <span class="d-inline-block float-sm-right">
        stars_today_m = re.search(
            r'<span[^>]*class="d-inline-block float-sm-right"[^>]*>(.*?)</span>',
            art, re.DOTALL,
        )
        stars_today = re.sub(r"\s+", " ", unescape(stars_today_m.group(1))).strip() if stars_today_m else ""
        # Language
        lang_m = re.search(
            r'<span itemprop="programmingLanguage">([^<]+)</span>', art
        )
        lang = lang_m.group(1).strip() if lang_m else ""
        meta_parts = [p for p in [lang, stars_today] if p]
        items.append({
            "title": repo,
            "url": f"https://github.com{path}",
            "discussion_url": f"https://github.com{path}",
            "meta": " · ".join(meta_parts) + (f" — {desc}" if desc else ""),
        })
    return items


# ---------- arXiv ----------

def fetch_arxiv(count, categories, hours_window):
    cat_query = "+OR+".join(f"cat:{c.strip()}" for c in categories)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={cat_query}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={count * 3}"
    )
    xml = http_get(url, accept="application/atom+xml")

    items = []
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)
    for entry in entries:
        if len(items) >= count:
            break
        title_m = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        link_m = re.search(r'<id>(.*?)</id>', entry)
        published_m = re.search(r"<published>(.*?)</published>", entry)
        authors = re.findall(r"<name>(.*?)</name>", entry)
        cat_m = re.search(r'<arxiv:primary_category[^/]+term="([^"]+)"', entry)
        if not (title_m and link_m):
            continue
        try:
            published = datetime.fromisoformat(
                published_m.group(1).replace("Z", "+00:00")
            ).timestamp() if published_m else 0
        except Exception:
            published = 0
        # arXiv announcements are batched, so use at least 48h regardless of HOURS_WINDOW.
        arxiv_window = max(hours_window, 48) * 3600
        if published < (time.time() - arxiv_window):
            continue
        title = re.sub(r"\s+", " ", unescape(title_m.group(1))).strip()
        link = link_m.group(1).strip()
        author_str = ", ".join(authors[:3])
        if len(authors) > 3:
            author_str += f", +{len(authors) - 3} more"
        cat = cat_m.group(1) if cat_m else ""
        items.append({
            "title": title,
            "url": link,
            "discussion_url": link,
            "meta": f"{cat} · {author_str}",
        })
    return items


# ---------- Rendering ----------

def render_section_html(name, items):
    if not items:
        return ""
    rows = []
    for i, it in enumerate(items, start=1):
        discuss_link = ""
        if it.get("discussion_url") and it["discussion_url"] != it["url"]:
            discuss_link = f' · <a href="{it["discussion_url"]}" style="color:#888;">discuss</a>'
        rows.append(f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top;width:24px;">
            <div style="font-size:13px;color:#888;">{i}.</div>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{it['url']}" style="color:#000;text-decoration:none;font-weight:600;font-size:15px;">{it['title']}</a>
            <div style="color:#888;font-size:12px;margin-top:4px;">{it['meta']}{discuss_link}</div>
          </td>
        </tr>
        """)
    return f"""
      <h3 style="margin-top:28px;margin-bottom:4px;font-size:14px;text-transform:uppercase;letter-spacing:0.5px;color:#ff6600;">{name}</h3>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
    """


def render_html(sections, local_now_str):
    body = "".join(render_section_html(name, items) for name, items in sections)
    return f"""
    <html><body style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;padding:20px;">
      <h2 style="border-bottom:2px solid #ff6600;padding-bottom:8px;margin-bottom:0;">Daily Tech Digest</h2>
      <div style="color:#888;font-size:12px;margin-top:4px;">{local_now_str}</div>
      {body}
      <p style="color:#aaa;font-size:11px;margin-top:32px;">Auto-generated digest.</p>
    </body></html>
    """


def render_text(sections):
    out = ["Daily Tech Digest", "=" * 40, ""]
    for name, items in sections:
        if not items:
            continue
        out.append(name.upper())
        out.append("-" * len(name))
        for i, it in enumerate(items, start=1):
            out.append(f"{i}. {it['title']}")
            out.append(f"   {it['url']}")
            out.append(f"   {it['meta']}")
            out.append("")
        out.append("")
    return "\n".join(out)


# ---------- Email ----------

def send_email(subject, html, text):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASSWORD"]
    to = os.environ["EMAIL_TO"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pw)
        server.sendmail(user, [to], msg.as_string())


# ---------- Orchestration ----------

ALL_SOURCES = ["hn", "lobsters", "devto", "github", "arxiv"]


def should_send_now():
    """
    Return (should_send, local_time_str). Manual runs (workflow_dispatch) always
    send. Scheduled runs only send if the local hour matches TARGET_HOUR.
    """
    tz_name = os.environ.get("TIMEZONE", "America/Edmonton")
    target_hour = int(os.environ.get("TARGET_HOUR", "17"))
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    local_str = local_now.strftime("%A %b %d, %Y · %-I:%M %p %Z")

    # GitHub Actions sets this env var; "schedule" = cron, "workflow_dispatch" = manual.
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        print(f"Manual trigger — sending regardless of hour. Local: {local_str}")
        return True, local_str

    if local_now.hour == target_hour:
        print(f"Local hour {local_now.hour} matches target {target_hour} — sending. ({local_str})")
        return True, local_str
    print(f"Local hour {local_now.hour} != target {target_hour} — skipping. ({local_str})")
    return False, local_str


def main():
    should_send, local_str = should_send_now()
    if not should_send:
        return

    hours = int(os.environ.get("HOURS_WINDOW", "24"))
    enabled = [
        s.strip().lower()
        for s in os.environ.get("SOURCES", ",".join(ALL_SOURCES)).split(",")
        if s.strip()
    ]

    fetchers = {
        "hn": ("Hacker News", lambda: fetch_hn(int(os.environ.get("HN_COUNT", "15")), hours)),
        "lobsters": ("Lobsters", lambda: fetch_lobsters(int(os.environ.get("LOBSTERS_COUNT", "12")), hours)),
        "devto": ("dev.to", lambda: fetch_devto(int(os.environ.get("DEVTO_COUNT", "10")), hours)),
        "github": ("GitHub Trending", lambda: fetch_github_trending(
            int(os.environ.get("GITHUB_COUNT", "10")),
            os.environ.get("GITHUB_LANGUAGE") or None,
        )),
        "arxiv": ("arXiv", lambda: fetch_arxiv(
            int(os.environ.get("ARXIV_COUNT", "10")),
            os.environ.get("ARXIV_CATEGORIES", "cs.AI,cs.LG,cs.CL,cs.SE").split(","),
            hours,
        )),
    }

    sections = []
    total = 0
    for key in ALL_SOURCES:
        if key not in enabled:
            continue
        name, fn = fetchers[key]
        try:
            items = fn()
            sections.append((name, items))
            total += len(items)
            print(f"{name}: {len(items)} items")
        except Exception as e:
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            sections.append((name, []))

    if total == 0:
        print("No items from any source — skipping email.")
        return

    subject = f"Daily Tech Digest — {total} items"
    send_email(subject, render_html(sections, local_str), render_text(sections))
    print(f"Sent digest with {total} items.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
