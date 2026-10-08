# tests/test_human_approval_barrier.py
# Gate 4.5-A3 真实外部副作用硬门禁与不可变工件审批系统（10 项验收指标）
import json
import os
import shutil
import pytest
from controller import Controller
from sandbox_manager import start_sandbox, exec_in_sandbox, WORKSPACE_DIR
from tools.approval_gate import (ApprovalGate, UnauthorizedExternalWriteError,
                                 ArtifactTamperedError, DestinationMismatchError,
                                 ApprovalReplayError)
from tools.github_pr_issuer import GitHubPRIssuer
from tools.github_pr_poller import GitHubPRPoller

DIFF_OK = ("--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n"
           "-x = 1\n+x = 2\n")
DIFF_TAMPERED = DIFF_OK.replace("x = 2", "x = 3")  # 篡改 1 个字节
REPO = "owner/test-repo"
BRANCH = "fix/null-pointer-1024"
BASE_SHA = "a1b2c3d4e5f6a7b8"
COMMIT_SHA = "c9d8e7f6a5b4c3d2"
TASK = "gh_issue_1024"


class MockWriteAPI:
    def __init__(self):
        self.push_calls = 0
        self.pr_calls = 0
        self.pushes = []
        self.prs = []

    def push(self, repo, branch, commit_sha):
        self.push_calls += 1
        self.pushes.append({"repo": repo, "branch": branch, "commit_sha": commit_sha})
        return {"success": True, "ref": f"refs/heads/{branch}"}

    def create_pull_request(self, **kwargs):
        self.pr_calls += 1
        self.prs.append(kwargs)
        return {"number": 42, "html_url": f"https://github.com/{kwargs['repo']}/pull/42"}


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


def _seed_opp(ctrl, opp_id=TASK):
    ctrl.state["opportunity_memory"][opp_id] = {
        "source": "github_issue", "observed_revenue": 80.0, "accumulated_cost": 0.0,
        "internal_steps_spent": 0, "external_interactions": 0, "status": "ACTIVE"
    }
    ctrl.save_state()


def _make_artifact(gate, diff=DIFF_OK, task=TASK):
    return gate.freeze_artifact(
        task_id=task,
        source_issue="https://github.com/owner/repo/issues/1024",
        target_repo=REPO, target_branch=BRANCH,
        base_sha=BASE_SHA, commit_sha=COMMIT_SHA,
        diff_content=diff, model_id="mock-llm", token_cost=0.0012,
        test_exit_code=0,
        tokens=450, test_summary="1 passed in 0.05s"
    )


def _approve(gate, ctrl, artifact, task=TASK):
    ctrl.enter_approval_wait(task, artifact)
    token = gate.issue_approval_token(artifact)
    r = ctrl.apply_human_decision(task, "APPROVE", token)
    assert r["success"] is True
    return token


