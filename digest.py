"""
============================================================================
 DAILY TECH & SECURITY DIGEST  —  the whole program lives in this one file
============================================================================

BIG PICTURE (read this part out loud during the demo):

  This script does three things, in order:
    1. FETCH   — go out to the internet and grab the latest stories from a
                 bunch of tech + security websites.
    2. RENDER  — paste all those stories into one nice-looking email.
    3. SEND    — email it to you (or a whole team).

  It is meant to be run once a day by a robot (GitHub Actions). Nobody has to
  press a button. The robot wakes up on a schedule, runs this file, and the
  email lands in your inbox.

THE SOURCES WE PULL FROM:
  Tech news:
    - Hacker News      (top stories, official Firebase API)
    - Lobsters         (hottest stories, official JSON endpoint)
    - dev.to           (top articles, official API)
    - GitHub Trending  (today's hot repos — scraped from the web page,
                        because GitHub has no official "trending" API)
    - arXiv            (new computer-science research papers)
  Security / vulnerabilities:
    - CISA KEV         (bugs CONFIRMED to be actively attacked — highest signal)
    - NVD              (brand-new CVEs at/above a severity you choose)
    - GitHub Advisories(security bugs in the package ecosystems you use)
    - Security news    (The Hacker News / BleepingComputer / Krebs headlines)

WHY THE TWO CRON TIMES + A "SEND HOUR" CHECK? (explained again at should_send_now)
  GitHub's scheduler only understands UTC and ignores daylight saving. So we
  let it fire TWICE (23:00 and 00:00 UTC), and this script decides which of
  those two is actually 5pm in your timezone today. Result: exactly one email
  per day, all year, even when the clocks change.

SETTINGS (these come in as "environment variables" — basically named values
the workflow file hands to the script). The REQUIRED ones:
  SMTP_HOST       mail server, e.g. smtp.gmail.com
  SMTP_PORT       mail port,   e.g. 587
  SMTP_USER       the address the email is sent FROM
  SMTP_PASSWORD   an app password (NOT your real password — see the README)
  EMAIL_TO        who to send to. ONE address, or SEVERAL separated by commas
                  e.g. "me@x.com"  OR  "me@x.com, teammate@y.com, boss@z.com"

  Everything below is OPTIONAL — each has a sensible default, so you can ignore
  them until you want to tune things:
    TIMEZONE, TARGET_HOUR, HOURS_WINDOW,
    HN_COUNT, LOBSTERS_COUNT, DEVTO_COUNT, GITHUB_COUNT, GITHUB_LANGUAGE,
    ARXIV_COUNT, ARXIV_CATEGORIES,
    KEV_COUNT, KEV_DAYS, NVD_COUNT, NVD_SEVERITIES, NVD_API_KEY,
    GHSA_COUNT, GHSA_ECOSYSTEMS, GHSA_SEVERITIES, GHSA_DAYS,
    SECNEWS_COUNT, GITHUB_TOKEN, SOURCES
  (Full descriptions of each live in the README and the workflow file.)
"""

# These are all "batteries-included" Python tools — nothing to pip install.
# That's on purpose: fewer moving parts = easier to trust and easier to demo.
import os                                   # read the settings/environment variables
import re                                   # find patterns in text (used for scraping)
import smtplib                              # the actual "send an email" library
import sys                                  # for printing errors + exit codes
import time                                 # timestamps and polite pauses
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart   # build an email with multiple parts
from email.mime.text import MIMEText             # one part = plain text, one = HTML
from email.utils import parsedate_to_datetime    # parse the dates in RSS feeds
from html import unescape, escape           # clean up / safely encode web text
from urllib.parse import quote, urlsplit    # quote: encode text for a URL; urlsplit: inspect a URL's scheme
from urllib.request import Request, urlopen # download a web page or API response
from zoneinfo import ZoneInfo               # turn "America/Edmonton" into a real timezone
import json                                 # read JSON responses from APIs

