# tests/test_source_truth_calibration.py
# Gate 4.5-B0.1 已知正样本校准（Source Truth Calibration / 零写操作）
import os
import pytest
from tools.market_funnel_evaluator import MarketFunnelEvaluator
from tools.opportunity_sources.base_source import OpportunitySource
from tools.opportunity_sources.algora_source import AlgoraSource

NOW = "2026-10-04T00:00:00Z"

# 与真实公开页结构一致的 Ground Truth 契约（projectdiscovery：
# nuclei#6674 $100/33 claims、nuclei#6532 $100/21 claims）
TRUTH_HTML = """
<html><body><table>
<tr class="bg-white border-b">
  <td>
    <div class="font-extrabold text-emerald-300 hover:text-emerald-200">$100</div>
    <a href="https://github.com/projectdiscovery/nuclei/issues/6674">
      <p class="text-sm font-medium text-gray-300">nuclei#6674</p>
      <p class="text-base font-medium leading-tight text-gray-100 group-hover/issue:text-white">Replace panic with error handling in template loader when dialers are missing</p>
    </a>
    <div class="whitespace-nowrap text-sm font-medium text-gray-300">33 claims</div>
  </td>
</tr>
<tr class="bg-white border-b">
  <td>
    <div class="font-extrabold text-emerald-300 hover:text-emerald-200">$100</div>
    <a href="https://github.com/projectdiscovery/nuclei/issues/6532">
      <p class="text-sm font-medium text-gray-300">nuclei#6532</p>
      <p class="text-base font-medium leading-tight text-gray-100 group-hover/issue:text-white">Integrate typos tool into CI</p>
    </a>
    <div class="whitespace-nowrap text-sm font-medium text-gray-300">21 claims</div>
  </td>
</tr>
</table></body></html>
"""

EMPTY_HTML = "<html><body><table></table></body></html>"


class FakeResp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return {}


class FakeTransport:
    def __init__(self, html):
        self.html = html
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        return FakeResp(text=self.html, status_code=200)


# 1. 校准适配器：真实契约结构 -> 2 条规范化数据，health OK，金额/认领数精确
def test_calibrated_adapter_parses_projectdiscovery_truth():
    source = AlgoraSource(transport=FakeTransport(TRUTH_HTML))
    r = source.fetch_bounties("projectdiscovery", expected_min=2)

    assert r["success"] is True
    assert r["source_health"] == "SOURCE_OK"
    assert r["channel"] == "html"
    assert len(r["items"]) == 2

    opps = [source.normalize(raw) for raw in r["items"]]
    assert all(OpportunitySource.validate(o) for o in opps)

    by_issue = {o["issue_id"]: o for o in opps}
    assert set(by_issue) == {"6674", "6532"}
    assert by_issue["6674"]["reward_amount"] == 100.0
    assert by_issue["6674"]["claim_count"] == 33
    assert by_issue["6532"]["reward_amount"] == 100.0
    assert by_issue["6532"]["claim_count"] == 21
    assert by_issue["6674"]["repo"] == "projectdiscovery/nuclei"
    assert by_issue["6674"]["reward_verified"] is True


# 2. 33 claims 不再被硬丢弃：保留候选池，competition_level == SATURATED
def test_competition_level_classification_not_discarded():
    opp = {
        "source": "algora", "source_id": "bnt_x", "repo": "projectdiscovery/nuclei",
        "issue_id": "6674", "issue_url": "https://github.com/projectdiscovery/nuclei/issues/6674",
        "title": "Replace panic with error handling in template loader",
        "body": "", "reward_amount": 100.0, "reward_currency": "USD",
        "reward_verified": True,
        "bounty_created_at": "2026-09-01T00:00:00Z",
        "last_activity_at": "2026-10-02T00:00:00Z",
        "claim_count": 33, "open_pr_count": 0, "language": "python", "labels": []
    }
    m = MarketFunnelEvaluator.evaluate([opp], current_time_iso=NOW)
    assert m["economically_plausible_count"] == 1  # 不被硬丢弃
    assert m["economically_plausible"][0]["competition_level"] == "SATURATED"
    assert m["high_competition_dropped"] == 0


# 3. 已知存在数据但通道返回 0 条 -> SOURCE_INCONSISTENT + 告警截断，不产出错误 Density=0
def test_source_health_inconsistent_detection():
    source = AlgoraSource(transport=FakeTransport(EMPTY_HTML))
    r = source.fetch_bounties("projectdiscovery", expected_min=2)
    assert r["source_health"] == "SOURCE_INCONSISTENT"

    mkt = source.fetch_market(orgs=["projectdiscovery"], expected_min={"projectdiscovery": 2})
    assert mkt["source_health"] == "SOURCE_INCONSISTENT"
    assert mkt["evaluable"] is False
    assert mkt["alert"] is not None

    # 截断守护：evaluable=False 时严禁调用漏斗评估（模拟主控守卫）
    funnel_result = None
    if mkt["evaluable"]:
        funnel_result = MarketFunnelEvaluator.evaluate(mkt["raw_items"])
    assert funnel_result is None  # 未产出错误的 Density=0


# 4. 全量回归（Gate 1 -> 4.5-A3 + B0 校准后语义）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py", "test_human_approval_barrier.py",
              "test_targeted_opportunity_evaluator.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
