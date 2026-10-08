# tools/upwork_scanner.py
"""
Upwork 确定性微任务扫描器 —— On-Demand 单次批处理（合规版）。

合规约束（Phase 1.5 审计 + 控制器合规引擎）：
- 严禁常驻后台轮询循环（Upwork ToS 4.1 禁止持续监测内容语料）；
- 每次执行为一次多查询单遍扫描（用户定向任务），去重状态持久化；
- 仅拉取公开 RSS（GET），异常记日志并按查询重试 1 次；
- 命中任务追加写入 matched_jobs.jsonl（字段：timestamp/title/link/budget/pub_date/snippet/query）。
"""
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

SEARCH_QUERIES = [
    "python api",
    "python automation",
    "python script",
    "csv automation",
    "data cleaning python",
]

DISQUALIFIED_KEYWORDS = [
    "screen share", "screenshare", "call", "zoom", "meet", "voice",
    "anydesk", "teamviewer", "hourly", "long term", "jailbreak", "bypass"
]

MIN_BUDGET = 30.0
MAX_BUDGET = 300.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

OUTPUT_FILE = "matched_jobs.jsonl"
SEEN_FILE = "data/upwork_seen_links.json"
LOG_FILE = "logs/upwork_scanner.log"


def log(msg: str):
    line = f"[{datetime.now().isoformat()}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clean_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    return " ".join(html.unescape(text).split())


def extract_metadata(description_html: str):
    is_hourly = "Hourly Range:" in (description_html or "")
    budget_match = re.search(r"<b>Budget</b>:\s*\$([0-9,]+)", description_html or "")
    budget = float(budget_match.group(1).replace(",", "")) if budget_match else None
    return is_hourly, budget


def evaluate_job(title: str, description_text: str, is_hourly: bool, budget):
    full_text = f"{title} {description_text}".lower()
    if is_hourly:
        return False, "Hourly job rejected"
    if budget is not None and (budget < MIN_BUDGET or budget > MAX_BUDGET):
        return False, f"Budget out of range (${budget})"
    for kw in DISQUALIFIED_KEYWORDS:
        if kw in full_text:
            return False, f"Triggered keyword: {kw}"
    return True, "Passed"


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    os.makedirs(os.path.dirname(SEEN_FILE) or ".", exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def fetch_feed(query: str) -> str:
    """单查询拉取（GET，异常时重试 1 次）。"""
    url = (f"https://www.upwork.com/ab/feed/jobs/rss?q={query.replace(' ', '+')}"
           f"&sort=recency")
    last_err = None
    for attempt in range(2):
        try:
            res = requests.get(url, headers=HEADERS, timeout=12)
            if res.status_code == 200:
                return res.content
            last_err = f"HTTP {res.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(2)
    log(f"fetch failed for '{query}': {last_err}")
    return None


def scan_once(seen: set = None) -> dict:
    """单次批处理：多查询单遍扫描（无循环，无守护）。"""
    seen = seen if seen is not None else load_seen()
    hits = 0
    errors = 0
    for query in SEARCH_QUERIES:
        content = fetch_feed(query)
        if content is None:
            errors += 1
            continue
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            log(f"RSS parse error '{query}': {e}")
            errors += 1
            continue
        for item in root.findall("./channel/item"):
            link_el = item.find("link")
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            if not link or link in seen:
                continue
            seen.add(link)

            title_el = item.find("title")
            pub_el = item.find("pubDate")
            desc_el = item.find("description")
            title = title_el.text if title_el is not None and title_el.text else ""
            pub_date = pub_el.text if pub_el is not None and pub_el.text else ""
            desc_raw = desc_el.text if desc_el is not None and desc_el.text else ""

            is_hourly, budget = extract_metadata(desc_raw)
            clean_desc = clean_html(desc_raw)
            passed, reason = evaluate_job(title, clean_desc, is_hourly, budget)

            if passed:
                record = {
                    "timestamp": datetime.now().isoformat(),
                    "title": title,
                    "budget": budget,
                    "pub_date": pub_date,
                    "link": link,
                    "snippet": clean_desc[:250],
                    "query": query
                }
                with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                hits += 1
                log(f"[HIT] ${budget} | {title[:60]} | {link}")
        time.sleep(1)  # 查询间隔礼让
    save_seen(seen)
    log(f"scan_once complete: hits={hits} errors={errors} seen={len(seen)}")
    return {"hits": hits, "errors": errors, "seen_total": len(seen)}


def main():
    log("On-demand single-batch scan started (no daemon, no polling loop).")
    result = scan_once()
    print(json.dumps(result))
    log("PHYSICAL STOP. Awaiting next manual trigger.")


if __name__ == "__main__":
    main()
