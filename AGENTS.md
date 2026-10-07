# AGENTS.md

Context file for AI coding assistants working on this repo. Humans: see README.md.

## What this project does

A daily job-digest generator. It pulls remote **engineering** job listings from
four free public feeds, deduplicates each current fetch, optionally asks an AI to
classify each job's real hiring location, remote type, position and required
experience, and writes one CSV file per day listing the openings with links.

Owner is based in India (IST, UTC+05:30) and wants two views: openings inside
India and openings outside India. Outside-India jobs are split into "fully
remote (any country)" and "country/region-specific remote".

## File map

| File | Role |
|---|---|
| `job_digest.py` | Entry point. Fetch, filter, dedupe, call AI layer, bucket, write CSV. |
| `ai_layer.py` | AI classification. Two backends: ChatGPT website via Selenium, or OpenAI-compatible API. Also owns the cache. |
| `daily-jobs.yml` | GitHub Actions workflow. Belongs at `.github/workflows/daily-jobs.yml`. Runs daily 03:30 UTC (09:00 IST). |
| `requirements.txt` | `selenium` only. Needed only for the Selenium backend. |
| `README.md` | Human setup guide. |
| `AGENTS.md` | This file. |

Generated at runtime (do not hand-edit, do not treat as source):

| Path | Content |
|---|---|
| `digests/jobs-YYYY-MM-DD.csv` | The daily output (date is in IST). |
| `seen.json` | Legacy URL history; no longer used to filter daily output. |
| `ai_cache.json` | `{url: ai_result}` verdicts so a job is never classified twice. |
| `chrome_profile/` | Persistent Chrome profile holding the ChatGPT login (Selenium backend). Never commit it. |

All paths are relative to the current working directory, so scripts must be run
from the project root (or tests must `chdir` to a temp dir).

## Data flow (`job_digest.main`)

1. Call each source through `safe(name, fn)`. A failing source logs `FAILED` and
   returns `[]`; it must never abort the run.
2. Build the current listing set: drop jobs with no URL, duplicate URLs within
  the fetch, and anything failing `is_engineering` (title must contain an
  `INCLUDE` keyword and no `EXCLUDE` keyword). `seen.json` does not filter jobs.
3. If `AI_BACKEND` is not `none` and the listing set is non-empty, lazily import
  `ai_layer` and call `classify_jobs` to get `{url: ai_result}`. Cached verdicts
  are reused; uncached jobs are classified.
4. Bucket each job: with an AI result use `ai_bucket`, otherwise the keyword
   fallback `bucket`.
5. Write `digests/jobs-<date>.csv` with the current listing set.

## Data shapes

Job dict (built by `job()`):

    {"title": str, "company": str, "location": str, "url": str,
     "source": str, "description": str}

`description` is HTML-stripped, whitespace-collapsed and cut to 3000 chars.
`location` defaults to `"Not specified"`.

AI result (built by `ai_layer.parse_reply`, stored in `ai_cache.json`):

    {"actual_location": str,
      "position": str,
      "experience_required": str,
     "remote_type": "fully_remote" | "country_specific_remote" |
                    "region_specific_remote" | "hybrid_or_onsite" | "unclear",
     "countries": [str],
     "open_to_india": true | false | null,
     "reason": str}

`parse_reply` coerces bad values: unknown `remote_type` becomes `"unclear"`,
non-bool `open_to_india` becomes `null`, and missing experience becomes
`"Not specified"`. A missing position falls back to the feed title in the CSV.

## Bucketing rules

`ai_bucket(job, result)` returns a section key or `None` (drop the job):

1. `hybrid_or_onsite` -> `None` (dropped from the CSV).
2. `fully_remote` -> `"worldwide"`.
3. Not fully remote and the location or countries mention India -> `"india"`.
4. `country_specific_remote` / `region_specific_remote` -> `"region"`.
5. `unclear` -> fall back to `bucket(job)`.

`bucket(job)` is the keyword fallback on the listed location: India words ->
`"india"`; worldwide-ish words (or no location) -> `"worldwide"`; else `"region"`.

Section keys map to CSV values in the `section` column (`titles` dict). Jobs with
no AI result use the feed title as their position and "Not specified" for
experience.

## AI layer details

- Backend is chosen by env var `AI_BACKEND`: `selenium`, `api`, or `none` (default).
- Jobs are sent in batches of `AI_BATCH` (default 5). Each posting in a prompt is
  labelled `[1]..[n]`; those ids are **batch-local**, not global. The model must
  return a JSON array of objects keyed by `id`. `parse_reply` extracts the first
  `[` to last `]` with a regex, so extra prose or code fences around the array are
  tolerated.