# Some websites block anonymous robots. We introduce ourselves politely with a
# "User-Agent" so our requests look like a well-behaved tool, not a scraper.
USER_AGENT = "tech-digest-bot/1.0 (personal news digest)"


# ----------------------------------------------------------------------------
# TWO TINY HELPERS — every source uses one of these to talk to the internet.
# Writing them once means we don't repeat the same download code nine times.
# ----------------------------------------------------------------------------

def http_get(url, accept=None, timeout=20, extra_headers=None):
    """Download a URL and hand back the raw text (HTML, XML, whatever)."""
    headers = {"User-Agent": USER_AGENT}
    if accept:                              # tell the server what format we want back
        headers["Accept"] = accept
    if extra_headers:                       # e.g. an auth token for some APIs
        headers.update(extra_headers)
    req = Request(url, headers=headers)
    # `timeout` matters: if a site hangs, we give up instead of freezing forever.
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, timeout=20, extra_headers=None):
    """Same as above, but the response is JSON, so parse it into a Python dict/list."""
    return json.loads(
        http_get(url, accept="application/json", timeout=timeout, extra_headers=extra_headers)
    )


# ============================================================================
# THE FETCHERS
# One function per website. Each one returns a LIST of items, and every item is
# the SAME simple shape — a dictionary with four keys:
#     title           the headline
#     url             where the story lives
#     discussion_url  a comments/thread link (sometimes same as url)
#     meta            the little grey line under the title (points, author, etc.)
# Because they ALL return that identical shape, the email builder later can
# treat every source the same way. That uniformity is the whole trick.
# ============================================================================


# ---------- Hacker News ----------
HN_TOP = "https://hacker-news.firebaseio.com/v0/topstories.json"   # list of story IDs
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"     # details for one ID


def fetch_hn(count, hours_window):
    # Step 1: ask HN for the IDs of the current top stories (just numbers).
    ids = http_get_json(HN_TOP)
    # "cutoff" = the oldest timestamp we'll accept. Anything older than the
    # window (e.g. last 24h) gets skipped, so the digest stays fresh.
    cutoff = time.time() - (hours_window * 3600)
    items = []
    # Step 2: HN gives IDs but not the story details, so we fetch each one.
    # We look at up to 200 IDs because once we filter by "last 24h" many drop out.
    for story_id in ids[:200]:
        if len(items) >= count:             # stop early once we have enough
            break
        try:
            s = http_get_json(HN_ITEM.format(story_id))
        except Exception:
            continue                        # one bad story shouldn't kill the loop
        if not s or s.get("type") != "story":
            continue                        # skip job posts, polls, deleted items
        if s.get("time", 0) < cutoff:
            continue                        # too old — skip it
        items.append({
            "title": s.get("title", "(no title)"),
            # If a story has no link (an "Ask HN" post), fall back to its thread.
            "url": s.get("url") or f"https://news.ycombinator.com/item?id={s['id']}",
            "discussion_url": f"https://news.ycombinator.com/item?id={s['id']}",
            "meta": f"{s.get('score', 0)} points · {s.get('descendants', 0)} comments · by {s.get('by', '')}",
        })
    return items


# ---------- Lobsters ----------

def fetch_lobsters(count, hours_window):
    # Lobsters is friendlier: one request returns the hottest stories with all
    # their details already attached, so no second round of fetches needed.
    data = http_get_json("https://lobste.rs/hottest.json")
    cutoff = time.time() - (hours_window * 3600)
    items = []
    for s in data:
        if len(items) >= count:
            break
        # Their timestamps look like "2024-01-15T12:34:56.000-05:00".
        try:
            created = datetime.fromisoformat(s["created_at"]).timestamp()
        except Exception:
            created = 0
        if created < cutoff:
            continue                        # outside our time window — skip
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
    # dev.to's API: `top=1` means "the top articles from the last 1 day".
    # We ask for twice as many as we need (count * 2) because the time filter
    # below will throw some away.
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
# GitHub has NO official "trending" API. So instead of asking nicely for JSON,
# we download the actual trending web page and pick the data out of the HTML.
# This is called "scraping." It's a bit fragile — if GitHub redesigns the page,
# the patterns below may need updating — which is exactly why every fetcher is
# wrapped in error handling: a broken scrape just means an empty section.
GITHUB_TRENDING_URL = "https://github.com/trending"


