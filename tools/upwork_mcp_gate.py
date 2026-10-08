# tools/upwork_mcp_gate.py
"""
AIE Commercial Discovery — Upwork MCP Gate U-MCP-1（只读 Discovery 合规外壳）。

Source Health 枚举：MCP_OK / MCP_EMPTY / MCP_AUTH_REQUIRED / MCP_ERROR / MCP_UNAVAILABLE。
统一输出 Schema（每 Job 至少 11 字段）与现有去重（data/upwork_seen_links.json /
matched_jobs.jsonl）保持格式兼容。

合规暂停条件（本 Gate 不实现，须先经 Upwork Support 确认）：
  自动定时搜索 / AI filtering/scoring / 持久化 MCP 输出 / hosted client / 多 tool 联动。
本模块只做：单次 Discovery 结果的结构化落盘（由已授权会话注入结果），零写操作。
"""
import json
import os
from datetime import datetime, timezone

OUTPUT_FILE = "matched_jobs.jsonl"
SEEN_FILE = "data/upwork_seen_links.json"
SCHEMA_FIELDS = [
    "source", "job_id", "url", "title", "description", "budget",
    "job_type", "skills", "posted_time", "proposal_count",
    "retrieved_at", "raw_source_metadata"
]

MCP_HEALTH_STATES = ("MCP_OK", "MCP_EMPTY", "MCP_AUTH_REQUIRED",
                     "MCP_ERROR", "MCP_UNAVAILABLE")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    os.makedirs(os.path.dirname(SEEN_FILE) or ".", exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


def normalize_job(raw: dict) -> dict:
    """MCP 原始返回 -> 统一 Schema（11 字段齐备，缺失显式置 None）。"""
    job = {k: None for k in SCHEMA_FIELDS}
    job.update({
        "source": "upwork_mcp",
        "job_id": raw.get("job_id") or raw.get("id") or raw.get("jobUid"),
        "url": raw.get("url") or raw.get("job_url"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "budget": raw.get("budget"),
        "job_type": raw.get("job_type") or raw.get("type"),
        "skills": raw.get("skills"),
        "posted_time": raw.get("posted_time") or raw.get("posted_at"),
        "proposal_count": raw.get("proposal_count") or raw.get("proposals"),
        "retrieved_at": now_iso(),
        "raw_source_metadata": raw
    })
    return job


def persist_discovery(jobs: list) -> dict:
    """单次 Discovery 结果落盘：URL 去重（不破坏现有格式）。"""
    seen = load_seen()
    written = 0
    duplicates = 0
    for raw in jobs:
        job = normalize_job(raw)
        url = job["url"]
        if url and url in seen:
            duplicates += 1
            continue
        if url:
            seen.add(url)
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
        written += 1
    save_seen(seen)
    return {"written": written, "duplicates": duplicates, "seen_total": len(seen)}


def probe_connection(upwork_tools: list = None) -> dict:
    """连接状态探测：基于本会话实际注册的 upwork__* MCP 工具集合。"""
    tools = upwork_tools if upwork_tools is not None else []
    if not tools:
        return {"source_health": "MCP_AUTH_REQUIRED",
                "reason": "no upwork__* MCP tools registered in this session "
                          "(OAuth browser login not completed)"}
    return {"source_health": "MCP_OK", "tools": tools}
