# tests/test_upwork_scanner.py
# Upwork 扫描器纯函数与去重持久化单元测试（纯本地 / 合成夹具明确标注）
import json
import os

import pytest

from tools.upwork_scanner import (clean_html, evaluate_job, extract_metadata,
                                  load_seen, save_seen, DISQUALIFIED_KEYWORDS,
                                  SEARCH_QUERIES)


def test_clean_html_strips_tags():
    raw = "<b>Budget</b>: $120 &amp; fixed price"
    # 标签替换为空格，"Budget" 与 ":" 之间会出现一个空格（可接受的清洗行为）
    assert clean_html(raw) == "Budget : $120 & fixed price"


def test_extract_metadata():
    is_hourly, budget = extract_metadata("<b>Budget</b>: $120 fixed")
    assert is_hourly is False and budget == 120.0
    is_hourly, budget = extract_metadata("Hourly Range: $20-$40")
    assert is_hourly is True and budget is None
    assert extract_metadata("no budget here") == (False, None)


def test_evaluate_job_filters():
    ok, _ = evaluate_job("Python CSV script", "fix a small csv parser", False, 120.0)
    assert ok is True
    # 时薪拒绝
    assert evaluate_job("x", "y", True, None)[0] is False
    # 预算区间
    assert evaluate_job("x", "y", False, 10.0)[0] is False
    assert evaluate_job("x", "y", False, 500.0)[0] is False
    # 敏感词熔断
    for kw in ("zoom", "anydesk", "jailbreak", "screen share"):
        assert evaluate_job(f"x {kw} y", "z", False, 100.0)[0] is False


def test_budget_boundary_unknown_budget_passes():
    # 无预算字段时不因预算拦截（仅时薪与敏感词可拦）
    ok, _ = evaluate_job("python script", "clean task", False, None)
    assert ok is True


def test_seen_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.upwork_scanner.SEEN_FILE", str(tmp_path / "seen.json"))
    seen = load_seen()
    assert seen == set()
    seen.add("https://upwork.com/jobs/1")
    seen.add("https://upwork.com/jobs/2")
    save_seen(seen)
    assert load_seen() == seen


def test_compliance_no_polling_loop():
    # 静态断言：扫描器源码不得含常驻轮询循环构造
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "upwork_scanner.py")
    src = open(src_path, encoding="utf-8").read()
    assert "while True" not in src
    assert "sleep(300)" not in src
    # 查询清单与敏感词清单齐备
    assert len(SEARCH_QUERIES) >= 5
    assert "jailbreak" in DISQUALIFIED_KEYWORDS
