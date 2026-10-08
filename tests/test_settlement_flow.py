# tests/test_settlement_flow.py
# Gate 4.3-A 确定性单测：经济结算状态机与外部打款凭据闭环（纯 Mock / 零外网）
import os
import json
import pytest
from controller import Controller
from tools.payment_adapter import PaymentAdapter


def _webhook(opportunity_id, amount, payment_ref="pay_tx_12345", status="confirmed"):
    return {
        "payment_provider": "stripe",
        "payment_ref": payment_ref,
        "opportunity_id": opportunity_id,
        "amount": amount,
        "status": status,
        "timestamp": "2026-10-01T12:00:00Z"
    }


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


def _seed_opportunity(ctrl, opp_id="opp_gh_s_1", status="DELIVERED", observed=50.0, payment_waiting=True):
    ctrl.state["opportunity_memory"][opp_id] = {
        "source": "github_issue",
        "observed_revenue": observed,
        "accumulated_cost": 0.03,
        "internal_steps_spent": 3,
        "external_interactions": 1,
        "status": status,
        "payment_waiting": payment_waiting
    }
    ctrl.save_state()


# 1. DELIVERED -> SETTLED，收入准确递增 50.0，ledger 唯一 REVENUE 记录
def test_delivered_to_settled_and_revenue_incremented(clean_controller):
    ctrl = clean_controller
    _seed_opportunity(ctrl)

    parsed = PaymentAdapter.parse_payment_webhook(_webhook("opp_gh_s_1", 50.0))
    assert parsed["event_type"] == "PAYMENT_CONFIRMED"
    assert parsed["amount"] == 50.0

    res = ctrl.settle_opportunity_payment(parsed)

    assert res["success"] is True
    opp = ctrl.state["opportunity_memory"]["opp_gh_s_1"]
    assert opp["status"] == "SETTLED"
    assert opp["settled_at"] == "2026-10-01T12:00:00Z"
    assert ctrl.state["financials"]["total_revenue"] == 50.0
    assert ctrl.state["financials"]["current_cash"] == 150.0
    ledger = ctrl.state["transaction_ledger"]
    assert len(ledger) == 1
    assert ledger[0]["type"] == "REVENUE"
    assert ledger[0]["amount"] == 50.0
    assert ledger[0]["opportunity_id"] == "opp_gh_s_1"
    assert ledger[0]["payment_ref"] == "pay_tx_12345"


# 2. 连续 3 次相同 payment_ref：第 1 次成功，第 2/3 次幂等跳过，金额严禁翻倍
def test_duplicate_payment_rejection_idempotency(clean_controller):
    ctrl = clean_controller
    _seed_opportunity(ctrl)

    parsed = PaymentAdapter.parse_payment_webhook(_webhook("opp_gh_s_1", 50.0))

    r1 = ctrl.settle_opportunity_payment(parsed)
    r2 = ctrl.settle_opportunity_payment(parsed)
    r3 = ctrl.settle_opportunity_payment(parsed)

    assert r1["success"] is True
    assert r2["success"] is True and r2.get("duplicate") is True  # 幂等 no-op 而非报错
    assert r3.get("duplicate") is True
    assert ctrl.state["financials"]["total_revenue"] == 50.0  # 严禁翻倍为 150.0
    assert ctrl.state["financials"]["current_cash"] == 150.0
    assert len(ctrl.state["transaction_ledger"]) == 1


# 3. 不存在的机会 / 非 DELIVERED 状态的机会：拦截拒绝，收入分毫未变
def test_unmatched_or_illegal_opportunity_rejected(clean_controller):
    ctrl = clean_controller
    _seed_opportunity(ctrl, opp_id="opp_dead", status="DEAD", payment_waiting=False)
    _seed_opportunity(ctrl, opp_id="opp_active", status="ACTIVE", payment_waiting=False)

    # 不存在的 opportunity_id
    r1 = ctrl.settle_opportunity_payment(
        PaymentAdapter.parse_payment_webhook(_webhook("opp_ghost_404", 50.0)))
    assert r1["success"] is False

    # DEAD 状态
    r2 = ctrl.settle_opportunity_payment(
        PaymentAdapter.parse_payment_webhook(_webhook("opp_dead", 50.0)))
    assert r2["success"] is False

    # ACTIVE（等待交付）状态
    r3 = ctrl.settle_opportunity_payment(
        PaymentAdapter.parse_payment_webhook(_webhook("opp_active", 50.0)))
    assert r3["success"] is False

    assert ctrl.state["financials"]["total_revenue"] == 0.0
    assert ctrl.state["financials"]["current_cash"] == 100.0
    assert len(ctrl.state["transaction_ledger"]) == 0


