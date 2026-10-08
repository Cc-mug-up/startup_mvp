# tests/test_real_remote_execution.py
# Gate 4.5-B2 真实外部副作用验证与物理断路器系统（12 项硬指标 / 零真实写入）
import json
import os
import pytest
from controller import Controller
from tools.approval_gate import (ApprovalGate, UnauthorizedExternalWriteError,
                                 ArtifactTamperedError, ApprovalReplayError)
from tools.github_pr_issuer import GitHubPRIssuer
from tools.kill_switch import GlobalKillSwitch, KillSwitchActivatedError

REPO = "owner/test-repo"
BRANCH = "fix/b2-1"
BASE_SHA = "a1b2c3d4e5f6a7b8"
COMMIT_SHA = "c9d8e7f6a5b4c3d2"
TASK = "gh_issue_b2_1"
DIFF_OK = "--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
DIFF_TAMPERED = DIFF_OK.replace("x = 2", "x = 3")


class MockWriteAPI:
    def __init__(self):
        self.push_calls = 0
        self.pr_calls = 0

    def push(self, **kw):
        self.push_calls += 1
        return {"success": True, "ref": f"refs/heads/{kw['branch']}"}

    def create_pull_request(self, **kw):
        self.pr_calls += 1
        return {"number": 7, "html_url": f"https://github.com/{kw['repo']}/pull/7"}


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakePRTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def get(self, url, timeout=None):
        return FakeResp(200, self.payloads.pop(0))


@pytest.fixture
def clean_controller(tmp_path):
    state_file = tmp_path / "state.json"
    init_state = {
        "financials": {
            "initial_capital": 100.0, "current_cash": 100.0,
            "total_revenue": 0.0, "total_hard_cost": 0.0,
            "cost_breakdown": {"llm_decision": 0.0, "tool_execution": 0.0, "infrastructure": 0.0}
        },
        "runtime_guards": {
            "system_status": "ACTIVE", "current_time": "2026-10-01T00:00:00Z",
            "consecutive_internal_steps": 0, "consecutive_invalid_steps": 0,
            "max_internal_steps": 10, "max_invalid_steps": 3,
            "pending_external_event": None
        },
        "opportunity_memory": {},
        "transaction_ledger": [],
        "settled_event_refs": []
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(init_state, f)
    return Controller(state_path=str(state_file), log_path=str(tmp_path / "audit.jsonl"))


def _artifact(gate):
    return gate.freeze_artifact(
        task_id=TASK, source_issue="https://github.com/owner/test-repo/issues/1",
        target_repo=REPO, target_branch=BRANCH, base_sha=BASE_SHA, commit_sha=COMMIT_SHA,
        diff_content=DIFF_OK, model_id="mock-llm", token_cost=0.001, test_exit_code=0,
        tokens=100)


# ------------------------------------------------------------------ #
# B2.1-B2.4: 物理断路器瞬间阻断一切写
# ------------------------------------------------------------------ #
def test_kill_switch_instantly_blocks_all_writes():
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _artifact(gate)
    token = gate.issue_approval_token(artifact)

    # 指标1/2：审批已通过，但断路器激活 -> push 与 create_pr 均物理阻断
    GlobalKillSwitch.engage()
    try:
        with pytest.raises(KillSwitchActivatedError):
            issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                     DIFF_OK, push_api=api)
        with pytest.raises(KillSwitchActivatedError):
            issuer.create_pr_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                   DIFF_OK, "t", "b")
        # 指标3：0 远程写入
        assert api.push_calls == 0 and api.pr_calls == 0
    finally:
        GlobalKillSwitch.disengage()

    # 指标4：解除断路器后，同一审批令牌恢复正常写入能力
    r = issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_OK, push_api=api)
    assert r["success"] is True
    assert api.push_calls == 1


