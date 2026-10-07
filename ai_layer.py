#!/usr/bin/env python3
"""AI layer for the remote jobs digest.

Sends job descriptions (in small batches) to an AI and gets back JSON saying
where the company is really hiring and what kind of remote the role is.

Pick a backend with the AI_BACKEND environment variable:
  selenium  Drives the ChatGPT website in a Chrome profile that you log into
            once (first run opens a visible window). Runs on YOUR machine only.
  api       Any OpenAI-compatible API. Needs OPENAI_API_KEY; optional
            AI_BASE_URL (default https://api.openai.com/v1) and AI_MODEL
            (default gpt-4o-mini). Works on GitHub Actions.
  none      Skip the AI layer (default).

Other knobs: AI_BATCH (jobs per prompt, 5), AI_MAX_JOBS (optional per-run cap),
AI_DESC_CHARS (description chars sent per job, 1500), AI_HEADLESS=1,
AI_PROFILE_DIR (Chrome profile folder, ./chrome_profile).

Results are cached in ai_cache.json so a job is never classified twice.
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

CACHE_FILE = Path("ai_cache.json")
PROFILE_DIR = Path(os.environ.get("AI_PROFILE_DIR", "chrome_profile"))
BATCH = int(os.environ.get("AI_BATCH", "5"))
MAX_JOBS = int(os.environ.get("AI_MAX_JOBS", "0"))
DESC_CHARS = int(os.environ.get("AI_DESC_CHARS", "1500"))

REMOTE_TYPES = {
    "fully_remote",
    "country_specific_remote",
    "region_specific_remote",
    "hybrid_or_onsite",
    "unclear",
}

# ChatGPT page details. The site changes now and then; if the Selenium
# backend stops finding things, update these four lines.
CHATGPT_URL = "https://chatgpt.com/?temporary-chat=true"
INPUT_SEL = "#prompt-textarea"
STOP_SEL = "button[data-testid='stop-button']"
ANSWER_SEL = "div[data-message-author-role='assistant']"

PROMPT_HEAD = """You are screening job postings. For EACH posting below, identify the hiring position, required experience, where the company is actually hiring, and what kind of remote the role is.
Return ONLY a JSON array (no prose, no markdown fences), one object per posting, exactly in this shape:
{"id": <number>, "position": "<hiring role/title>", "experience_required": "<required experience, or Not specified>", "actual_location": "<country/region/city the hire must be based in, or Worldwide>", "remote_type": "fully_remote" | "country_specific_remote" | "region_specific_remote" | "hybrid_or_onsite" | "unclear", "countries": ["<allowed countries; empty if worldwide>"], "open_to_india": true | false | null, "reason": "<max 12 words>"}
Rules: fully_remote means candidates in any country can apply. country_specific_remote means remote but only for candidates in specific countries. region_specific_remote means remote but limited to a region or time zone range (for example EU, APAC, Americas). hybrid_or_onsite means the person must work from an office at least some days. Trust the description text over the listed location when they disagree (for example listed Worldwide but the text says US only). Use null for open_to_india if you cannot tell.
Use the posting's stated role as the position; use the listed title if the description does not give a different role. Report experience only when the posting states a requirement, preserving its wording (for example, 5+ years or 3-5 years). Do not infer experience; return "Not specified" when absent.

POSTINGS:
"""


def build_prompt(batch):
    lines = []
    for i, j in enumerate(batch, 1):
        desc = (j.get("description") or "")[:DESC_CHARS]
        lines.append(
            f"[{i}] Title: {j['title']} | Company: {j['company']} | "
            f"Listed location: {j['location']} | Description: {desc}"
        )
    return PROMPT_HEAD + "\n".join(lines)


def parse_reply(text, n):
    """Pull the JSON array out of a reply; return {batch_index: result}."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply")
    data = json.loads(m.group(0))
    out = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= idx <= n:
            continue
        rt = item.get("remote_type")
        if rt not in REMOTE_TYPES:
            rt = "unclear"
        countries = item.get("countries") or []
        if not isinstance(countries, list):
            countries = [countries]
        open_to_india = item.get("open_to_india")
        if not isinstance(open_to_india, bool):
            open_to_india = None
        out[idx] = {
            "position": str(item.get("position") or ""),
            "experience_required": str(item.get("experience_required") or "Not specified"),
            "actual_location": str(item.get("actual_location") or "Unclear"),
            "remote_type": rt,
            "countries": [str(c) for c in countries],
            "open_to_india": open_to_india,
            "reason": str(item.get("reason") or "")[:120],
        }
    return out


# ---- Backend: OpenAI-compatible API ---------------------------------------
def ask_api(prompt):
    base = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    body = json.dumps({
        "model": os.environ.get("AI_MODEL", "gpt-4o-mini"),
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


# ---- Backend: ChatGPT website via Selenium --------------------------------
class ChatGPTSession:
    def __init__(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        opts.add_argument(f"--user-data-dir={PROFILE_DIR.resolve()}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1200,900")
        if os.environ.get("AI_HEADLESS") == "1":
            opts.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=opts)

    def ask(self, prompt, timeout=240):
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        d = self.driver
        d.get(CHATGPT_URL)
        print("  waiting for ChatGPT input box (log in in the browser window if asked)...")
        box = WebDriverWait(d, 300).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, INPUT_SEL))
        )
        box.click()
        # One line only: a newline would submit the message early.
        box.send_keys(re.sub(r"\s+", " ", prompt))
        time.sleep(1)
        box.send_keys(Keys.ENTER)
        try:
            WebDriverWait(d, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, STOP_SEL))
            )
        except TimeoutException:
            pass  # a very fast reply can finish before we ever see the stop button
        WebDriverWait(d, timeout).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, STOP_SEL))
        )
        time.sleep(2)
        answers = d.find_elements(By.CSS_SELECTOR, ANSWER_SEL)
        if not answers:
            raise RuntimeError("no answer found on the page")
        return answers[-1].text

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


def _make_asker():
    backend = os.environ.get("AI_BACKEND", "none").lower()
    if backend == "api":
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")
        return ask_api, (lambda: None)
    if backend == "selenium":
        session = ChatGPTSession()
        return session.ask, session.close
    return None, (lambda: None)


def classify_jobs(jobs):
    """Return {url: result} for every job we have an AI verdict on."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    todo = [
        j for j in jobs
        if j["url"] not in cache
        or not {"position", "experience_required"}.issubset(cache.get(j["url"]) or {})
    ]
    if MAX_JOBS > 0:
        todo = todo[:MAX_JOBS]

    if todo:
        try:
            ask, close = _make_asker()
        except Exception as e:
            print(f"AI layer disabled: {e}")
            ask, close = None, (lambda: None)

        if ask:
            fails = 0
            try:
                for start in range(0, len(todo), BATCH):
                    batch = todo[start:start + BATCH]
                    try:
                        results = parse_reply(ask(build_prompt(batch)), len(batch))
                    except Exception as e:
                        fails += 1
                        print(f"AI batch failed ({e})")
                        if fails >= 2:
                            print("Stopping the AI layer for this run")
                            break
                        continue
                    fails = 0
                    for i, j in enumerate(batch, 1):
                        if i in results:
                            cache[j["url"]] = results[i]
                    print(f"AI classified {len(results)}/{len(batch)}")
                    time.sleep(3)
            finally:
                close()
            CACHE_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")

    return {j["url"]: cache[j["url"]] for j in jobs if j["url"] in cache}