def fetch_github_trending(count, language=None):
    url = GITHUB_TRENDING_URL
    if language:                            # optional: only show e.g. Python repos
        url += f"/{quote(language)}"
    url += "?since=daily"                   # "today's" trending, not weekly/monthly
    html = http_get(url, accept="text/html")

    items = []
    # On the page, each repo is wrapped in <article class="Box-row">...</article>.
    # We grab the chunk of HTML for each repo, then dig out the bits we want.
    articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL)
    for art in articles:
        if len(items) >= count:
            break
        # The repo name ("owner/name") is inside the heading's link.
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', art)
        if not m:
            continue
        path = m.group(1).strip()
        repo = path.lstrip("/")
        # The one-line description sits in a <p> with a "col-9" class.
        desc_m = re.search(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', art, re.DOTALL)
        desc = unescape(re.sub(r"\s+", " ", desc_m.group(1)).strip()) if desc_m else ""
        # "Stars today" lives in a right-floated span.
        stars_today_m = re.search(
            r'<span[^>]*class="d-inline-block float-sm-right"[^>]*>(.*?)</span>',
            art, re.DOTALL,
        )
        stars_today = re.sub(r"\s+", " ", unescape(stars_today_m.group(1))).strip() if stars_today_m else ""
        # The main programming language.
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
    # Build a query like "cat:cs.AI OR cat:cs.LG OR ...", newest first.
    cat_query = "+OR+".join(f"cat:{c.strip()}" for c in categories)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={cat_query}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={count * 3}"
    )
    # arXiv answers in Atom XML (an RSS-ish format), not JSON.
    xml = http_get(url, accept="application/atom+xml")

    items = []
    # Each paper is one <entry>...</entry> block. Pull them out and parse each.
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
        # arXiv releases papers in once-a-day batches, so a strict 24h window can
        # miss a whole day. We force AT LEAST a 48h window for arXiv specifically.
        arxiv_window = max(hours_window, 48) * 3600
        if published < (time.time() - arxiv_window):
            continue
        title = re.sub(r"\s+", " ", unescape(title_m.group(1))).strip()
        link = link_m.group(1).strip()
        # Show up to 3 author names, then "+N more" so the line stays short.
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


# ---------- CISA KEV (Known Exploited Vulnerabilities) ----------
# THE most important security source. This is the U.S. government's list of bugs
# that are CONFIRMED being used in real attacks right now. If something's here,
# you patch it. It's a plain JSON file: no key, no rate limit.
CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)


def fetch_cisa_kev(count, days):
    data = http_get_json(CISA_KEV_URL, timeout=30)
    # Here the window is in DAYS, not hours — KEV adds only a few entries a week,
    # so a 24h view would usually be empty.
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    vulns = data.get("vulnerabilities", [])
    # The file isn't guaranteed to be sorted, so we sort newest-added first.
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
            "discussion_url": detail,       # no separate thread → reuse the same link
            "meta": (
                f"{v.get('vendorProject', '')} {v.get('product', '')} · "
                f"added {v.get('dateAdded', '')} · patch due {v.get('dueDate', 'n/a')}"
            ),
        })
    return items