# 4. 金额伪造攻击（负数 / 与 observed_revenue 不符的天价）：拦截拒绝，严禁入账
def test_amount_tampering_or_negative_rejected(clean_controller):
    ctrl = clean_controller
    _seed_opportunity(ctrl)  # observed_revenue = 50.0

    # 负数：适配器直接判无效
    neg = PaymentAdapter.parse_payment_webhook(_webhook("opp_gh_s_1", -100.0))
    assert neg["event_type"] == "PAYMENT_INVALID"
    assert ctrl.settle_opportunity_payment(neg)["success"] is False

    # 与 observed_revenue 不符的天价金额：控制器拦截
    inflated = PaymentAdapter.parse_payment_webhook(_webhook("opp_gh_s_1", 99999.0))
    assert inflated["event_type"] == "PAYMENT_CONFIRMED"
    r = ctrl.settle_opportunity_payment(inflated)
    assert r["success"] is False and "does not match" in r["error"]

    # 零金额：适配器判无效
    zero = PaymentAdapter.parse_payment_webhook(_webhook("opp_gh_s_1", 0.0))
    assert zero["event_type"] == "PAYMENT_INVALID"

    assert ctrl.state["financials"]["total_revenue"] == 0.0
    assert ctrl.state["financials"]["current_cash"] == 100.0
    assert len(ctrl.state["transaction_ledger"]) == 0


# 5. 全生命周期数学守恒：final_cash == initial_cash + total_revenue - total_cost
def test_full_ledger_conservation(clean_controller):
    ctrl = clean_controller
    ctrl.state["opportunity_memory"]["opp_gh_s_5"] = {
        "source": "github_issue",
        "observed_revenue": 50.0,
        "accumulated_cost": 0.0,
        "internal_steps_spent": 0,
        "external_interactions": 0,
        "status": "ACTIVE"
    }

    # 创建机会并消耗 Token 成本（INTERNAL）
    act_internal = {
        "opportunity_id": "opp_gh_s_5",
        "action_type": "INTERNAL",
        "tool_name": "write_file",
        "tool_args": {},
        "justification": "Develop fix"
    }
    ctrl.apply_action("act_1", act_internal, True, "VALID",
                      {"success": True, "hard_cost": 0.0005}, llm_decision_cost=0.01)

    # 提 PR（EXTERNAL）挂起
    act_pr = {
        "opportunity_id": "opp_gh_s_5",
        "action_type": "EXTERNAL",
        "tool_name": "submit_pr",
        "tool_args": {},
        "justification": "Submit PR"
    }
    pr_tool_result = {
        "success": True,
        "hard_cost": 0.005,
        "pending_event": {
            "event_type": "PR_REVIEW",
            "opportunity_id": "opp_gh_s_5",
            "external_reference": "pr_mock/repo_42",
            "timeout_timestamp": "2026-10-02T00:00:00+00:00"
        }
    }
    ctrl.apply_action("act_2", act_pr, True, "VALID", pr_tool_result, llm_decision_cost=0.01)
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"

    # Merged -> DELIVERED
    from tools.github_pr_poller import GitHubPRPoller
    ctrl.resolve_pending_event(GitHubPRPoller.parse_poll_response({
        "state": "closed", "merged": True,
        "external_reference": "pr_mock/repo_42",
        "timestamp": "2026-10-01T06:00:00Z"
    }))
    assert ctrl.state["opportunity_memory"]["opp_gh_s_5"]["status"] == "DELIVERED"

    # 打款 -> SETTLED
    parsed = PaymentAdapter.parse_payment_webhook(
        _webhook("opp_gh_s_5", 50.0, payment_ref="pay_tx_777"))
    assert ctrl.settle_opportunity_payment(parsed)["success"] is True

    fin = ctrl.state["financials"]
    assert fin["total_revenue"] == 50.0
    # 数学守恒恒等式
    expected_cash = fin["initial_capital"] + fin["total_revenue"] - fin["total_hard_cost"]
    assert abs(fin["current_cash"] - expected_cash) < 1e-9
    # 成本明细：llm 0.02 + tool (0.0005 + 0.005)
    assert abs(fin["total_hard_cost"] - 0.0255) < 1e-9
    assert fin["cost_breakdown"]["llm_decision"] == pytest.approx(0.02)
    assert fin["cost_breakdown"]["tool_execution"] == pytest.approx(0.0055)


# 6. 全套回归：Gate 1 + Gate 4.2-C 嵌套执行，零破损
def test_full_gates_zero_regression():
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    assert rc1 == 0 and rc2 == 0