# ------------------------------------------------------------------ #
# B2.S1/S2: Anti-TOCTOU 与未授权在 B2 语境下绝无网络写调用
# ------------------------------------------------------------------ #
def test_anti_toctou_and_artifact_lock_enforced_in_b2():
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _artifact(gate)
    token = gate.issue_approval_token(artifact)

    # S1（指标5）：审批后篡改 -> ArtifactTamperedError，0 网络写
    with pytest.raises(ArtifactTamperedError):
        issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_TAMPERED, push_api=api)
    assert api.push_calls == 0 and api.pr_calls == 0

    # S2（指标6）：无令牌/未授权 -> UnauthorizedExternalWriteError，0 网络写
    with pytest.raises(UnauthorizedExternalWriteError):
        issuer.push_branch_gated(artifact, None, REPO, BRANCH, BASE_SHA, DIFF_OK, push_api=api)
    assert api.push_calls == 0 and api.pr_calls == 0

    # 指标7：消费保护——成功写入后同操作重放立即拦截。
    # 注意：S1 的篡改尝试已按设计作废了旧令牌，消费保护需用新签发的令牌验证
    token2 = gate.issue_approval_token(artifact)
    r = issuer.push_branch_gated(artifact, token2["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_OK, push_api=api)
    assert r["success"] is True
    with pytest.raises(ApprovalReplayError):
        issuer.push_branch_gated(artifact, token2["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_OK, push_api=api)
    assert api.push_calls == 1  # 真实写仅一次


# ------------------------------------------------------------------ #
# B2.5-B2.8: 外部 PR 状态双向对账
# ------------------------------------------------------------------ #
def test_reconciliation_tracks_pr_closed_or_merged(clean_controller):
    ctrl = clean_controller
    ctrl.state["opportunity_memory"][TASK] = {
        "source": "github_issue", "observed_revenue": 100.0, "accumulated_cost": 0.0,
        "internal_steps_spent": 0, "external_interactions": 1, "status": "ACTIVE"
    }

    # 指标8：远端 open -> 保持 WAITING/PR_OPEN
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": "pr_owner_test-repo_7", "timeout_timestamp": "2026-10-05T00:00:00+00:00"}
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()

    t = FakePRTransport([{"state": "open", "merged": False}])
    r = ctrl.poll_external_pr_status(TASK, "owner/test-repo", 7, transport=t)
    assert r["correction"] == "WAITING/PR_OPEN"
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"

    # 指标9：closed 且未合并 -> DEAD/PR_REJECTED
    t = FakePRTransport([{"state": "closed", "merged": False}])
    r = ctrl.poll_external_pr_status(TASK, "owner/test-repo", 7, transport=t)
    assert r["correction"] == "DEAD/PR_REJECTED"
    assert ctrl.state["opportunity_memory"][TASK]["status"] == "DEAD"
    assert ctrl.state["opportunity_memory"][TASK]["reason"] == "PR_REJECTED"
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"

    # 指标10：closed 且已合并 -> DELIVERED/PR_MERGED，零收入
    ctrl.state["opportunity_memory"][TASK]["status"] = "ACTIVE"
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": "pr_owner_test-repo_7", "timeout_timestamp": "2026-10-05T00:00:00+00:00"}
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()
    t = FakePRTransport([{"state": "closed", "merged": True}])
    r = ctrl.poll_external_pr_status(TASK, "owner/test-repo", 7, transport=t)
    assert r["correction"] == "DELIVERED/PR_MERGED"
    opp = ctrl.state["opportunity_memory"][TASK]
    assert opp["status"] == "DELIVERED"
    assert opp["payment_waiting"] is True
    assert ctrl.state["financials"]["total_revenue"] == 0.0  # 交付不等于结算

    # 指标11：对账证据归档
    archive = ctrl.state.get("event_archive", [])
    assert any(e["resolution"]["event_type"] == "EXTERNAL_PR_RECONCILE" for e in archive)


# ------------------------------------------------------------------ #
# 指标12: 全量回归（Gate 1 -> 4.5-B1）
# ------------------------------------------------------------------ #
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py", "test_human_approval_barrier.py",
              "test_targeted_opportunity_evaluator.py", "test_source_truth_calibration.py",
              "test_targeted_pipeline.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
