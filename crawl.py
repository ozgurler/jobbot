#!/usr/bin/env python3
"""
Daily job crawler + matcher for Emre's job search.

Pulls new postings from public ATS APIs (Greenhouse, Lever, Ashby, Workday) and
remote-job aggregators, filters to relevant remote/Portland roles, scores them
against profile.json, picks the resume variant, and writes:

  data/digest-YYYY-MM-DD.json   ranked matches for today (input for the review step)
  data/latest.json              same as above, stable path
  data/seen.json                every job id we've already surfaced (dedupe across days)
  data/board_status.json        which boards responded / failed (prune from here)

No LLM calls here on purpose: this is the deterministic, cheap layer. Cover notes
and final ranking happen in the Claude session that reads the digest.
"""
import json, re, sys, time, hashlib, datetime as dt, html
from pathlib import Path
import urllib.request, urllib.error, urllib.parse

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

PROFILE = json.loads((ROOT / "profile.json").read_text())
COMPANIES = json.loads((ROOT / "companies.json").read_text())
NOW = dt.datetime.now(dt.timezone.utc)
LOOKBACK = dt.timedelta(hours=PROFILE["limits"]["lookback_hours"])
UA = "Mozilla/5.0 (compatible; jobbot/1.0; +https://www.emreozgurler.com)"

def get(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers: h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def strip_html(s):
    if not s: return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", s).strip()

def parse_ts(v):
    """Accepts ISO strings, epoch seconds/millis. Returns aware datetime or None."""
    if v is None: return None
    try:
        if isinstance(v, (int, float)):
            if v > 1e12: v = v / 1000
            return dt.datetime.fromtimestamp(v, dt.timezone.utc)
        s = str(v).strip()
        if re.fullmatch(r"\d{13}", s): return dt.datetime.fromtimestamp(int(s)/1000, dt.timezone.utc)
        if re.fullmatch(r"\d{10}", s): return dt.datetime.fromtimestamp(int(s), dt.timezone.utc)
        s = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None

def job_id(source, company, title, url):
    return hashlib.sha1(f"{source}|{company}|{title}|{url}".lower().encode()).hexdigest()[:16]

# ---------------------------------------------------------------- sources

def fetch_greenhouse(slug):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        out.append(dict(
            source="greenhouse", company=slug, title=j.get("title",""),
            location=(j.get("location") or {}).get("name",""),
            url=j.get("absolute_url",""), posted=parse_ts(j.get("updated_at") or j.get("first_published")),
            description=strip_html(j.get("content","")), ats="greenhouse",
        ))
    return out

def fetch_lever(slug):
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        loc = ", ".join(x for x in [cat.get("location",""), cat.get("commitment","")] if x)
        if j.get("workplaceType"): loc += f" ({j['workplaceType']})"
        out.append(dict(
            source="lever", company=slug, title=j.get("text",""), location=loc,
            url=j.get("hostedUrl",""), posted=parse_ts(j.get("createdAt")),
            description=strip_html(j.get("descriptionPlain") or j.get("description","")), ats="lever",
        ))
    return out

def fetch_ashby(slug):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        loc = j.get("location","") or ""
        if j.get("isRemote"): loc += " (Remote)"
        comp = (j.get("compensation") or {}).get("compensationTierSummary","")
        out.append(dict(
            source="ashby", company=j.get("organizationName") or slug, title=j.get("title",""),
            location=loc, url=j.get("jobUrl") or j.get("applyUrl",""),
            posted=parse_ts(j.get("publishedAt")), description=strip_html(j.get("descriptionHtml","")),
            ats="ashby", salary=comp,
        ))
    return out

def fetch_workday(name, url):
    """Workday CXS search API. Search a few keyword sets, last 7 days, dedupe."""
    out, seen = [], set()
    base = url.rsplit("/jobs", 1)[0]
    site = url.rstrip("/").split("/")[-2]
    tenant_host = url.split("/wday/")[0]
    for q in ["sales engineer", "account executive", "account manager", "business development", "solutions", "technical sales", "field service"]:
        payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}
        try:
            d = get(url, data=payload, headers={"Referer": tenant_host})
        except Exception as e:
            raise
        for j in d.get("jobPostings", []):
            path = j.get("externalPath","")
            if path in seen: continue
            seen.add(path)
            posted_on = j.get("postedOn","")  # "Posted 3 Days Ago", "Posted Today", "Posted Yesterday"
            days = None
            m = re.search(r"(\d+)\+? Days? Ago", posted_on)
            if "Today" in posted_on: days = 0
            elif "Yesterday" in posted_on: days = 1
            elif m: days = int(m.group(1))
            posted = NOW - dt.timedelta(days=days) if days is not None else None
            out.append(dict(
                source="workday", company=name, title=j.get("title",""),
                location=j.get("locationsText",""), url=f"{tenant_host}/{site}{path}" if path.startswith("/") else path,
                posted=posted, description="", ats="workday",
            ))
    return out

