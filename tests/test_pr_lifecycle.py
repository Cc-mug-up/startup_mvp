# tests/test_pr_lifecycle.py
# Gate 4.2-B 确定性单测：PR 提交与 WAITING 状态机冻结闭环（Mock GitHub，零外部脏数据）
import json
import pytest
from controller import Controller
from sandbox_manager import start_sandbox, exec_in_sandbox
from tools.internal_tools import InternalTools
from tools.github_pr_issuer import GitHubPRIssuer


class MockGitHubAPI:
    """unittest.mock 隔离桩：模拟 GitHub REST 返回，计数真实请求次数。"""

    def __init__(self):
        self.calls = 0
        self._pr_number = 41

    def create_pull_request(self, **kwargs):
        self.calls += 1
        self._pr_number += 1
        return {
            "number": self._pr_number,
            "html_url": f"https://github.com/{kwargs['repo']}/pull/{self._pr_number}"
        }


class ResolveDispatcher:
    def __init__(self, revenue: float):
        self.revenue = revenue

    def check_pending_event(self, pending):
        return {"status": "RESOLVED", "verified_revenue": self.revenue}


@pytest.fixture
def clean_controller(tmp_path):
    state_file = tmp_path / "state.json"
    init_state = {
        "financials": {
            "initial_capital": 100.0,
            "current_cash": 100.0,
            "total_revenue": 0.0,
            "total_hard_cost": 0.0,
            "cost_breakdown": {"llm_decision": 0.0, "tool_execution": 0.0, "infrastructure": 0.0}
        },
        "runtime_guards": {
            "system_status": "ACTIVE",
            "current_time": "2026-10-01T00:00:00Z",
            "consecutive_internal_steps": 0,
            "consecutive_invalid_steps": 0,
            "max_internal_steps": 10,
            "max_invalid_steps": 3,
            "pending_external_event": None
        },
        "opportunity_memory": {},
        "transaction_ledger": [],
        "settled_event_refs": []
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(init_state, f)
    return Controller(state_path=str(state_file), log_path=str(tmp_path / "audit.jsonl"))


def _full_opp():
    return {
        "source": "github_issue",
        "observed_revenue": 0.0,
        "accumulated_cost": 0.0,
        "internal_steps_spent": 0,
        "external_interactions": 0,
        "status": "ACTIVE"
    }


def _drive_to_waiting(ctrl, issuer, opp_id):
    """任务从沙箱拿到 commit_sha -> Issuer 生成 PR -> 控制器登记 pending 并冻结。"""
    ctrl.state["opportunity_memory"][opp_id] = _full_opp()
    pr = issuer.create_pr(opp_id, "mock/repo", "agent/fix", "Fix bug", "Patch body", "a" * 40)
    assert pr["success"] is True
    tool_result = {
        "success": True,
        "hard_cost": 0.005,
        "pending_event": {
            "event_type": "PR_REVIEW",
            "opportunity_id": opp_id,
            "external_reference": pr["external_reference"],
            "timeout_timestamp": "2026-10-03T00:00:00+00:00"
        }
    }
    action = {
        "opportunity_id": opp_id,
        "action_type": "EXTERNAL",
        "tool_name": "submit_pr",
        "tool_args": {},
        "justification": "Submit fix PR"
    }
    ctrl.apply_action("act_submit", action, True, "VALID", tool_result, llm_decision_cost=0.001)
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["opportunity_id"] == opp_id
    return pr


# 1. 凭证隔离穿透测试：Token 不存在于沙箱环境变量、执行上下文与 /workspace 挂载
def test_token_strictly_isolated_from_sandbox():
    start_sandbox()
    token = "ghp_SUPER_SECRET_TOKEN_12345"
    GitHubPRIssuer(github_token=token, api_client=MockGitHubAPI())

    # 容器环境变量（env 输出回宿主比对，Token 本身不进容器命令）
    res = InternalTools.shell("env")
    assert res["success"] is True
    assert token not in res["output"]

    # 执行上下文（再次独立取证）
    code, out, err = exec_in_sandbox("env")
    assert code == 0 and token not in out

    # /workspace 挂载目录全文检索（用片段检索，避免完整凭证进入容器命令行）
    code, out, err = exec_in_sandbox("! grep -rq 'SUPER_SECRET_TOKEN_12345' /workspace")
    assert code == 0, f"Token fragment leaked into /workspace: {out}{err}"


# 2. 状态机强制休克：PR 生成后冻结为 WAITING，INTERNAL/EXTERNAL 一律拦截
def test_pr_creation_freezes_into_waiting(clean_controller):
    ctrl = clean_controller
    issuer = GitHubPRIssuer("ghp_TEST_TOKEN", MockGitHubAPI())
    pr = _drive_to_waiting(ctrl, issuer, "opp_gh_a_1")

    internal_action = {
        "opportunity_id": "opp_gh_a_1",
        "action_type": "INTERNAL",
        "tool_name": "write_file",
        "tool_args": {},
        "justification": "Keep coding while waiting"
    }
    valid, reason = ctrl.validate_action(internal_action)
    assert not valid and "WAITING" in reason

    external_action = {
        "opportunity_id": "opp_gh_a_1",
        "action_type": "EXTERNAL",
        "tool_name": "submit_pr",
        "tool_args": {},
        "justification": "Resubmit PR"
    }
    valid, reason = ctrl.validate_action(external_action)
    assert not valid and "WAITING" in reason

    # 仅 WAIT 轮询合法
    wait_action = {
        "opportunity_id": "opp_gh_a_1",
        "action_type": "WAIT",
        "tool_name": "none",
        "tool_args": {},
        "justification": "Poll external event"
    }
    valid, _ = ctrl.validate_action(wait_action)
    assert valid

    # 冻结期间挂起事件不被扰动
    assert ctrl.state["runtime_guards"]["pending_external_event"]["external_reference"] == pr["external_reference"]


# 3. 幂等性防刷：同一机会重复提交被拒，平台零重复请求
def test_duplicate_pr_submission_rejected():
    api = MockGitHubAPI()
    issuer = GitHubPRIssuer("ghp_TEST_TOKEN", api)

    r1 = issuer.create_pr("opp_dup_1", "mock/repo", "agent/fix", "Fix", "Body", "a" * 40)
    assert r1["success"] is True
    assert r1["external_reference"] == "pr_mock/repo_42"
    assert r1["hard_cost"] == 0.0 and r1["commit_sha"] == "a" * 40
    assert api.calls == 1

    r2 = issuer.create_pr("opp_dup_1", "mock/repo", "agent/fix", "Fix2", "Body2", "b" * 40)
    assert r2["success"] is False
    assert "Duplicate" in r2["error"]
    assert r2["external_reference"] == r1["external_reference"]
    assert api.calls == 1  # 平台未收到第二次请求

    # 不同机会不受幂等表影响
    r3 = issuer.create_pr("opp_other_2", "mock/repo", "agent/fix", "Fix3", "Body3", "c" * 40)
    assert r3["success"] is True
    assert api.calls == 2


# 4. 外部引用强绑定：external_reference 与原 opportunity 严格成对入账，不可错配
def test_pr_tied_to_correct_opportunity(clean_controller):
    ctrl = clean_controller
    issuer = GitHubPRIssuer("ghp_TEST_TOKEN", MockGitHubAPI())

    pr_a = _drive_to_waiting(ctrl, issuer, "opp_gh_a_7")
    ctrl.poll_waiting_event(ResolveDispatcher(15.0))

    pr_b = _drive_to_waiting(ctrl, issuer, "opp_gh_b_8")
    ctrl.poll_waiting_event(ResolveDispatcher(30.0))

    ledger = ctrl.state["transaction_ledger"]
    assert len(ledger) == 2
    assert ledger[0]["opportunity_id"] == "opp_gh_a_7"
    assert ledger[0]["external_ref"] == pr_a["external_reference"]
    assert ledger[0]["amount"] == 15.0
    assert ledger[1]["opportunity_id"] == "opp_gh_b_8"
    assert ledger[1]["external_ref"] == pr_b["external_reference"]
    assert ledger[1]["amount"] == 30.0
    assert pr_a["external_reference"] != pr_b["external_reference"]
    assert ctrl.state["financials"]["total_revenue"] == 45.0