# ------------------------------------------------------------------ #
# 1. 未授权时外部写零调用
# ------------------------------------------------------------------ #
def test_a3_01_zero_external_writes_without_approval(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _make_artifact(gate)
    ctrl.enter_approval_wait(TASK, artifact)

    # 冻结期间 INTERNAL/EXTERNAL 动作被拦截
    valid, _ = ctrl.validate_action({"opportunity_id": TASK, "action_type": "EXTERNAL",
                                     "tool_name": "submit_pr", "tool_args": {}, "justification": "try"})
    assert not valid

    # 无令牌写调用：直接抛 UnauthorizedExternalWriteError，网络与 Git 零调用
    with pytest.raises(UnauthorizedExternalWriteError):
        issuer.push_branch_gated(artifact, None, REPO, BRANCH, BASE_SHA, DIFF_OK, push_api=api)
    with pytest.raises(UnauthorizedExternalWriteError):
        issuer.create_pr_gated(artifact, None, REPO, BRANCH, BASE_SHA, DIFF_OK, "t", "b")
    assert api.push_calls == 0 and api.pr_calls == 0


# ------------------------------------------------------------------ #
# 2. Anti-TOCTOU：审批后篡改 1 字节 -> ArtifactTamperedError，审批作废
# ------------------------------------------------------------------ #
def test_a3_02_toctou_tamper_detection_blocked(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _make_artifact(gate)
    token = _approve(gate, ctrl, artifact)

    with pytest.raises(ArtifactTamperedError):
        issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_TAMPERED, push_api=api)
    assert api.push_calls == 0
    assert gate.tokens[token["token_id"]]["revoked"] is True  # 审批作废

    # 状态回退 WAITING（重新挂起审批）
    ctrl.enter_approval_wait(TASK, artifact)
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["event_type"] == "PENDING_HUMAN_APPROVAL"


# ------------------------------------------------------------------ #
# 3. 目标锁：串库 / 串分支硬拦截
# ------------------------------------------------------------------ #
def test_a3_03_destination_mismatch_blocked(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _make_artifact(gate)
    token = _approve(gate, ctrl, artifact)

    with pytest.raises(DestinationMismatchError):
        issuer.push_branch_gated(artifact, token["token_id"], "owner/other-repo", BRANCH, BASE_SHA,
                                 DIFF_OK, push_api=api)
    with pytest.raises(DestinationMismatchError):
        issuer.push_branch_gated(artifact, token["token_id"], REPO, "release", BASE_SHA,
                                 DIFF_OK, push_api=api)
    with pytest.raises(DestinationMismatchError):
        issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, "deadbeef00000000",
                                 DIFF_OK, push_api=api)
    assert api.push_calls == 0


# ------------------------------------------------------------------ #
# 4. Scope Lock：10 文件补丁 -> 硬拒绝，DEAD/SCOPE_EXCEEDED
# ------------------------------------------------------------------ #
def test_a3_04_scope_lock_violation_blocked(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    wide_files = [{"path": f"f{i}.py", "added": 2, "deleted": 1} for i in range(10)]
    verdict = gate.check_scope(wide_files)
    assert verdict["ok"] is False and verdict["reason"] == "SCOPE_EXCEEDED"

    # 硬拒绝进入审批：任务直接 DEAD/SCOPE_EXCEEDED
    ctrl.mark_task_status(TASK, "DEAD", "SCOPE_EXCEEDED")
    opp = ctrl.state["opportunity_memory"][TASK]
    assert opp["status"] == "DEAD" and opp["reason"] == "SCOPE_EXCEEDED"
    assert TASK not in gate.artifacts  # 未生成任何审批卡


# ------------------------------------------------------------------ #
# 5. 重放防护：同一令牌第二次 push / pr 硬拦截
# ------------------------------------------------------------------ #
def test_a3_05_approval_token_replay_blocked(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _make_artifact(gate)
    token = _approve(gate, ctrl, artifact)

    r1 = issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                  DIFF_OK, push_api=api)
    assert r1["success"] is True
    with pytest.raises(ApprovalReplayError):
        issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                 DIFF_OK, push_api=api)

    r2 = issuer.create_pr_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                DIFF_OK, "Fix", "Body")
    assert r2["success"] is True
    with pytest.raises(ApprovalReplayError):
        issuer.create_pr_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                               DIFF_OK, "Fix2", "Body2")

    assert api.push_calls == 1 and api.pr_calls == 1  # 各操作仅一次真实写入