def fetch_remotive(url):
    d = get(url)
    return [dict(
        source="remotive", company=j.get("company_name",""), title=j.get("title",""),
        location=(j.get("candidate_required_location","") or "") + " (Remote)", url=j.get("url",""),
        posted=parse_ts(j.get("publication_date")), description=strip_html(j.get("description","")),
        ats="external", salary=j.get("salary",""),
    ) for j in d.get("jobs", [])]

def fetch_remoteok(url):
    d = get(url)
    out = []
    for j in d:
        if not isinstance(j, dict) or "position" not in j: continue
        out.append(dict(
            source="remoteok", company=j.get("company",""), title=j.get("position",""),
            location=(j.get("location","") or "") + " (Remote)", url=j.get("url",""),
            posted=parse_ts(j.get("epoch") or j.get("date")), description=strip_html(j.get("description","")),
            ats="external", salary=(f"${j['salary_min']}-{j['salary_max']}" if j.get("salary_min") else ""),
        ))
    return out

def fetch_himalayas(url):
    d = get(url)
    out = []
    for j in d.get("jobs", []):
        out.append(dict(
            source="himalayas", company=j.get("companyName",""), title=j.get("title",""),
            location=", ".join(j.get("locationRestrictions") or []) + " (Remote)",
            url=j.get("applicationLink") or j.get("guid",""), posted=parse_ts(j.get("pubDate")),
            description=strip_html(j.get("description","")), ats="external",
            salary=(f"${j['minSalary']}-{j['maxSalary']}" if j.get("minSalary") else ""),
        ))
    return out

# ---------------------------------------------------------------- filtering & scoring

T = PROFILE["title_tiers"]
LOC = PROFILE["location"]
EXCL = [e.lower() for e in PROFILE["exclude_employers"]]

def has(terms, text):
    return [t for t in terms if t in text]

def classify(job):
    title = job["title"].lower()
    loc = (job["location"] or "").lower()
    desc = (job.get("description") or "").lower()
    comp = (job["company"] or "").lower()

    if any(e in comp for e in EXCL): return None, "excluded employer"
    if has(T["reject"], title): return None, "rejected title"

    core = has(T["core"], title)
    adj = has(T["adjacent"], title)
    semi = has(T["semiconductor"], title + " " + comp)
    if not (core or adj or semi): return None, "title not relevant"

    remote = any(k in loc for k in ["remote", "anywhere", "work from home", "wfh"]) or \
             ("remote" in desc[:1500]) or "(remote)" in loc
    portland = any(re.search(r"\b" + re.escape(k) + r"\b", loc) for k in LOC["home_metro"]) and "orlando" not in loc
    us = any(k in loc for k in ["united states", "usa", "us", "u.s.", "america", "north america", "americas", "worldwide", "global", "anywhere"]) \
         or re.search(r"\b[a-z ]+, [a-z]{2}\b", loc) is not None or loc.strip() == ""
    non_us_only = any(k in loc for k in ["united kingdom", "london", "germany", "berlin", "india", "bangalore", "canada only", "australia", "sydney", "singapore", "japan", "tokyo", "france", "paris", "netherlands", "amsterdam", "ireland", "dublin", "poland", "spain", "brazil", "mexico", "emea only", "apac", "latam", "philippines", "israel", "tel aviv"]) and not (portland or "united states" in loc)
    if non_us_only: return None, "non-US location"
    if not (remote or portland or us): return None, "location unclear/onsite elsewhere"
    if any(k in desc for k in LOC["reject_terms"]): return None, "onsite elsewhere"

    # score
    score = 0
    score += 40 if core else (25 if adj else 0)
    if semi: score += 20
    score += 15 if remote else (12 if portland else 0)
    skills = has(PROFILE["skill_terms"], desc)
    score += min(25, len(skills) * 2)
    if any(k in title for k in ["senior", "sr.", "sr ", "enterprise", "strategic", "lead"]): score += 3
    if any(k in title for k in ["associate", "junior", "entry"]): score -= 5
    if job.get("salary"): score += 2
    if job["source"] in ("greenhouse", "lever", "ashby"): score += 5  # can be auto-submitted
    if job["source"] == "remoteok": score -= 5  # noisier

    # resume routing
    sales_like = bool(core or adj)
    if semi and sales_like: resume = "semiconductor_sales"
    elif semi: resume = "semiconductor"
    else: resume = "generic"

    return dict(score=score, remote=remote, portland=portland, matched_title=core + adj + semi,
                matched_skills=skills[:12], resume=resume), "ok"