# ---------- NVD (National Vulnerability Database) ----------
# Every newly-published CVE at/above a severity you pick (CRITICAL by default).
# Free to use; an optional API key just raises how fast you can call it.
NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def fetch_nvd(count, severities, hours_window):
    # We ask NVD for CVEs published between "now minus the window" and "now".
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_window)
    fmt = "%Y-%m-%dT%H:%M:%S.000"
    api_key = os.environ.get("NVD_API_KEY")
    extra = {"apiKey": api_key} if api_key else None

    items = []
    # We may be asked for several severities (e.g. CRITICAL, HIGH) — one call each.
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
            # NVD sometimes rate-limits or times out. Don't crash — just skip
            # this severity and keep whatever else we managed to collect.
            continue
        for entry in data.get("vulnerabilities", []):
            if len(items) >= count:
                break
            cve = entry.get("cve", {})
            cve_id = cve.get("id", "")
            descs = cve.get("descriptions", [])
            desc = next((d.get("value", "") for d in descs if d.get("lang") == "en"), "")
            if len(desc) > 160:             # trim long descriptions to one tidy line
                desc = desc[:160].rstrip() + "…"
            detail = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
            items.append({
                "title": f"{cve_id} [{sev}]",
                "url": detail,
                "discussion_url": detail,
                "meta": desc,
            })
        # Be polite between calls. Without a key NVD wants a long gap (~6s);
        # with a key we can go fast (~0.6s). No wait needed after the last one.
        if idx < len(severities) - 1:
            time.sleep(0.6 if api_key else 6)
    return items


# ---------- GitHub Security Advisories ----------
# Security bugs in the package ecosystems YOU actually use (npm, NuGet, etc.).
# This keeps advisories relevant instead of drowning you in every language.
GHSA_API = "https://api.github.com/advisories"


def fetch_github_advisories(count, ecosystems, severities, days):
    # The token is optional. Without it you're rate-limited to ~60 calls/hour;
    # with it, thousands. Actions provides one for free (see the workflow file).
    token = os.environ.get("GITHUB_TOKEN")
    extra = {"Accept": "application/vnd.github+json"}
    if token:
        extra["Authorization"] = f"Bearer {token}"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    items = []
    # Loop over every ecosystem x severity combination the user asked for.
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
                continue                    # skip this combo if the call fails
            for adv in advs:
                if len(items) >= count:
                    break
                published = adv.get("published_at")
                if published:
                    try:
                        pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
                        if pub < cutoff:
                            continue         # older than our window — skip
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


