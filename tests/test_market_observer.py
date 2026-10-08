# tests/test_market_observer.py
# Gate 4.5-A0 确定性单测：真实市场勘探层（纯 Mock / 零网络）
import os
import pytest
from tools.market_observer import MarketObserver, OBSERVATION_FIELDS

NOW = "2026-10-04T00:00:00Z"


def _issue(num, title, body="", labels=None, updated="2026-10-01T00:00:00Z"):
    return {
        "number": num,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/octo/repo/issues/{num}",
        "repository_url": "https://api.github.com/repos/octo/repo",
        "labels": labels or [],
        "state": "open",
        "updated_at": updated
    }


def _meta(issue, language="python", repo_active=True):
    return {
        "issue_json": issue,
        "enrichment": {
            "state": issue.get("state", "open"),
            "updated_at": issue.get("updated_at"),
            "language": language,
            "repo_active_within_90d": repo_active
        }
    }


FIVE_INPUTS = [
    _meta(_issue(1, "Fix zero division error in auth client", labels=[{"name": "bounty: $80"}])),
    _meta(_issue(2, "Fix the login bug", body="Will pay $100 if you fix this fast")),
    _meta(_issue(3, "Fix bug in serializer", labels=[{"name": "bounty: $10"}])),
    _meta(_issue(4, "Fix bug in parser", labels=[{"name": "bounty: $60"}], updated="2026-07-31T00:00:00Z")),
    _meta(_issue(5, "Fix segfault in binding", labels=[{"name": "bounty: $90"}]), language="rust"),
]


# 1. 19 字段契约完整性：每条 observation 严格包含全部指定字段，无缺无多
def test_observer_schema_completeness():
    assert len(OBSERVATION_FIELDS) == 19
    for meta in FIVE_INPUTS:
        obs = MarketObserver.build_observation(meta, NOW)
        assert set(obs.keys()) == set(OBSERVATION_FIELDS), f"field mismatch: {obs.keys()}"


# 2. 赏金显式性分离：口头承诺 reward_explicit=False/reward=0；正规标签 True/100.0
def test_reward_explicit_separation():
    verbal = MarketObserver.build_observation(FIVE_INPUTS[1], NOW)
    assert verbal["reward"] == 0.0
    assert verbal["reward_explicit"] is False

    formal = _meta(_issue(6, "Fix a bug", labels=[{"name": "bounty: $100"}]))
    obs = MarketObserver.build_observation(formal, NOW)
    assert obs["reward"] == 100.0
    assert obs["reward_explicit"] is True


# 3. 密度计算正确性：10 条样本（2 条 CANDIDATE）density 严格等于 0.2
def test_density_calculation_correctness():
    observer = MarketObserver()
    candidates = [
        _meta(_issue(11, "Fix zero division error", labels=[{"name": "bounty: $80"}])),
        _meta(_issue(12, "Add unit tests for parser", labels=[{"name": "bounty: $120"}])),
    ]
    discards = [
        _meta(_issue(13, "Fix the login bug", body="Will pay $50 if fixed")),
        _meta(_issue(14, "Fix the login bug", body="please fix for free")),
        _meta(_issue(15, "Fix the login bug", body="Will donate $30")),
        _meta(_issue(16, "Fix bug", labels=[{"name": "bounty: $10"}])),
        _meta(_issue(17, "Fix bug in parser", labels=[{"name": "bounty: $60"}], updated="2026-07-31T00:00:00Z")),
        _meta(_issue(18, "Fix bug in cache", labels=[{"name": "bounty: $60"}], updated="2026-08-01T00:00:00Z")),
        _meta(_issue(19, "Fix binding crash", labels=[{"name": "bounty: $90"}]), language="rust"),
        _meta(_issue(20, "Fix ffi call", labels=[{"name": "bounty: $90"}]), language="go"),
    ]
    for meta in candidates + discards:
        observer.observations.append(MarketObserver.build_observation(meta, NOW))

    metrics = observer.calculate_market_metrics()
    assert metrics["total_raw_count"] == 10
    assert metrics["candidate_count"] == 2
    assert metrics["opportunity_density"] == 0.2  # 严格等于
    assert metrics["explicit_reward_count"] == 7  # 6 条带结构化标签 + 1 条 $10 标签
    dist = metrics["discard_reason_distribution"]
    assert dist["UNDERPRICED"] == 4   # 3 条口头承诺 + 1 条 $10
    assert dist["INACTIVE"] == 2
    assert dist["UNSUPPORTED_LANGUAGE"] == 2


# 4. 全套回归零破损（Gate 1 / 4.2-C / 4.3-A / 4.4-A / 4.4-B / 4.4-C）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    rc3 = pytest.main([os.path.join(base, "test_settlement_flow.py"), "-q"])
    rc4 = pytest.main([os.path.join(base, "test_github_scanner.py"), "-q"])
    rc5 = pytest.main([os.path.join(base, "test_opportunity_filter.py"), "-q"])
    rc6 = pytest.main([os.path.join(base, "test_opportunity_replay.py"), "-q"])
    assert rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 and rc5 == 0 and rc6 == 0
