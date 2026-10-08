# tests/test_targeted_opportunity_evaluator.py
# Gate 4.5-B0 多源机会漏斗评估器确定性单测（纯 Mock / 零网络）
import os
import pytest
from tools.market_funnel_evaluator import MarketFunnelEvaluator
from tools.opportunity_sources.base_source import OpportunitySource
from tools.opportunity_sources.algora_source import AlgoraSource

NOW = "2026-10-04T00:00:00Z"


def _opp(sid, reward, verified=True, title="Fix zero division bug in parser",
         language="python", last_activity="2026-10-02T00:00:00Z",
         claim_count=0, open_pr_count=0, body="", labels=None):
    return {
        "source": "algora", "source_id": f"bnt_{sid}",
        "repo": "acme/repo", "issue_id": str(sid),
        "issue_url": f"https://github.com/acme/repo/issues/{sid}",
        "title": title, "body": body,
        "reward_amount": reward, "reward_currency": "USD",
        "reward_verified": verified,
        "bounty_created_at": "2026-09-15T00:00:00Z",
        "last_activity_at": last_activity,
        "claim_count": claim_count, "open_pr_count": open_pr_count,
        "language": language, "labels": labels or []
    }


# 1. 四层漏斗数学正确性：10 条梯度样本，严格单调递减/守恒，比例精确，无零除异常
def test_funnel_densities_mathematical_soundness():
    samples = [
        _opp(1, 0.0, verified=False),                                   # L1 掉：无背书
        _opp(2, 20.0),                                                  # L1 掉：低价
        _opp(3, 200.0, title="Rewrite whole architecture of the app"),  # L2 掉：重构黑名单
        _opp(4, 100.0, language="rust"),                                # L2 掉：非 python
        _opp(5, 150.0, body="This requires GPU cluster to run tests"),  # L3 掉：静态预检
        _opp(6, 120.0, last_activity="2024-09-30T00:00:00Z"),           # L4 掉：极端化石且零互动
        _opp(7, 100.0, claim_count=33),                                 # 保留：SATURATED（4.5-B0.1 裁决）
        _opp(8, 90.0, open_pr_count=5),                                 # 保留：竞标拥挤不再是硬丢弃
        _opp(9, 80.0, claim_count=1),                                   # 通过：LOW
        _opp(10, 60.0, claim_count=0),                                  # 通过：LOW
    ]
    m = MarketFunnelEvaluator.evaluate(samples, current_time_iso=NOW)

    # 严格单调递减或守恒
    counts = [m["total_raw"], m["reward_verified_count"], m["rule_qualified_count"],
              m["preflight_qualified_count"], m["economically_plausible_count"]]
    assert counts == [10, 8, 6, 5, 4]
    assert all(a >= b for a, b in zip(counts, counts[1:]))

    # 比例精确，无零除异常
    d = m["funnel_densities"]
    assert d["d_reward"] == pytest.approx(8 / 10)
    assert d["d_qualified"] == pytest.approx(6 / 8)
    assert d["d_preflight"] == pytest.approx(5 / 6)
    assert d["d_economic"] == pytest.approx(4 / 5)
    assert m["stale_bounties_dropped"] == 1
    assert m["high_competition_dropped"] == 0  # 竞争度已改为 EV 特征，不再硬丢弃

    # 空输入零除安全
    empty = MarketFunnelEvaluator.evaluate([], current_time_iso=NOW)
    assert empty["funnel_densities"] == {"d_reward": 0.0, "d_qualified": 0.0,
                                         "d_preflight": 0.0, "d_economic": 0.0}


# 2. 新鲜度：$200 但 2 年无活动 -> 通过 L1/L2/L3，在 L4 被化石拦截
def test_freshness_filter_drops_fossil_bounties():
    fossil = _opp(21, 200.0, last_activity="2024-08-01T00:00:00Z")
    m = MarketFunnelEvaluator.evaluate([fossil], current_time_iso=NOW)
    assert m["reward_verified_count"] == 1
    assert m["rule_qualified_count"] == 1
    assert m["preflight_qualified_count"] == 1
    assert m["economically_plausible_count"] == 0
    assert m["stale_bounties_dropped"] == 1


# 3. 竞争度（4.5-B0.1 裁决）：$100 且 claim_count=33 -> 不丢弃，标 SATURATED EV 特征
def test_competition_filter_drops_crowded_bounties():
    crowded = _opp(22, 100.0, claim_count=33)
    m = MarketFunnelEvaluator.evaluate([crowded], current_time_iso=NOW)
    assert m["reward_verified_count"] == 1
    assert m["preflight_qualified_count"] == 1
    assert m["economically_plausible_count"] == 1  # 不再硬丢弃
    assert m["economically_plausible"][0]["competition_level"] == "SATURATED"
    assert m["high_competition_dropped"] == 0


# 4. Algora 结构归一化：完整提取金额/认领人数/时间戳，契约 16 字段齐全
def test_algora_schema_normalization():
    source = AlgoraSource()
    raw = {
        "id": "bnt_abc123",
        "title": "Fix null pointer in auth client",
        "body": "Crash on empty input",
        "repo": {"name": "acme/auth-client", "url": "https://github.com/acme/auth-client"},
        "issue": {"number": 88, "url": "https://github.com/acme/auth-client/issues/88"},
        "reward": {"amount_in_cents": 10000, "currency": "USD"},
        "created_at": "2026-09-15T08:00:00Z",
        "last_activity_at": "2026-09-30T12:00:00Z",
        "claimers": [{"user": "alice"}, {"user": "bob"}],
        "pull_requests": [{"url": "https://github.com/acme/auth-client/pull/7"}],
        "language": "python",
        "labels": ["bug", "bounty"]
    }
    opp = source.normalize(raw)

    assert OpportunitySource.validate(opp) is True  # 16 字段契约齐全
    assert opp["source"] == "algora"
    assert opp["source_id"] == "bnt_abc123"
    assert opp["reward_amount"] == 100.0          # 10000 分 -> $100
    assert opp["reward_currency"] == "USD"
    assert opp["reward_verified"] is True         # 平台官方托管背书
    assert opp["claim_count"] == 2                # 认领人数精确
    assert opp["open_pr_count"] == 1
    assert opp["bounty_created_at"] == "2026-09-15T08:00:00Z"
    assert opp["last_activity_at"] == "2026-09-30T12:00:00Z"
    assert opp["issue_id"] == "88"
    assert opp["language"] == "python"
    assert opp["labels"] == ["bug", "bounty"]


# 5. 全量回归（Gate 1 -> 4.5-A3）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py", "test_human_approval_barrier.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