- When an AI backend is enabled, all uncached jobs are classified by default.
  Set `AI_MAX_JOBS` to a positive value to impose a per-run cap; capped jobs
  remain in the CSV for later runs.
- Two consecutive failed batches stop the AI layer for that run. Results gathered
  so far are still cached.
- `ask_api(prompt)`: POST to `{AI_BASE_URL}/chat/completions` using
  `OPENAI_API_KEY`, model `AI_MODEL` (default `gpt-4o-mini`), temperature 0.
  Standard library only.
- `ChatGPTSession`: Selenium Chrome with `--user-data-dir=chrome_profile`. For each
  batch it opens a fresh temporary chat, types the prompt **collapsed to a single
  line** (a newline would submit the message early), presses Enter, waits for the
  stop-generating button to appear and then disappear, and reads the last
  assistant message element. The first run needs a human to log in in the visible
  window (the input-box wait is 300 s).
- The four page selectors are constants at the top of `ai_layer.py`
  (`CHATGPT_URL`, `INPUT_SEL`, `STOP_SEL`, `ANSWER_SEL`). If the Selenium backend
  breaks, that is the first place to look. They were written from memory and have
  **not** been verified against the live site.
- Selenium and the ChatGPT website backend cannot run on GitHub Actions (the login
  lives in a local browser profile). The workflow enables the `api` backend only
  when the `OPENAI_API_KEY` secret exists.

## Sources

| Function | Endpoint | Notes |
|---|---|---|
| `remotive()` | `remotive.com/api/remote-jobs?category=software-dev` | `candidate_required_location` is the location. |
| `remoteok()` | `remoteok.com/api` | First array item is legal text, not a job; filtered by `isinstance(j, dict) and j.get("position")`. |
| `wwr()` | We Work Remotely RSS (programming, devops) | Title format is `Company: Role`. Region comes from a `<region>` tag. |
| `himalayas()` | `himalayas.app/jobs/api` | `locationRestrictions` is a list. |

Field names for all four were written from memory and have **not** been tested
against live responses (the build environment could not reach these sites).
Expect to adjust them on first real run. LinkedIn, Naukri and similar sites are
deliberately not scraped (terms of use, bot blocking).

## Conventions and constraints

- Python 3.12, standard library only. `selenium` is imported lazily inside
  `ai_layer` so the base digest runs without it. Keep `ai_layer` imported lazily
  from `main`.
- One broken source or AI batch must never crash the run; catch, log, continue.
- Keep output as a CSV of the current matching listings, including position,
  experience, company, location, classification and link. No scoring or ranking
  was requested.
- Each run rebuilds the CSV from current feed responses, so listings appear
  again while they remain in the feeds. `ai_cache.json` avoids repeat AI calls
  for listings that already have a verdict; `seen.json` is retained but ignored.
- The GitHub workflow commits `digests/`, `seen.json` and (if present)
  `ai_cache.json` back to the repo. It needs "Read and write permissions".

## How to test without network or real AI

Monkeypatch instead of calling out:

- Set `AI_BACKEND=api` and `OPENAI_API_KEY=test`, then replace
  `ai_layer.ask_api` with a function that returns a JSON array string (it can be
  wrapped in prose or fences). Parse the `[n] Title:` ids out of the prompt to
  build the reply.
- Replace `job_digest.remotive`, `remoteok`, `wwr`, `himalayas` with lambdas
  returning hand-built job dicts. `main` looks them up at call time, so patching
  the module attributes works.
- `os.chdir` to a temp directory before calling `job_digest.main()` so
  `digests/` and `ai_cache.json` don't pollute the repo.
- Run `main()` twice: both CSVs should contain the fixture listings, while the
  AI cache prevents repeat model calls on the second run.
- Quick syntax check: `python3 -m py_compile job_digest.py ai_layer.py`.

## Common tasks

- **Change which roles are included:** edit `INCLUDE` / `EXCLUDE` in `job_digest.py`.
- **Add a source:** write a function returning a list of `job(...)` dicts (pass the
  description as the last argument), then add `("Name", fn)` to the list in `main`.
- **Add a field to the AI result:** update `PROMPT_HEAD`, `parse_reply`, and the
  CSV writer if it affects output. Old cache entries will lack the
  field, so read it with `.get()`.
- **Change the run time:** edit the cron line in `daily-jobs.yml` (it is UTC).
- **Deliver by email/Telegram/Slack:** not built yet. Add a step after the CSV
  file is written in `main`, read credentials from environment variables.

## Do not

- Do not scrape sites that forbid it, and do not add bot-evasion tricks beyond what
  is already in `ChatGPTSession`.
- Do not commit `chrome_profile/` or any API keys.
- Do not make the base digest depend on `selenium` or on an AI backend being present.
- Do not claim the Selenium backend or the source field names are verified until
  they have been run against the live sites.
