# tests/test_opportunity_filter.py
# Gate 4.4-B 确定性单测：粗筛过滤器与特征抽取器（纯 Mock / 零 LLM / 零网络）
import os
import pytest
from tools.opportunity_filter import OpportunityFilter

CURRENT = "2026-10-04T00:00:00Z"


def _opp(opp_id="opp_gh_octo_repo_201", revenue=80.0, title="Fix zero division error in auth client",
         updated_at="2026-10-01T00:00:00Z", state="open", language="python", labels=None,
         repo_active=True, description="Crash on empty input"):
    return {
        "opportunity_id": opp_id,
        "observed_revenue": revenue,
        "metadata": {
            "title": title,
            "state": state,
            "updated_at": updated_at,
            "language": language,
            "labels": labels if labels is not None else ["bug"],
            "repo_active_within_90d": repo_active
        },
        "description": description
    }


# 1. 满分候选：$80、3 天前、Fix 标题、Python -> CANDIDATE，四项全 True
def test_candidate_qualification_full_pass():
    res = OpportunityFilter.evaluate(_opp(), current_time_iso=CURRENT)

    assert res["status"] == "CANDIDATE"
    assert res["hard_filter"] == {
        "revenue_pass": True, "active_pass": True, "scope_pass": True, "stack_pass": True
    }
    assert res["discard_reasons"] == []
    feats = res["features"]
    assert feats["observed_revenue"] == 80.0
    assert feats["days_since_update"] == 3
    assert feats["language"] == "python"
    # 规则：按白名单顺序在 title+labels 全文取第一个命中词；样例带 "bug" 标签，
    # 白名单中 bug 先于 fix，故确定性产出 "bug"
    assert feats["scope_tag"] == "bug"


# 2. 低价/零价：DISCARDED + UNDERPRICED 归因
def test_underpriced_discard_with_reason():
    for rev in (20.0, 0.0):
        res = OpportunityFilter.evaluate(_opp(revenue=rev), current_time_iso=CURRENT)
        assert res["status"] == "DISCARDED"
        assert res["hard_filter"]["revenue_pass"] is False
        assert "UNDERPRICED" in res["discard_reasons"]


# 3. 过期 Issue / 仓库长期不活跃：INACTIVE 归因
def test_stale_issue_or_inactive_repo_discard():
    # 45 天未更新
    res = OpportunityFilter.evaluate(
        _opp(updated_at="2026-08-20T00:00:00Z"), current_time_iso=CURRENT)
    assert res["hard_filter"]["active_pass"] is False
    assert "INACTIVE" in res["discard_reasons"]
    assert res["status"] == "DISCARDED"

    # repo_active_within_90d 标记为 False
    res = OpportunityFilter.evaluate(
        _opp(repo_active=False), current_time_iso=CURRENT)
    assert res["hard_filter"]["active_pass"] is False
    assert "INACTIVE" in res["discard_reasons"]


# 4. 黑名单词（performance / refactor）阻断：SCOPE_BLACKLISTED 归因
def test_scope_blacklist_blocks_ambiguous_feature():
    for title in ("Improve database query performance", "Refactor router"):
        res = OpportunityFilter.evaluate(_opp(title=title, revenue=200.0), current_time_iso=CURRENT)
        assert res["hard_filter"]["scope_pass"] is False
        assert "SCOPE_BLACKLISTED" in res["discard_reasons"]
        assert res["status"] == "DISCARDED"


# 5. 不支持的语言（rust / go）：UNSUPPORTED_LANGUAGE 归因
def test_unsupported_language_discard():
    for lang in ("rust", "go"):
        res = OpportunityFilter.evaluate(_opp(language=lang), current_time_iso=CURRENT)
        assert res["hard_filter"]["stack_pass"] is False
        assert "UNSUPPORTED_LANGUAGE" in res["discard_reasons"]
        assert res["status"] == "DISCARDED"


# 6. 数据保留守恒：DISCARDED 也不丢字段、不崩溃
def test_data_retention_conservation():
    raw = _opp(title="Refactor router", revenue=200.0)
    res = OpportunityFilter.evaluate(raw, current_time_iso=CURRENT)
    assert res["status"] == "DISCARDED"
    assert res["raw_opportunity"] is raw  # 原始对象完整保留
    assert res["raw_opportunity"]["observed_revenue"] == 200.0
    assert res["features"]["observed_revenue"] == 200.0  # 特征抽取依然完整

    # 极端残缺输入（无 metadata）：不崩溃，产出系统级归因（4.4-C 裁决：
    # 缺必要结构归因 INVALID_SCHEMA，严禁归因为业务类 INACTIVE/SCOPE_UNCLEAR）
    weird = {"opportunity_id": "opp_weird_1", "observed_revenue": 999.0}
    res2 = OpportunityFilter.evaluate(weird, current_time_iso=CURRENT)
    assert res2["status"] == "DISCARDED"
    assert res2["discard_reasons"] == ["INVALID_SCHEMA"]
    assert res2["features"]["observed_revenue"] == 999.0

    # 批量入口：仅返回 CANDIDATE
    cands = OpportunityFilter.filter_candidates(
        [_opp(), _opp(opp_id="opp_low", revenue=10.0)], current_time_iso=CURRENT)
    assert len(cands) == 1
    assert cands[0]["opportunity_id"] == "opp_gh_octo_repo_201"


# 7. 全套回归零破损（Gate 1 / 4.2-C / 4.3-A / 4.4-A）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    rc3 = pytest.main([os.path.join(base, "test_settlement_flow.py"), "-q"])
    rc4 = pytest.main([os.path.join(base, "test_github_scanner.py"), "-q"])
    assert rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0
