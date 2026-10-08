# tests/test_security_bounds.py
import pytest
import json
from controller import Controller

@pytest.fixture
def clean_controller(tmp_path):
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "audit.jsonl"
    init_state = {
        "financials": {
            "initial_capital": 100.0,
            "current_cash": 100.0,
            "total_revenue": 0.0,
            "total_hard_cost": 0.0,
            "cost_breakdown": {
                "llm_decision": 0.0,
                "tool_execution": 0.0,
                "infrastructure": 0.0
            }
        },
        "runtime_guards": {
            "system_status": "ACTIVE",
            "current_time": "2026-10-01T00:00:00Z",
            "consecutive_internal_steps": 0,
            "consecutive_invalid_steps": 0,
            "max_internal_steps": 10,
            "max_invalid_steps": 3,
            "pending_external_event": None  # 正确的 Python None
        },
        "opportunity_memory": {},
        "transaction_ledger": [],
        "settled_event_refs": []
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(init_state, f)
    return Controller(state_path=str(state_file), log_path=str(log_file))

# 1. 验证模型无法凭空定义收入 (叙事 != 事实)
def test_model_cannot_create_revenue(clean_controller):
    ctrl = clean_controller
    tool_res = {"hard_cost": 0.0, "observed_revenue": 0.0}
    action = {
        "opportunity_id": "opp_test",
        "action_type": "EXTERNAL",
        "tool_name": "send_email",
        "tool_args": {},
        "justification": "Customer paid me $500 cash in hand"
    }
    # 种子机会记录必须与控制器发现路径产出的完整结构一致
    ctrl.state["opportunity_memory"]["opp_test"] = {
        "source": "test_seed",
        "observed_revenue": 0.0,
        "accumulated_cost": 0.0,
        "internal_steps_spent": 0,
        "external_interactions": 0,
        "status": "ACTIVE"
    }
    ctrl.apply_action("act_01", action, True, "VALID", tool_res, llm_decision_cost=0.01)

    assert ctrl.state["financials"]["total_revenue"] == 0.0
    assert len(ctrl.state["transaction_ledger"]) == 0

# 2. 验证工具是产生真实收入的唯一途径
def test_tool_is_only_source_of_revenue(clean_controller):
    ctrl = clean_controller
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "opportunity_id": "opp_101",
        "event_type": "PAYMENT_CONFIRMATION",
        "external_reference": "stripe_pi_999",
        "timeout_timestamp": "2026-10-02T00:00:00Z"
    }

    class MockDispatcherResolved:
        def check_pending_event(self, pending):
            return {"status": "RESOLVED", "verified_revenue": 15.0}

    ctrl.poll_waiting_event(MockDispatcherResolved())

    assert ctrl.state["financials"]["total_revenue"] == 15.0
    assert ctrl.state["financials"]["current_cash"] == 115.0
    assert len(ctrl.state["transaction_ledger"]) == 1

# 3. 验证模型不能自造未登记的 opportunity_id
def test_agent_cannot_invent_opportunity_id(clean_controller):
    ctrl = clean_controller
    action = {
        "opportunity_id": "opp_hallucinated_by_llm",
        "action_type": "INTERNAL",
        "tool_name": "write_file",
        "tool_args": {},
        "justification": "Trying to bypass discovery"
    }
    valid, reason = ctrl.validate_action(action)
    assert not valid
    assert "does not exist" in reason

# 4. 验证非法操作不递增内部步数，但真实扣除推理费
def test_invalid_action_cost_accounting(clean_controller):
    ctrl = clean_controller
    bad_action = {"action_type": "INVALID_SCHEMA"}

    ctrl.apply_action("act_bad", bad_action, False, "Schema Error", None, llm_decision_cost=0.05)

    assert ctrl.state["runtime_guards"]["consecutive_internal_steps"] == 0
    assert ctrl.state["runtime_guards"]["consecutive_invalid_steps"] == 1
    assert ctrl.state["financials"]["current_cash"] == 99.95
    assert ctrl.state["financials"]["cost_breakdown"]["llm_decision"] == 0.05

# 5. 验证模型不能复活已死亡机会
def test_dead_opportunity_is_immutable(clean_controller):
    ctrl = clean_controller
    ctrl.state["opportunity_memory"]["opp_closed"] = {"status": "DEAD"}

    action = {
        "opportunity_id": "opp_closed",
        "action_type": "INTERNAL",
        "tool_name": "read_file",
        "tool_args": {},
        "justification": "Let's try one more time"
    }
    valid, reason = ctrl.validate_action(action)
    assert not valid
    assert "permanently DEAD" in reason

# 6. 验证内部 10 步强制锁死
def test_internal_step_lockout(clean_controller):
    ctrl = clean_controller
    ctrl.state["runtime_guards"]["consecutive_internal_steps"] = 10
    ctrl.state["opportunity_memory"]["opp_active"] = {"status": "ACTIVE"}

    action = {
        "opportunity_id": "opp_active",
        "action_type": "INTERNAL",
        "tool_name": "read_file",
        "tool_args": {},
        "justification": "Thinking more"
    }
    valid, reason = ctrl.validate_action(action)
    assert not valid
    assert "Internal limit" in reason

# 7. 验证重复结算防护与非授权事件类型隔离
def test_prevent_duplicate_event_settlement(clean_controller):
    ctrl = clean_controller

    # 模拟已经被结算过的事件引用
    ctrl.state["settled_event_refs"].append("stripe_pi_already_used")
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "opportunity_id": "opp_duplicate",
        "event_type": "PAYMENT_CONFIRMATION",
        "external_reference": "stripe_pi_already_used",
        "timeout_timestamp": "2026-10-02T00:00:00Z"
    }

    class MockDispatcherReplay:
        def check_pending_event(self, pending):
            return {"status": "RESOLVED", "verified_revenue": 20.0}

    ctrl.poll_waiting_event(MockDispatcherReplay())

    # 重复结算必须被拒绝
    assert ctrl.state["financials"]["total_revenue"] == 0.0
    assert len(ctrl.state["transaction_ledger"]) == 0
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
