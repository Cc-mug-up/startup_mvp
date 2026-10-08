# tests/test_opportunity_replay.py
# Gate 4.4-C 阶段二：回放数据集离线回测（端到端：raw issue -> adapter -> +enrichment -> filter）
import os
import json
import time
import pytest
from tools.github_adapter import GitHubAdapter
from tools.opportunity_filter import OpportunityFilter, VALID_DISCARD_REASONS

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "github_issues_replay.json")


def _load_samples():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["meta"], data["samples"]


META, SAMPLES = _load_samples()
BASELINE = META["baseline_time"]


def _evaluate_sample(s: dict) -> dict:
    """端到端回放：raw issue -> GitHubAdapter.parse_issue -> +enrichment -> OpportunityFilter.evaluate"""
    if s["input"].get("direct_opp") is not None:
        # 异常样本：上游数据已损坏，直接进入过滤器
        return OpportunityFilter.evaluate(s["input"]["direct_opp"], current_time_iso=BASELINE)

    issue = s["input"]["issue_json"]
    env = s["input"]["enrichment"]
    cleaned = GitHubAdapter.parse_issue(issue)
    assert cleaned["success"] is True, f"{s['case_id']} adapter parse failed"
    opp = cleaned["discovered_opportunities"][0]

    # 富集层合并（未来富集模块的契约）：labels 必须从原始 Issue 流入 metadata，
    # 富集层显式提供时以其为准（X-02 借此注入类型异常）
    opp["metadata"]["state"] = env.get("state")
    opp["metadata"]["updated_at"] = env.get("updated_at")
    opp["metadata"]["language"] = env.get("language")
    opp["metadata"]["repo_active_within_90d"] = env.get("repo_active_within_90d")
    opp["metadata"]["labels"] = env.get("labels", issue.get("labels") or [])
    opp["description"] = env.get("description", issue.get("body", ""))

    return OpportunityFilter.evaluate(opp, current_time_iso=BASELINE)


def _by_id(case_id: str) -> dict:
    return next(s for s in SAMPLES if s["case_id"] == case_id)


def _evaluate_by_id(case_id: str) -> dict:
    return _evaluate_sample(_by_id(case_id))


# 1. 高危样本危险误报率为零：T-A/T-B/T-C/T-D/T-G 严禁漏入 CANDIDATE
def test_dangerous_false_positive_rate_is_zero():
    dangerous = ["T-A", "T-B", "T-C", "T-D", "T-G"]
    fps = []
    for cid in dangerous:
        res = _evaluate_by_id(cid)
        if res["status"] == "CANDIDATE":
            fps.append(cid)
    assert fps == [], f"Dangerous false positives leaked into CANDIDATE: {fps}"


# 2. 召回率：P-01 ~ P-05 必须 5/5 全量通过（>= 95% 要求）
def test_candidate_recall_rate():
    positives = ["P-01", "P-02", "P-03", "P-04", "P-05"]
    passed = [cid for cid in positives if _evaluate_by_id(cid)["status"] == "CANDIDATE"]
    recall = len(passed) / len(positives)
    assert recall >= 0.95, f"Recall {recall:.0%} below threshold; failed: {set(positives) - set(passed)}"


# 3. 归因覆盖率：所有 DISCARDED 样本 reasons 非空且全部属于规范枚举
def test_discard_reason_coverage():
    valid_set = set(VALID_DISCARD_REASONS)
    for s in SAMPLES:
        res = _evaluate_sample(s)
        if s["expected"]["status"] == "DISCARDED":
            reasons = res["discard_reasons"]
            assert reasons, f"{s['case_id']} discarded with empty reasons"
            assert set(reasons) <= valid_set, f"{s['case_id']} illegal reason: {reasons}"
            assert set(reasons) == set(s["expected"]["discard_reasons"]), \
                f"{s['case_id']} reason mismatch: got {reasons}, expected {s['expected']['discard_reasons']}"
        else:
            assert res["status"] == "CANDIDATE", f"{s['case_id']} expected CANDIDATE"


# 4. 异常输入隔离：X-01 -> INVALID_SCHEMA，X-02 -> MALFORMED_INPUT 且 < 0.1s
def test_malformed_input_and_schema_isolation():
    x01 = _evaluate_by_id("X-01")
    assert x01["discard_reasons"] == ["INVALID_SCHEMA"]
    assert "INACTIVE" not in x01["discard_reasons"]  # 严禁业务类归因

    start = time.perf_counter()
    x02 = _evaluate_by_id("X-02")
    elapsed = time.perf_counter() - start
    assert x02["discard_reasons"] == ["MALFORMED_INPUT"]
    assert elapsed < 0.1, f"100KB dirty input took {elapsed:.3f}s (>0.1s)"


# 5. 全套回归零破损（Gate 1 / 4.2-C / 4.3-A / 4.4-A / 4.4-B）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    rc3 = pytest.main([os.path.join(base, "test_settlement_flow.py"), "-q"])
    rc4 = pytest.main([os.path.join(base, "test_github_scanner.py"), "-q"])
    rc5 = pytest.main([os.path.join(base, "test_opportunity_filter.py"), "-q"])
    assert rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 and rc5 == 0
