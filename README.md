# Tech Digest

Emails you a once-daily digest of tech news from five sources, delivered at **5pm Calgary time** every day. Runs free on GitHub Actions.

**Sources:**
- **Hacker News** — top stories
- **Lobsters** — hottest stories
- **dev.to** — top articles of the day
- **GitHub Trending** — daily trending repos
- **arXiv** — newest cs.AI, cs.LG, cs.CL, cs.SE papers (configurable)

## Setup (5 minutes)

### 1. Create a new GitHub repo

Create a private repo (e.g. `tech-digest`) and push these files to it:

```
.
├── digest.py
└── .github/
    └── workflows/
        └── digest.yml
```

### 2. Get an email "app password"

You can't use your regular email password — you need an app-specific one.

**Gmail:**
1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
2. Create an app password: <https://myaccount.google.com/apppasswords>
3. Save the 16-character password it gives you.

**Other providers:** most have an equivalent ("app password," "application-specific password"). Outlook, Fastmail, ProtonMail Bridge, etc. all support this.

### 3. Add secrets to your repo

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Add these five:

| Name            | Value (Gmail example)              |
| --------------- | ---------------------------------- |
| `SMTP_HOST`     | `smtp.gmail.com`                   |
| `SMTP_PORT`     | `587`                              |
| `SMTP_USER`     | `you@gmail.com`                    |
| `SMTP_PASSWORD` | the 16-char app password           |
| `EMAIL_TO`      | where to send digests (can be you) |

### 4. Test it

Go to the **Actions** tab → **Tech Digest** → **Run workflow**. Manual runs always send (the 5pm gate is bypassed), so you should get an email within ~30 seconds.

If it fails, click into the run to see the error log. The workflow logs how many items each source returned.

### 5. Done

It now runs automatically once a day at 5pm Calgary time. To stop it: disable the workflow from the Actions tab, or delete the repo.

## How the 5pm timing works

GitHub Actions cron only runs in UTC and doesn't know about DST. To deliver at exactly 5pm Calgary year-round, the workflow:

1. Runs cron at **both** 23:00 UTC (which is 5pm during MDT/summer) and 00:00 UTC (which is 5pm during MST/winter).
2. The script then checks the actual local hour in `America/Edmonton` and only sends the email if it's the target hour. The other run silently exits.

Result: exactly one email per day, 5pm local, in any season. No code changes needed at the DST switchover in March/November.

To change the time, edit these in `.github/workflows/digest.yml`:
- `TARGET_HOUR` — the local hour you want (24h format, e.g. `9` for 9am, `20` for 8pm).
- The two `cron` lines — these need to bracket your target time across DST. For 5pm Calgary that's 23:00 and 00:00 UTC. For other times: pick the UTC hour that equals your target during DST and the UTC hour that equals it during standard time.

To use a different timezone, change `TIMEZONE` (any IANA name like `America/Toronto`, `Europe/London`, `Asia/Tokyo`) and update the cron lines accordingly.

## Tweaking

Edit `.github/workflows/digest.yml`:

- **Per-source counts:** `HN_COUNT`, `LOBSTERS_COUNT`, `DEVTO_COUNT`, `GITHUB_COUNT`, `ARXIV_COUNT`.
- **Freshness window:** `HOURS_WINDOW` filters out items older than N hours. Daily digest → 24h is the right default. (arXiv is exempt: it always uses at least 48h because papers are released in batches and a short window often catches nothing.)
- **arXiv categories:** `ARXIV_CATEGORIES` is comma-separated, e.g. `cs.AI,cs.LG,cs.CL,cs.SE,cs.DC`. Full list at <https://arxiv.org/category_taxonomy>.
- **GitHub language filter:** uncomment `GITHUB_LANGUAGE: "python"` (or `rust`, `typescript`, etc.) to limit trending to one language.
- **Disable sources:** uncomment `SOURCES` and list only what you want, e.g. `"hn,github"`.

## How it handles failures

Each source is wrapped in try/except. If Lobsters is down or arXiv times out, the digest still sends with the others — you'll just see the failed source listed in the logs. The email is only skipped if *every* source returns nothing.

## Notes on each source

- **HN, Lobsters, dev.to, arXiv** all use official, stable, unauthenticated APIs.
- **GitHub Trending** has no official API, so the script scrapes the HTML. If GitHub redesigns the trending page, scraping may break and you'd need to update the regexes in `fetch_github_trending`. This is the most fragile part.

## Cost

GitHub Actions gives free accounts 2,000 minutes/month for private repos (unlimited for public). This job runs in well under a minute. Two scheduled runs per day (one of which exits immediately) plus a daily real run uses maybe 2 minutes/day → 60 minutes/month. Plenty of headroom.
