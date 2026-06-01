"""
Multi-source tech + security digest.

Sources:
  - Hacker News (top stories, official Firebase API)
  - Lobsters (hottest, official JSON endpoint)
  - dev.to (top articles, official API)
  - GitHub Trending (HTML scrape — no official API)
  - arXiv (cs.* categories, Atom feed via the export API)
  - CISA KEV (vulnerabilities CONFIRMED exploited in the wild, official JSON feed)
  - NVD (newly published CVEs at/above a severity threshold, NVD 2.0 API)
  - GitHub Security Advisories (scoped to your ecosystems, e.g. npm + NuGet)
  - Security news (The Hacker News / BleepingComputer / Krebs RSS)

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

  --- security sources ---
  KEV_COUNT          how many CISA KEV entries (default 15)
  KEV_DAYS           KEV entries added in the last N days (default 7 — KEV is sparse)
  NVD_COUNT          how many NVD CVEs (default 15)
  NVD_SEVERITIES     comma-separated, CRITICAL/HIGH/MEDIUM/LOW (default: CRITICAL)
  NVD_API_KEY        optional NVD key for a higher rate limit (not required)
  GHSA_COUNT         how many GitHub advisories (default 12)
  GHSA_ECOSYSTEMS    comma-separated, e.g. "npm,nuget,pip" (default: npm,nuget)
  GHSA_SEVERITIES    comma-separated, critical/high/moderate/low (default: critical,high)
  GHSA_DAYS          advisories published in the last N days (default 7)
  GITHUB_TOKEN       optional; raises the GHSA rate limit (auto-provided in Actions)
  SECNEWS_COUNT      how many security-news headlines (default 10)

  SOURCES            comma-separated subset to enable, e.g. "kev,nvd,hn"
                     (default: all). Section order in the email follows ALL_SOURCES.
"""

import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from html import unescape, escape
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import json

USER_AGENT = "tech-digest-bot/1.0 (personal news digest)"


def http_get(url, accept=None, timeout=20, extra_headers=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, timeout=20, extra_headers=None):
    return json.loads(
        http_get(url, accept="application/json", timeout=timeout, extra_headers=extra_headers)
    )


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


# ---------- CISA KEV ----------
# Vulnerabilities CONFIRMED exploited in the wild. Highest-signal source.
# Plain JSON, no key, no meaningful rate limit.

CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)