# ---------------------------------------------------------------- main

def main():
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    status, jobs = {}, []

    def run(label, fn, *a):
        try:
            res = fn(*a)
            status[label] = {"ok": True, "count": len(res)}
            jobs.extend(res)
        except urllib.error.HTTPError as e:
            status[label] = {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            status[label] = {"ok": False, "error": str(e)[:120]}

    for s in dict.fromkeys(COMPANIES.get("greenhouse", [])): run(f"greenhouse:{s}", fetch_greenhouse, s)
    for s in dict.fromkeys(COMPANIES.get("lever", [])): run(f"lever:{s}", fetch_lever, s)
    for s in dict.fromkeys(COMPANIES.get("ashby", [])): run(f"ashby:{s}", fetch_ashby, s)
    for w in COMPANIES.get("workday", []): run(f"workday:{w['name']}", fetch_workday, w["name"], w["url"])
    agg = COMPANIES.get("aggregators", {})
    for k, u in agg.items():
        if k.startswith("remotive"): run(f"agg:{k}", fetch_remotive, u)
        elif k == "remoteok": run(f"agg:{k}", fetch_remoteok, u)
        elif k == "himalayas": run(f"agg:{k}", fetch_himalayas, u)

    fresh, rejected = [], {}
    for j in jobs:
        jid = job_id(j["source"], j["company"], j["title"], j["url"])
        if jid in seen: continue
        if j["posted"] and (NOW - j["posted"]) > LOOKBACK:
            continue  # old posting; still not marked seen so a re-post surfaces
        cls, why = classify(j)
        if not cls:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        j.update(cls)
        j["id"] = jid
        j["posted"] = j["posted"].isoformat() if j["posted"] else None
        j["description"] = j["description"][:3000]
        fresh.append(j)

    fresh.sort(key=lambda x: -x["score"])
    top = fresh[: PROFILE["limits"]["max_digest"]]
    for j in top: seen[j["id"]] = {"date": NOW.date().isoformat(), "company": j["company"], "title": j["title"]}

    digest = {
        "generated_at": NOW.isoformat(),
        "counts": {"fetched": len(jobs), "fresh_matches": len(fresh), "in_digest": len(top),
                    "boards_ok": sum(1 for v in status.values() if v["ok"]),
                    "boards_failed": sum(1 for v in status.values() if not v["ok"])},
        "rejected_reasons": rejected,
        "jobs": top,
    }
    (DATA / f"digest-{NOW.date().isoformat()}.json").write_text(json.dumps(digest, indent=1))
    (DATA / "latest.json").write_text(json.dumps(digest, indent=1))
    seen_path.write_text(json.dumps(seen, indent=0))
    (DATA / "board_status.json").write_text(json.dumps(status, indent=1))
    print(json.dumps(digest["counts"]), file=sys.stderr)
    for j in top[:15]:
        print(f"{j['score']:>3}  {j['resume']:<19} {j['company'][:22]:<22} {j['title'][:60]}  {j['url']}", file=sys.stderr)

if __name__ == "__main__":
    main()