# ------------------------------------------------------------------ #
# 6. 人类 REJECT：任务 DEAD/REJECTED_BY_HUMAN，临时分支销毁，零外部写
# ------------------------------------------------------------------ #
def test_a3_06_human_rejection_cleans_safely(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    artifact = _make_artifact(gate)
    ctrl.enter_approval_wait(TASK, artifact)

    # 本地临时分支存在
    start_sandbox()
    repo = "scratch_reject"
    host = os.path.join(WORKSPACE_DIR, repo)
    shutil.rmtree(host, ignore_errors=True)
    os.makedirs(host)
    with open(os.path.join(host, "a.txt"), "w") as f:
        f.write("x")
    for root, dirs, files in os.walk(host):
        for d in dirs:
            os.chown(os.path.join(root, d), 1000, 1000)
        for fn in files:
            os.chown(os.path.join(root, fn), 1000, 1000)
    os.chown(host, 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{repo} && git init -q && git config user.email a@b.c && "
        f"git config user.name s && git add -A && git commit -qm init && git branch agent/tmp-1024")
    assert code == 0, f"{out}{err}"

    # REJECT 决议
    r = ctrl.apply_human_decision(TASK, "REJECT")
    assert r["success"] is True
    opp = ctrl.state["opportunity_memory"][TASK]
    assert opp["status"] == "DEAD"
    assert opp["reason"] == "REJECTED_BY_HUMAN"
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"

    # 临时分支销毁
    code, out, err = exec_in_sandbox(f"cd /workspace/{repo} && git branch -D agent/tmp-1024")
    assert code == 0
    code, out, err = exec_in_sandbox(f"cd /workspace/{repo} && git branch --list agent/tmp-1024")
    assert code == 0 and out.strip() == ""  # 分支已不存在

    assert api.push_calls == 0 and api.pr_calls == 0  # 零外部写留存


# ------------------------------------------------------------------ #
# 7. 正常审批：Push SHA 与 PR 内容哈希与审批卡 100% 吻合 -> WAITING (PR_PENDING)
# ------------------------------------------------------------------ #
def test_a3_07_valid_approval_executes_exact_artifact(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    api = MockWriteAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api, approval_gate=gate)
    artifact = _make_artifact(gate)

    # 审批卡内容齐备
    card = gate.render_approval_card(artifact)
    for marker in ("HUMAN APPROVAL CARD", "TASK ID", "DIFF HASH", "ARTIFACT HASH", "RISK CLASS"):
        assert marker in card

    token = _approve(gate, ctrl, artifact)

    push = issuer.push_branch_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                    DIFF_OK, push_api=api)
    assert push["success"] is True
    assert api.pushes[0]["commit_sha"] == artifact["commit_sha"]  # Push SHA 吻合
    assert api.pushes[0]["repo"] == REPO and api.pushes[0]["branch"] == BRANCH

    pr = issuer.create_pr_gated(artifact, token["token_id"], REPO, BRANCH, BASE_SHA,
                                DIFF_OK, "Fix null pointer", "Patch body")
    assert pr["success"] is True
    assert artifact["artifact_hash"] in api.prs[0]["body"]  # PR 内容哈希吻合

    # 状态安全推进 WAITING (PR_PENDING)
    tool_result = {
        "success": True, "hard_cost": 0.005,
        "pending_event": {"event_type": "PR_REVIEW", "opportunity_id": TASK,
                          "external_reference": pr["external_reference"],
                          "timeout_timestamp": "2026-10-05T00:00:00+00:00"}
    }
    action = {"opportunity_id": TASK, "action_type": "EXTERNAL", "tool_name": "submit_pr",
              "tool_args": {}, "justification": "Submit approved artifact"}
    ctrl.apply_action("act_a3_7", action, True, "VALID", tool_result, llm_decision_cost=0.001)
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["external_reference"] == pr["external_reference"]


# ------------------------------------------------------------------ #
# 8. 交付后外部反馈 -> 平稳流转 DELIVERED
# ------------------------------------------------------------------ #
def test_a3_08_reconciliation_feedback_after_delivery(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": "pr_owner_test-repo_42",
        "timeout_timestamp": "2026-10-05T00:00:00+00:00"
    }
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()

    parsed = GitHubPRPoller.parse_poll_response({
        "state": "closed", "merged": True,
        "external_reference": "pr_owner_test-repo_42",
        "timestamp": "2026-10-02T00:00:00Z"
    })
    ctrl.resolve_pending_event(parsed)

    opp = ctrl.state["opportunity_memory"][TASK]
    assert opp["status"] == "DELIVERED"
    assert opp["payment_waiting"] is True
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"
    assert ctrl.state["financials"]["total_revenue"] == 0.0  # 交付不等于结算


# ------------------------------------------------------------------ #
# 9. 全流程审计记录完整性
# ------------------------------------------------------------------ #
def test_a3_09_task_lifecycle_record_completeness(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl)
    gate = ApprovalGate()
    artifact = _make_artifact(gate)
    token = _approve(gate, ctrl, artifact)

    opp = ctrl.state["opportunity_memory"][TASK]
    lifecycle = {
        "issue_id": artifact["task_id"],
        "source_issue": artifact["source_issue"],
        "diff_hash": artifact["diff_hash"],
        "artifact_hash": artifact["artifact_hash"],
        "commit_sha": artifact["commit_sha"],
        "test_exit_code": artifact["test_exit_code"],
        "token_cost": artifact["token_cost"],
        "created_at": artifact["created_at"],
        "human_approval_at": opp["human_approval_at"],
        "approval_token_id": token["token_id"],
        "approval_authorized_at": token["authorized_at"]
    }
    for key, val in lifecycle.items():
        assert val not in (None, "", 0.0) or key in ("test_exit_code",), \
            f"Lifecycle field '{key}' missing: {val!r}"
    assert lifecycle["human_approval_at"] == lifecycle["approval_authorized_at"]


# ------------------------------------------------------------------ #
# 10. 全量回归（Gate 1 -> 4.5-A2）
# ------------------------------------------------------------------ #
def test_a3_10_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
