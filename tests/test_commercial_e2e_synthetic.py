# tests/test_commercial_e2e_synthetic.py
# Commercial Pilot Phase 0：合成正向全链验证（纯本地 / 沙箱真实执行 / 零网络写）
import json
import os

import pytest

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.commercial_evaluator import (CommercialEvaluator, competition_signal,
                                        financial_truth, settlement_evidence,
                                        FUNDED_VERIFIED)
from tools.commercial_trace import CommercialTrace
from tools.internal_tools import InternalTools

REPO = "phase0_synth_repo"
CALC_BASE = "def add(a, b):\n    return a - b\n"  # 有意缺陷
TEST_BASE = ("from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
PATCH = ("--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n"
         " def add(a, b):\n-    return a - b\n+    return a + b\n")


def _synthetic_opportunity():
    """标准合格合成样本：FUNDED_VERIFIED $20 / Python / ACTIVE / 零 PR。"""
    return {
        "opportunity_id": "opp_synth_phase0_1",
        "title": "Fix add function sign error",
        "description": "calculator.py add returns wrong results.",
        "language": "python",
        "financial_truth": financial_truth(
            FUNDED_VERIFIED, "algora_escrow", "https://algora.io/bounties/synth-1",
            "platform_escrow_api"),
        "competition_signal": competition_signal(claims_count=1, open_pr_count=0),
        "maintainer_signal": {"last_commit_days": 3, "recent_issue_activity_days": 1,
                              "is_repo_archived": False}
    }


# ---------- 契约扩展 ----------
def test_maintainer_signal_admission_rules():
    ev = CommercialEvaluator()
    opp = _synthetic_opportunity()
    assert ev.evaluate(opp)["state"] == "QUALIFIED"

    dormant = dict(opp)
    dormant["maintainer_signal"] = {"last_commit_days": 45, "recent_issue_activity_days": 2,
                                    "is_repo_archived": False}
    r = ev.evaluate(dormant)
    assert r["state"] == "UNVERIFIED"
    assert "maintainer_dormant" in r["missing_evidence"]

    archived = dict(opp)
    archived["maintainer_signal"] = {"last_commit_days": 3, "recent_issue_activity_days": 2,
                                     "is_repo_archived": True}
    assert "maintainer_dormant" in ev.evaluate(archived)["missing_evidence"]


def test_settlement_evidence_contract():
    se = settlement_evidence("stripe_pi_888", 20.0, "2026-10-02T10:00:00Z",
                             "https://stripe.com/payments/888")
    assert se["amount_usd"] == 20.0
    with pytest.raises(AssertionError):
        settlement_evidence("", 20.0, "ts", "url")
    with pytest.raises(AssertionError):
        settlement_evidence("ref", -5.0, "ts", "url")


# ---------- 端到端合成闭环 ----------
@pytest.fixture(scope="module")
def synth_repo():
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    import shutil
    shutil.rmtree(os.path.join(WORKSPACE_DIR, REPO), ignore_errors=True)
    code, out, err = exec_in_sandbox(
        f"cd /workspace && mkdir {REPO} && cd {REPO} && git init -q && "
        f"git config user.email a@b.c && git config user.name s", timeout=120)
    assert code == 0, err or out
    for f, c in (("calculator.py", CALC_BASE), ("test_calculator.py", TEST_BASE)):
        wr = InternalTools.write_file(f"{REPO}/{f}", c)
        assert wr["success"]
        os.chown(os.path.join(WORKSPACE_DIR, REPO, f), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{REPO} && git add -A && git commit -qm base", timeout=120)
    assert code == 0, err or out
    yield REPO
    stop_sandbox()


def test_phase0_synthetic_full_chain(synth_repo, tmp_path):
    # 1. 商业评估：QUALIFIED
    opp = _synthetic_opportunity()
    verdict = CommercialEvaluator().evaluate(opp)
    assert verdict["state"] == "QUALIFIED", verdict

    # 2. 桥接：Opportunity -> Gate 4.5 Task 对象（接口契约）
    task = {
        "task_id": opp["opportunity_id"],
        "repo_dir": synth_repo,
        "patch_content": PATCH,
        "test_cmd": "python3 -m pytest",
        "budget_usd": 1.0,
        "source_opportunity": opp
    }
    assert task["task_id"] == opp["opportunity_id"]
    assert task["source_opportunity"]["financial_truth"]["funding_status"] == FUNDED_VERIFIED

    # 3. Gate 4.5 单 Pod 本地执行：真实补丁 + 真实 pytest + 真实提交
    wr = InternalTools.write_file(f"{synth_repo}/.agent_patch.diff", PATCH)
    assert wr["success"]
    os.chown(os.path.join(WORKSPACE_DIR, synth_repo, ".agent_patch.diff"), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{synth_repo} && git apply .agent_patch.diff && rm .agent_patch.diff "
        f"&& rm -rf __pycache__ && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Synthetic fix' && git rev-parse HEAD", timeout=180)
    assert code == 0, f"execution failed: {err or out[:300]}"
    commit_sha = out.strip().splitlines()[-1]
    artifact = {"commit_sha": commit_sha, "tests_passed": True,
                "changed_files": ["calculator.py"]}
    assert len(commit_sha) == 40 and artifact["tests_passed"] is True

    # 4. 商业审计轨迹：t0 -> t1 -> t3 -> t4 -> t6 -> t7 + 结算证据
    trace = CommercialTrace(opp["opportunity_id"])
    # 严格单调序：全八阶段标记（规格列出的 t0/t1/t3/t4/t6/t7 为关键阶段，
    # t2/t5 为中间审计锚点）
    ts = ["2026-10-02T10:00:00Z", "2026-10-02T10:05:00Z", "2026-10-02T10:07:00Z",
          "2026-10-02T10:10:00Z", "2026-10-02T10:15:00Z", "2026-10-02T10:30:00Z",
          "2026-10-02T11:00:00Z"]
    for stage, tstamp in zip(
            ["t0_discovered", "t1_human_qualified", "t2_reviewed", "t3_work_started",
             "t4_pr_submitted", "t5_delivery_verified", "t6_pr_merged"], ts):
        trace.mark(stage, ts=tstamp)
    trace.record_cost("llm_tokens", 2.0, "synthetic analysis")
    trace.record_cost("sandbox_time", 3.0, "delivery")
    se = settlement_evidence("stripe_pi_888", 20.0, "2026-10-02T11:05:00Z",
                             "https://stripe.com/payments/888")
    trace.confirm_settlement(se["amount_usd"], settled_at=se["settled_at"])
    trace.settlement_evidence = se
    trace.artifact = artifact

    # 5. 指标断言：TTC 单调递增 + 经济净值正确
    m = trace.metrics()
    assert m["ttc_seconds"] == 3900.0  # 11:05 - 10:00
    assert m["revenue_usd"] == 20.0
    assert m["total_cost_usd"] == 5.0
    assert m["net_economic_value_usd"] == 15.0
    assert m["roi"] == 3.0
    deltas = trace.stage_elapsed()
    assert all(v >= 0 for v in deltas.values())  # TTC 单调递增

    # 6. 审计报告快照
    snapshot = trace.to_dict()
    snapshot["settlement_evidence"] = se
    snapshot["artifact"] = artifact
    out_path = tmp_path / "phase0_trace.json"
    out_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    print("==================== [ PHASE 0 SYNTHETIC TRACE AUDIT REPORT ] ====================")
    print(f"interface_contract_ok=True (Opportunity->Task bridge, settlement_evidence 4-field)")
    print(f"artifact: commit={commit_sha} tests_passed={artifact['tests_passed']}")
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    print("==================================================================================")