def fetch_cisa_kev(count, days):
    data = http_get_json(CISA_KEV_URL, timeout=30)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    vulns = data.get("vulnerabilities", [])
    # The feed isn't guaranteed sorted; sort newest-added first.
    vulns.sort(key=lambda v: v.get("dateAdded", ""), reverse=True)
    items = []
    for v in vulns:
        if len(items) >= count:
            break
        try:
            added = datetime.strptime(v["dateAdded"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if added < cutoff:
            continue
        cve = v.get("cveID", "")
        detail = f"https://nvd.nist.gov/vuln/detail/{cve}"
        items.append({
            "title": f"{cve}: {v.get('vulnerabilityName', '')}",
            "url": detail,
            "discussion_url": detail,  # no separate discussion → no "discuss" link
            "meta": (
                f"{v.get('vendorProject', '')} {v.get('product', '')} · "
                f"added {v.get('dateAdded', '')} · patch due {v.get('dueDate', 'n/a')}"
            ),
        })
    return items


# ---------- NVD ----------
# All newly published CVEs at/above a severity threshold (NVD 2.0 API).
# Free; an optional NVD_API_KEY raises the rate limit.

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def fetch_nvd(count, severities, hours_window):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_window)
    fmt = "%Y-%m-%dT%H:%M:%S.000"
    api_key = os.environ.get("NVD_API_KEY")
    extra = {"apiKey": api_key} if api_key else None

    items = []
    for idx, sev in enumerate(severities):
        if len(items) >= count:
            break
        url = (
            NVD_API
            + f"?pubStartDate={quote(start.strftime(fmt))}"
            + f"&pubEndDate={quote(end.strftime(fmt))}"
            + f"&cvssV3Severity={sev}"
            + "&resultsPerPage=200"
        )
        try:
            data = http_get_json(url, timeout=30, extra_headers=extra)
        except Exception:
            # NVD occasionally rate-limits or times out; skip this severity.
            continue
        for entry in data.get("vulnerabilities", []):
            if len(items) >= count:
                break
            cve = entry.get("cve", {})
            cve_id = cve.get("id", "")
            descs = cve.get("descriptions", [])
            desc = next((d.get("value", "") for d in descs if d.get("lang") == "en"), "")
            if len(desc) > 160:
                desc = desc[:160].rstrip() + "…"
            detail = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
            items.append({
                "title": f"{cve_id} [{sev}]",
                "url": detail,
                "discussion_url": detail,
                "meta": desc,
            })
        # Be polite to NVD between severity requests (one request needs no wait).
        if idx < len(severities) - 1:
            time.sleep(0.6 if api_key else 6)
    return items


# ---------- GitHub Security Advisories ----------
# Scoped to the package ecosystems you actually depend on.

GHSA_API = "https://api.github.com/advisories"


def fetch_github_advisories(count, ecosystems, severities, days):
    token = os.environ.get("GITHUB_TOKEN")
    extra = {"Accept": "application/vnd.github+json"}
    if token:
        extra["Authorization"] = f"Bearer {token}"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    items = []
    for eco in ecosystems:
        for sev in severities:
            if len(items) >= count:
                break
            url = (
                f"{GHSA_API}?ecosystem={quote(eco)}&severity={quote(sev)}"
                "&sort=published&direction=desc&per_page=50"
            )
            try:
                advs = http_get_json(url, timeout=30, extra_headers=extra)
            except Exception:
                continue
            for adv in advs:
                if len(items) >= count:
                    break
                published = adv.get("published_at")
                if published:
                    try:
                        pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
                        if pub < cutoff:
                            continue
                    except Exception:
                        pass
                ghsa = adv.get("ghsa_id", "")
                summary = adv.get("summary", "") or ghsa
                cve = adv.get("cve_id") or ""
                items.append({
                    "title": f"[{eco}] {summary}",
                    "url": adv.get("html_url", ""),
                    "discussion_url": adv.get("html_url", ""),
                    "meta": " · ".join(p for p in [ghsa, cve, sev] if p),
                })
    return items


# ---------- Security news (RSS) ----------

SECURITY_FEEDS = [
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
]


def _strip_cdata(s):
    s = s.strip()
    m = re.match(r"^<!\[CDATA\[(.*?)\]\]>$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def fetch_security_news(count, hours_window):
    cutoff = time.time() - (hours_window * 3600)
    per_feed = max(3, count // max(1, len(SECURITY_FEEDS)) + 1)
    items = []
    for source_name, feed_url in SECURITY_FEEDS:
        if len(items) >= count:
            break
        try:
            xml = http_get(feed_url, accept="application/rss+xml")
        except Exception:
            continue
        entries = re.findall(r"<item(?:\s[^>]*)?>(.*?)</item>", xml, re.DOTALL)
        added = 0
        for entry in entries:
            if added >= per_feed or len(items) >= count:
                break
            title_m = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
            link_m = re.search(r"<link>(.*?)</link>", entry, re.DOTALL)
            date_m = re.search(r"<pubDate>(.*?)</pubDate>", entry, re.DOTALL)
            if not title_m:
                continue
            link = _strip_cdata(link_m.group(1)).strip() if link_m else ""
            if not link:  # Atom-style <link href="..."/> fallback
                alink = re.search(r'<link[^>]*href="([^"]+)"', entry)
                link = alink.group(1).strip() if alink else ""
            if not link:  # last resort: <guid>
                guid_m = re.search(r"<guid[^>]*>(.*?)</guid>", entry, re.DOTALL)
                link = _strip_cdata(guid_m.group(1)).strip() if guid_m else ""
            if not link:
                continue
            published = 0
            if date_m:
                try:
                    published = parsedate_to_datetime(date_m.group(1).strip()).timestamp()
                except Exception:
                    published = 0
            if published and published < cutoff:
                continue
            title = unescape(_strip_cdata(re.sub(r"\s+", " ", title_m.group(1))).strip())
            items.append({
                "title": title,
                "url": link,
                "discussion_url": link,
                "meta": source_name,
            })
            added += 1
    return items


# ---------- Rendering ----------

# Security sections get a red accent so vulnerabilities stand out from tech news.
SECURITY_SECTION_NAMES = {
    "Actively Exploited — CISA KEV",
    "New CVEs — NVD",
    "Dependency Advisories — npm + NuGet",
    "Security News",
}


def render_section_html(name, items):
    if not items:
        return ""
    accent = "#c8102e" if name in SECURITY_SECTION_NAMES else "#ff6600"
    rows = []
    for i, it in enumerate(items, start=1):
        discuss_link = ""
        if it.get("discussion_url") and it["discussion_url"] != it["url"]:
            discuss_link = f' · <a href="{it["discussion_url"]}" style="color:#888;">discuss</a>'
        title = escape(it["title"])
        meta = escape(it["meta"])
        rows.append(f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top;width:24px;">
            <div style="font-size:13px;color:#888;">{i}.</div>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{it['url']}" style="color:#000;text-decoration:none;font-weight:600;font-size:15px;">{title}</a>
            <div style="color:#888;font-size:12px;margin-top:4px;">{meta}{discuss_link}</div>
          </td>
        </tr>
        """)
    return f"""
      <h3 style="margin-top:28px;margin-bottom:4px;font-size:14px;text-transform:uppercase;letter-spacing:0.5px;color:{accent};">{escape(name)}</h3>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
    """


def render_html(sections, local_now_str):
    body = "".join(render_section_html(name, items) for name, items in sections)
    return f"""
    <html><body style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;padding:20px;">
      <h2 style="border-bottom:2px solid #ff6600;padding-bottom:8px;margin-bottom:0;">Daily Tech &amp; Security Digest</h2>
      <div style="color:#888;font-size:12px;margin-top:4px;">{local_now_str}</div>
      {body}
      <p style="color:#aaa;font-size:11px;margin-top:32px;">Auto-generated digest.</p>
    </body></html>
    """


def render_text(sections):
    out = ["Daily Tech & Security Digest", "=" * 40, ""]
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

# Section order in the email follows this list. Security sources lead so the
# most actionable items (actively-exploited CVEs) are seen first — reorder
# freely, e.g. move "hn" to the front to keep the old tech-first layout.
ALL_SOURCES = ["kev", "nvd", "ghsa", "secnews", "hn", "lobsters", "devto", "github", "arxiv"]


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
        "kev": ("Actively Exploited — CISA KEV", lambda: fetch_cisa_kev(
            int(os.environ.get("KEV_COUNT", "15")),
            int(os.environ.get("KEV_DAYS", "7")),
        )),
        "nvd": ("New CVEs — NVD", lambda: fetch_nvd(
            int(os.environ.get("NVD_COUNT", "15")),
            [s.strip().upper() for s in os.environ.get("NVD_SEVERITIES", "CRITICAL").split(",") if s.strip()],
            hours,
        )),
        "ghsa": ("Dependency Advisories — npm + NuGet", lambda: fetch_github_advisories(
            int(os.environ.get("GHSA_COUNT", "12")),
            [s.strip() for s in os.environ.get("GHSA_ECOSYSTEMS", "npm,nuget").split(",") if s.strip()],
            [s.strip().lower() for s in os.environ.get("GHSA_SEVERITIES", "critical,high").split(",") if s.strip()],
            int(os.environ.get("GHSA_DAYS", "7")),
        )),
        "secnews": ("Security News", lambda: fetch_security_news(
            int(os.environ.get("SECNEWS_COUNT", "10")),
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

    subject = f"Daily Tech & Security Digest — {total} items"
    send_email(subject, render_html(sections, local_str), render_text(sections))
    print(f"Sent digest with {total} items.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