# ---------- Security news (RSS headlines) ----------
# Plain headlines from a few well-known security blogs, via their RSS feeds.
SECURITY_FEEDS = [
    ("The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("Krebs on Security", "https://krebsonsecurity.com/feed/"),
]


def _strip_cdata(s):
    # RSS often wraps text like <![CDATA[ the actual text ]]>. Peel that off.
    s = s.strip()
    m = re.match(r"^<!\[CDATA\[(.*?)\]\]>$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def fetch_security_news(count, hours_window):
    cutoff = time.time() - (hours_window * 3600)
    # Spread the requested count across the feeds so no single blog dominates.
    per_feed = max(3, count // max(1, len(SECURITY_FEEDS)) + 1)
    items = []
    for source_name, feed_url in SECURITY_FEEDS:
        if len(items) >= count:
            break
        try:
            xml = http_get(feed_url, accept="application/rss+xml")
        except Exception:
            continue                        # one dead feed shouldn't stop the rest
        # Each article is an <item>...</item> block in the RSS.
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
            # Different feeds store the link differently, so we try three ways:
            link = _strip_cdata(link_m.group(1)).strip() if link_m else ""
            if not link:                    # 1) Atom-style <link href="..."/>
                alink = re.search(r'<link[^>]*href="([^"]+)"', entry)
                link = alink.group(1).strip() if alink else ""
            if not link:                    # 2) last resort: the <guid>
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
                continue                    # too old — skip
            title = unescape(_strip_cdata(re.sub(r"\s+", " ", title_m.group(1))).strip())
            items.append({
                "title": title,
                "url": link,
                "discussion_url": link,
                "meta": source_name,
            })
            added += 1
    return items


# ============================================================================
# RENDERING — turn the lists of items into an actual email.
# We build TWO versions of the same content:
#   - an HTML version (pretty, what most people see)
#   - a plain-text version (fallback for email apps that block HTML)
# Email clients automatically show whichever they prefer.
# ============================================================================

# These four section titles are the "security" ones. We give them a red accent
# so vulnerabilities visually stand apart from ordinary tech news.
SECURITY_SECTION_NAMES = {
    "Actively Exploited — CISA KEV",
    "New CVEs — NVD",
    "Dependency Advisories — npm + NuGet",
    "Security News",
}


def safe_url(raw):
    """
    Make a link safe to drop into the email's HTML.

    Two jobs, both about defense-in-depth (the text already gets escape()'d):
      1. ALLOWLIST the scheme. We only permit ordinary web links — http and
         https. Anything else (javascript:, data:, vbscript:, a blank value,
         or something malformed) is treated as untrusted and becomes "#", so
         the link is inert and can't do anything.
      2. HTML-escape what's left, so a stray quote in the URL can't "break out"
         of the href="..." attribute and inject extra markup.

    The sources we use are reputable APIs, and mail clients already block
    things like javascript: links — this just makes that guarantee our own
    instead of relying on every reader's email app to do the right thing.
    """
    if not raw:
        return "#"
    raw = raw.strip()
    try:
        scheme = urlsplit(raw).scheme.lower()
    except Exception:
        return "#"                          # unparseable → treat as unsafe
    if scheme not in ("http", "https"):
        return "#"                          # block javascript:, data:, etc.
    return escape(raw, quote=True)          # escape &, <, >, and quotes


def render_section_html(name, items):
    """Build the HTML for ONE section (e.g. all the Hacker News rows)."""
    if not items:                           # nothing to show → render nothing
        return ""
    # Red for security sections, GitHub-orange for everything else.
    accent = "#c8102e" if name in SECURITY_SECTION_NAMES else "#ff6600"
    rows = []
    for i, it in enumerate(items, start=1):
        # Only show a separate "discuss" link when the thread differs from the story.
        discuss_link = ""
        if it.get("discussion_url") and it["discussion_url"] != it["url"]:
            disc = safe_url(it["discussion_url"])
            discuss_link = f' · <a href="{disc}" style="color:#888;">discuss</a>'
        # escape() makes sure a weird character in a title can't break the HTML.
        title = escape(it["title"])
        meta = escape(it["meta"])
        story_url = safe_url(it["url"])
        rows.append(f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top;width:24px;">
            <div style="font-size:13px;color:#888;">{i}.</div>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{story_url}" style="color:#000;text-decoration:none;font-weight:600;font-size:15px;">{title}</a>
            <div style="color:#888;font-size:12px;margin-top:4px;">{meta}{discuss_link}</div>
          </td>
        </tr>
        """)
    joined = "".join(rows)
    return f"""
      <h3 style="margin-top:28px;margin-bottom:4px;font-size:14px;text-transform:uppercase;letter-spacing:0.5px;color:{accent};">{escape(name)}</h3>
      <table style="width:100%;border-collapse:collapse;">{joined}</table>
    """


def render_html(sections, local_now_str):
    """Stitch every section together into one full HTML email."""
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
    """The plain-text fallback — same content, no styling."""
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


# ============================================================================
# SENDING THE EMAIL
# ============================================================================

def send_email(subject, html, text):
    # Pull the mail settings out of the environment. These came from the
    # repository "secrets" — they are never written into the code.
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASSWORD"]

    # EMAIL_TO can be ONE address or MANY separated by commas. We split on
    # commas and tidy up whitespace, so "a@x.com, b@y.com" becomes a real list.
    # A single address simply produces a list with one entry — same code path,
    # so the script handles "single recipient" and "whole team" identically.
    recipients = [addr.strip() for addr in os.environ["EMAIL_TO"].split(",") if addr.strip()]

    # An "alternative" email carries both the plain-text and HTML versions;
    # the recipient's mail app picks whichever it likes.
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    # The visible "To:" header is the addresses joined back together with commas.
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))     # add text first...
    msg.attach(MIMEText(html, "html"))      # ...then HTML (last = preferred)

    # Connect, upgrade to an encrypted connection (starttls), log in, send.
    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pw)
        # Pass the FULL list of recipients so everyone actually receives it.
        server.sendmail(user, recipients, msg.as_string())


# ============================================================================
# ORCHESTRATION — the conductor that runs everything in order.
# ============================================================================

# The email shows sections in THIS order. Security sources lead so the most
# urgent items (bugs under active attack) are seen first. Want the old
# tech-first look? Just move "hn" to the front of this list.
ALL_SOURCES = ["kev", "nvd", "ghsa", "secnews", "hn", "lobsters", "devto", "github", "arxiv"]


def should_send_now():
    """
    Decide whether THIS run should actually send an email, and return
    (yes_or_no, a_pretty_local_time_string).

    Why this exists: the robot's scheduler only speaks UTC and ignores daylight
    saving, so we let it fire twice a day (see the workflow file). This function
    is the gatekeeper that makes sure only ONE of those firings sends.

    The rule:
      - If a human pressed "Run workflow" (a manual run) -> always send. Great
        for demos and testing.
      - If it's a scheduled run -> only send when the local clock reads
        TARGET_HOUR (5pm by default) in your timezone. The other firing is
        the wrong hour locally, so it quietly does nothing.
    """
    tz_name = os.environ.get("TIMEZONE", "America/Edmonton")
    target_hour = int(os.environ.get("TARGET_HOUR", "17"))   # 17 = 5pm
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(tz)
    local_str = local_now.strftime("%A %b %d, %Y · %-I:%M %p %Z")

    # GitHub Actions tells us HOW the run started via this variable:
    #   "workflow_dispatch" = a human clicked Run,  "schedule" = the cron fired.
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        print(f"Manual trigger — sending regardless of hour. Local: {local_str}")
        return True, local_str

    # Scheduled run: only send if it's the target hour locally right now.
    if local_now.hour == target_hour:
        print(f"Local hour {local_now.hour} matches target {target_hour} — sending. ({local_str})")
        return True, local_str
    print(f"Local hour {local_now.hour} != target {target_hour} — skipping. ({local_str})")
    return False, local_str


def main():
    # 1) Should we even be running right now? If not, stop immediately.
    should_send, local_str = should_send_now()
    if not should_send:
        return

    # 2) Read the time window and figure out which sources are turned on.
    hours = int(os.environ.get("HOURS_WINDOW", "24"))
    enabled = [
        s.strip().lower()
        for s in os.environ.get("SOURCES", ",".join(ALL_SOURCES)).split(",")
        if s.strip()
    ]

    # 3) A lookup table: source key -> (section title, how to fetch it).
    #    Each fetch is wrapped in a `lambda` so it only actually RUNS later,
    #    inside the try/except loop below — that way one failing source can't
    #    stop the others. The os.environ.get(...) calls read per-source settings
    #    (how many items, which categories, etc.) with safe defaults.
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

    # 4) Run each enabled fetcher, in the ALL_SOURCES order, collecting results.
    sections = []
    total = 0
    for key in ALL_SOURCES:
        if key not in enabled:              # user turned this source off — skip
            continue
        name, fn = fetchers[key]
        try:
            items = fn()                    # <-- the actual fetch happens here
            sections.append((name, items))
            total += len(items)
            print(f"{name}: {len(items)} items")
        except Exception as e:
            # KEY DESIGN POINT: if one source breaks, we log it and keep going
            # with an empty section, so the rest of the digest still goes out.
            print(f"{name}: FAILED ({e})", file=sys.stderr)
            sections.append((name, []))

    # 5) If literally nothing came back, don't send an empty email.
    if total == 0:
        print("No items from any source — skipping email.")
        return

    # 6) Build the email and send it.
    subject = f"Daily Tech & Security Digest — {total} items"
    send_email(subject, render_html(sections, local_str), render_text(sections))
    print(f"Sent digest with {total} items.")


# This is the program's front door. We wrap main() so that ANY unexpected error
# prints clearly and exits with code 1 — which makes the GitHub Actions run show
# up as a red X, so you actually notice when something went wrong.
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
