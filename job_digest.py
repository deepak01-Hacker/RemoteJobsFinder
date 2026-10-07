#!/usr/bin/env python3
"""Daily remote engineering jobs digest.

Fetches listings from free public feeds, filters for engineering roles, drops
anything already seen, optionally runs an AI layer (see ai_layer.py) that works
out where each job really hires and what kind of remote it is, and writes a
CSV file with position, experience, company, location and link.

Run: python job_digest.py
With AI: AI_BACKEND=selenium python job_digest.py   (or AI_BACKEND=api)
"""
import csv
import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- Edit these -----------------------------------------------------------
INCLUDE = [
    "engineer", "developer", "software", "backend", "back-end", "frontend",
    "front-end", "full stack", "full-stack", "devops", "sre", "site reliability",
    "platform", "data engineer", "machine learning", "ml engineer", "mobile",
    "android", "ios", "security engineer", "qa engineer", "sdet", "cloud",
]
EXCLUDE = ["sales engineer", "support engineer", "intern ", "recruiter"]
# ---------------------------------------------------------------------------

INDIA_WORDS = [
    "india", "bangalore", "bengaluru", "gurugram", "gurgaon", "delhi", "noida",
    "mumbai", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad",
]
WORLDWIDE_WORDS = ["worldwide", "anywhere", "global", "remote", "apac", "asia"]

OUT_DIR = Path("digests")
UA = {"User-Agent": "remote-jobs-digest/1.0 (personal use)"}

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def safe(name, fn):
    try:
        jobs = fn()
        print(f"{name}: {len(jobs)} listings")
        return jobs
    except Exception as e:  # one broken source must not kill the run
        print(f"{name}: FAILED ({e})")
        return []


def clean(text, limit=3000):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def job(title, company, location, url, source, description=""):
    return {
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "location": (location or "Not specified").strip() or "Not specified",
        "url": (url or "").strip(),
        "source": source,
        "description": clean(description),
    }


def remotive():
    data = json.loads(fetch("https://remotive.com/api/remote-jobs?category=software-dev&limit=200"))
    return [job(j.get("title"), j.get("company_name"),
                j.get("candidate_required_location"), j.get("url"), "Remotive",
                j.get("description"))
            for j in data.get("jobs", [])]


def remoteok():
    data = json.loads(fetch("https://remoteok.com/api"))
    return [job(j.get("position"), j.get("company"), j.get("location") or "Worldwide",
                j.get("url"), "RemoteOK", j.get("description"))
            for j in data if isinstance(j, dict) and j.get("position")]


def wwr():
    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    ]
    out = []
    for f in feeds:
        root = ET.fromstring(fetch(f))
        for item in root.iter("item"):
            raw = item.findtext("title") or ""
            company, _, title = raw.partition(":")
            if not title:
                company, title = "", raw
            region = item.findtext("region") or "Worldwide"
            out.append(job(title, company, region, item.findtext("link"),
                           "WeWorkRemotely", item.findtext("description")))
    return out


def himalayas():
    data = json.loads(fetch("https://himalayas.app/jobs/api?limit=100"))
    out = []
    for j in data.get("jobs", []):
        loc = j.get("locationRestrictions") or []
        loc = ", ".join(loc) if isinstance(loc, list) else str(loc)
        out.append(job(j.get("title"), j.get("companyName"), loc or "Worldwide",
                       j.get("applicationLink") or j.get("guid"), "Himalayas",
                       j.get("description") or j.get("excerpt")))
    return out


def is_engineering(j):
    t = j["title"].lower() + " "
    return any(k in t for k in INCLUDE) and not any(k in t for k in EXCLUDE)


def bucket(j):
    """Keyword-based guess from the listed location (used when there is no AI verdict)."""
    loc = j["location"].lower()
    if any(w in loc for w in INDIA_WORDS):
        return "india"
    if any(re.search(rf"\b{w}\b", loc) for w in WORLDWIDE_WORDS) or loc == "not specified":
        return "worldwide"
    return "region"


def ai_bucket(j, r):
    """Section for a job with an AI verdict, or None to drop it (not remote)."""
    rt = r["remote_type"]
    if rt == "hybrid_or_onsite":
        return None
    loc = r["actual_location"].lower()
    in_india = any(w in loc for w in INDIA_WORDS) or any("india" in c.lower() for c in r["countries"])
    if rt == "fully_remote":
        return "worldwide"
    if in_india:
        return "india"
    if rt in ("country_specific_remote", "region_specific_remote"):
        return "region"
    return bucket(j)  # unclear: fall back to the keyword guess


def main():
    all_jobs = []
    for name, fn in [("Remotive", remotive), ("RemoteOK", remoteok),
                     ("WeWorkRemotely", wwr), ("Himalayas", himalayas)]:
        all_jobs += safe(name, fn)

    current_jobs, batch_urls = [], set()
    for j in all_jobs:
        if not j["url"] or j["url"] in batch_urls:
            continue
        if not is_engineering(j):
            continue
        batch_urls.add(j["url"])
        current_jobs.append(j)

    ai = {}
    if os.environ.get("AI_BACKEND", "none").lower() != "none" and current_jobs:
        from ai_layer import classify_jobs
        ai = classify_jobs(current_jobs)

    groups = {"india": [], "worldwide": [], "region": []}
    for j in current_jobs:
        r = ai.get(j["url"])
        key = ai_bucket(j, r) if r else bucket(j)
        if key is None:
            continue
        groups[key].append((j, r))

    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).strftime("%Y-%m-%d")
    titles = {
        "india": "Inside India (India-based or India-eligible remote)",
        "worldwide": "Outside India - Fully remote (any country)",
        "region": "Outside India - Country/region-specific remote",
    }

    listed = sum(len(v) for v in groups.values())
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"jobs-{today}.csv"
    fields = [
        "section", "position", "experience_required", "company", "listed_location",
        "actual_location", "remote_type", "countries", "open_to_india", "reason",
        "url", "source",
    ]
    with out.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for key in ("india", "worldwide", "region"):
            for j, r in sorted(groups[key], key=lambda item: item[0]["company"].lower()):
                writer.writerow({
                    "section": titles[key],
                    "position": (r.get("position") or j["title"]) if r else j["title"],
                    "experience_required": (
                        r.get("experience_required", "Not specified") if r else "Not specified"
                    ),
                    "company": j["company"],
                    "listed_location": j["location"],
                    "actual_location": r.get("actual_location", "") if r else "",
                    "remote_type": r.get("remote_type", "") if r else "",
                    "countries": "; ".join(r.get("countries", [])) if r else "",
                    "open_to_india": r.get("open_to_india", "") if r else "",
                    "reason": r.get("reason", "") if r else "",
                    "url": j["url"],
                    "source": j["source"],
                })

    print(f"Wrote {out} with {listed} listings")


if __name__ == "__main__":
    main()
