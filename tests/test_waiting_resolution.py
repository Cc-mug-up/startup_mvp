# tests/test_waiting_resolution.py
# Gate 4.2-C 确定性单测：现实世界事件适配器与解挂状态机（静态 Mock / 零网络）
import os
import json
import pytest
from controller import Controller
from tools.github_pr_poller import GitHubPRPoller

# 说明：控制器状态机的正常运行态为 "ACTIVE"（Gate 1/2 冻结产物），
# 规格断言中的 "恢复为 RUNNING" 即映射为该归一化正常态。


class PendingDispatcher:
    """轮询永远返回 PENDING 的外部桩（用于超时测试）。"""

    def check_pending_event(self, pending):
        return {"status": "PENDING"}


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


def _seed_waiting(ctrl, opp_id="opp_gh_c_1", timeout_ts="2026-10-02T00:00:00+00:00", ext_ref="pr_mock/repo_42"):
    ctrl.state["opportunity_memory"][opp_id] = {
        "source": "github_issue",
        "observed_revenue": 20.0,
        "accumulated_cost": 0.05,  # 沉没成本基线
        "internal_steps_spent": 4,
        "external_interactions": 1,
        "status": "ACTIVE"
    }
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW",
        "opportunity_id": opp_id,
        "external_reference": ext_ref,
        "timeout_timestamp": timeout_ts
    }
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()
    return opp_id


# 1. PR_MERGED：恢复 RUNNING（ACTIVE）-> DELIVERED，金额分毫不增，pending 清空并归档
def test_pr_merged_transitions_to_delivered_without_revenue(clean_controller):
    ctrl = clean_controller
    opp_id = _seed_waiting(ctrl)
    initial_revenue = ctrl.state["financials"]["total_revenue"]

    payload = {
        "state": "closed", "merged": True,
        "external_reference": "pr_mock/repo_42",
        "timestamp": "2026-10-01T06:00:00Z"
    }
    parsed = GitHubPRPoller.parse_poll_response(payload)
    # Poller 契约：纯事实提取，三字段齐备
    assert parsed == {"event_type": "PR_MERGED", "external_reference": "pr_mock/repo_42", "timestamp": "2026-10-01T06:00:00Z"}

    ctrl.resolve_pending_event(parsed)

    # a) 系统状态从 WAITING 恢复为 RUNNING（归一化为 ACTIVE）
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"
    # b) opportunity 流转为 DELIVERED
    assert ctrl.state["opportunity_memory"][opp_id]["status"] == "DELIVERED"
    assert ctrl.state["opportunity_memory"][opp_id]["payment_waiting"] is True
    # c) 【关键断言】金额分毫不增
    assert ctrl.state["financials"]["total_revenue"] == initial_revenue
    assert len(ctrl.state["transaction_ledger"]) == 0
    # d) pending 被正常清空并归档
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    archive = ctrl.state.get("event_archive", [])
    assert len(archive) == 1
    assert archive[0]["event"]["external_reference"] == "pr_mock/repo_42"
    assert archive[0]["resolution"]["event_type"] == "PR_MERGED"


# 2. PR_REJECTED：DEAD + reason: PR_REJECTED，数据不删除，沉没成本保留
def test_pr_rejected_transitions_to_dead_with_reason(clean_controller):
    ctrl = clean_controller
    opp_id = _seed_waiting(ctrl)

    parsed = GitHubPRPoller.parse_poll_response(
        {"state": "closed", "merged": False, "external_reference": "pr_mock/repo_42", "timestamp": "2026-10-01T06:00:00Z"}
    )
    assert parsed["event_type"] == "PR_REJECTED"

    ctrl.resolve_pending_event(parsed)

    opp = ctrl.state["opportunity_memory"][opp_id]
    # a) status 变为 DEAD
    assert opp["status"] == "DEAD"
    # b) 记录结构化归因 reason: PR_REJECTED
    assert opp["reason"] == "PR_REJECTED"
    # c) 数据不删除、沉没成本如实保留、系统恢复 RUNNING
    assert opp["accumulated_cost"] == 0.05
    assert opp["internal_steps_spent"] == 4
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert ctrl.state["financials"]["total_revenue"] == 0.0


# 3. 超时：打标 TIMEOUT（区分于 DEAD）+ reason，强制拔除 pending 解除休克
def test_waiting_timeout_distinguished_from_dead(clean_controller):
    ctrl = clean_controller
    opp_id = _seed_waiting(ctrl, timeout_ts="2026-09-30T00:00:00+00:00")  # 已过期

    ctrl.poll_waiting_event(PendingDispatcher())

    opp = ctrl.state["opportunity_memory"][opp_id]
    # a) status 变为 TIMEOUT（明确区分于被拒绝的 DEAD）
    assert opp["status"] == "TIMEOUT"
    assert opp["status"] != "DEAD"
    # b) 记录 reason: TIMEOUT
    assert opp["reason"] == "TIMEOUT"
    # c) pending 强制拔除，系统解除休克恢复 RUNNING
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"


# 4. 重复 MERGED 幂等：第 1 次流转，第 2/3 次 no-op 忽略，状态账本分毫不乱
def test_duplicate_merged_events_idempotency(clean_controller):
    ctrl = clean_controller
    opp_id = _seed_waiting(ctrl)

    payload = {
        "state": "closed", "merged": True,
        "external_reference": "pr_mock/repo_42",
        "timestamp": "2026-10-01T06:00:00Z"
    }
    parsed = GitHubPRPoller.parse_poll_response(payload)

    for i in range(3):
        ctrl.resolve_pending_event(parsed)  # 第 2/3 次必须被幂等忽略，且不报错

    # a) 第 1 次已触发流转
    assert ctrl.state["opportunity_memory"][opp_id]["status"] == "DELIVERED"
    # b) 第 2/3 次为 no-op：归档只有 1 条，状态无重复解挂
    assert len(ctrl.state.get("event_archive", [])) == 1
    # c) 状态与账本分毫不乱
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"
    assert ctrl.state["financials"]["total_revenue"] == 0.0
    assert len(ctrl.state["transaction_ledger"]) == 0

    # 过期/错配事件同样被幂等忽略
    stale = {"event_type": "PR_MERGED", "external_reference": "pr_other/repo_99", "timestamp": "2026-10-01T07:00:00Z"}
    ctrl.resolve_pending_event(stale)
    assert len(ctrl.state.get("event_archive", [])) == 1


# 5. Gate 1 回归：controller.py 修改后保持 7/7 零回归
def test_gate1_regression():
    rc = pytest.main([os.path.join(os.path.dirname(__file__), "test_security_bounds.py"), "-q"])
    assert rc == 0
