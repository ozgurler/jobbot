# jobbot — daily job crawl and application queue

Private automation for Emre's job search.

**How it runs**

1. GitHub Actions runs `crawl.py` daily at 05:30 PT (`.github/workflows/crawl.yml`). It pulls new postings from Greenhouse, Lever, Ashby, Workday boards listed in `companies.json` plus remote-job aggregators, filters and scores them against `profile.json`, picks the resume variant, and commits `data/latest.json` and `data/digest-<date>.json`.
2. A Claude scheduled task runs at 07:00 PT, reads `data/latest.json`, does a final ranking, writes a short cover note per role, publishes a review page, and pings Emre.
3. Emre approves in chat; Claude submits the approved ones through his browser (Greenhouse/Lever/Ashby forms auto-filled; LinkedIn/Workday with him watching).

**Tuning**

- `profile.json` — role keywords, rejects, excluded employers, resume routing.
- `companies.json` — boards to crawl. Check `data/board_status.json` after a run; failed slugs are listed there.
- `data/seen.json` — dedupe memory; delete an id to make a job resurface.
- `data/applied.json` — log of submitted applications (maintained by the review session).
