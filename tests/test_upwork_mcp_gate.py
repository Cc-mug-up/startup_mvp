# tests/test_upwork_mcp_gate.py
# Gate U-MCP-1 合规外壳单元测试（纯本地）
import json
import os

from tools.upwork_mcp_gate import (SCHEMA_FIELDS, MCP_HEALTH_STATES,
                                   normalize_job, persist_discovery, probe_connection)


def test_schema_fields_complete():
    job = normalize_job({"id": "j1", "url": "https://upwork.com/jobs/1",
                         "title": "Python fix", "description": "desc",
                         "budget": 100, "type": "fixed", "skills": ["python"]})
    assert all(k in job for k in SCHEMA_FIELDS)
    assert job["source"] == "upwork_mcp"
    assert job["job_id"] == "j1"
    assert job["retrieved_at"] is not None


def test_schema_missing_fields_null():
    job = normalize_job({})
    assert job["title"] is None and job["budget"] is None
    assert job["source"] == "upwork_mcp"


def test_persist_dedupe_compatible(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.upwork_mcp_gate.OUTPUT_FILE", str(tmp_path / "matched.jsonl"))
    monkeypatch.setattr("tools.upwork_mcp_gate.SEEN_FILE", str(tmp_path / "seen.json"))
    jobs = [
        {"id": "j1", "url": "https://upwork.com/jobs/1", "title": "A"},
        {"id": "j2", "url": "https://upwork.com/jobs/2", "title": "B"},
        {"id": "j1b", "url": "https://upwork.com/jobs/1", "title": "A dup"},
    ]
    r = persist_discovery(jobs)
    assert r["written"] == 2 and r["duplicates"] == 1
    # 二次执行：全部去重
    r2 = persist_discovery(jobs)
    assert r2["written"] == 0 and r2["duplicates"] == 3
    # jsonl 格式兼容（行式 JSON）
    with open(str(tmp_path / "matched.jsonl"), encoding="utf-8") as f:
        for line in f:
            json.loads(line)


def test_probe_connection_states():
    assert probe_connection([])["source_health"] == "MCP_AUTH_REQUIRED"
    assert probe_connection(["upwork__search_jobs"])["source_health"] == "MCP_OK"
    assert set(MCP_HEALTH_STATES) == {"MCP_OK", "MCP_EMPTY", "MCP_AUTH_REQUIRED",
                                      "MCP_ERROR", "MCP_UNAVAILABLE"}
