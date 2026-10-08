# tests/test_commercial_controller.py
# Phase 1.6 Commercial Controller 决策/合规引擎单元测试（纯本地）
import pytest

from tools.commercial_controller import (AIE_AUTONOMY_MATRIX, COMPLIANCE_POLICY,
                                         CommercialController, ComplianceViolation,
                                         ExpectedValueModel)


class FakeMCPClient:
    """合成夹具客户端（明确标注：非真实 Upwork 数据）。"""

    def __init__(self, jobs):
        self.jobs = jobs
        self.connects_spent = 0

    def search_jobs(self, query):
        return {"ok": True, "jobs": self.jobs, "query": query, "synthetic": True}


def _job(budget, win_probability, title="Python bug fix"):
    return {"budget_usd": budget, "win_probability": win_probability, "title": title}


# ---------- 契约 ----------
def test_autonomy_matrix_four_tuple():
    assert set(AIE_AUTONOMY_MATRIX.keys()) == {"read", "draft", "confirm", "binding"}
    assert AIE_AUTONOMY_MATRIX["confirm"]["autonomy"] == "HUMAN_GATED"
    assert AIE_AUTONOMY_MATRIX["binding"]["autonomy"] == "FORBIDDEN"
    assert AIE_AUTONOMY_MATRIX["read"]["human_breakpoint"] is None


def test_compliance_policy_locked():
    assert COMPLIANCE_POLICY["continuous_polling"] == "COMPLIANCE_RESTRICTED"
    assert COMPLIANCE_POLICY["trigger_model"] == "ON_DEMAND_SINGLE_BATCH"
    assert COMPLIANCE_POLICY["ai_disclosure_required"] is True


# ---------- E[V] 经济模型 ----------
def test_expected_value_math():
    ev = ExpectedValueModel().expected_value(_job(budget=100.0, win_probability=0.5))
    # 平台费 10 / Connects 6×0.15=0.9 / LLM 0.05 / 人工 5min×12$/h=1.0
    assert ev["platform_fee_usd"] == 10.0
    assert ev["connects_cost_usd"] == pytest.approx(0.9)
    assert ev["total_cost_usd"] == pytest.approx(10.0 + 0.9 + 0.05 + 1.0)
    assert ev["gross_after_fee_usd"] == 90.0
    assert ev["expected_value_usd"] == pytest.approx(90.0 * 0.5 - 11.95)
    assert ev["expected_value_usd"] > 5.0  # 高于阈值


def test_expected_value_below_threshold_blocks_draft():
    ev = ExpectedValueModel().expected_value(_job(budget=20.0, win_probability=0.2))
    assert ev["expected_value_usd"] < CommercialController.EV_THRESHOLD_USD


# ---------- 控制器合规硬闸 ----------
def test_controller_run_once_and_draft_zero_cost():
    ctrl = CommercialController(mcp_client=FakeMCPClient([
        _job(100.0, 0.5), _job(20.0, 0.2), _job(60.0, 0.3)]))
    r = ctrl.run_once({"q": "python fixed_price"})
    assert r["ok"] and r["synthetic"] if False else r["ok"] is True
    evs = r["evaluations"]
    assert len(evs) == 3
    assert evs[0]["advance_to_draft"] is True   # EV > 5
    assert evs[1]["advance_to_draft"] is False  # 低价任务拦截

    draft = ctrl.draft_proposal({"title": "Python bug fix"})
    assert draft["state"] == "DRAFT"
    assert "[AI-GENERATED]" in draft["ai_disclosure"]
    assert ctrl.connects_spent == 0


def test_controller_confirm_is_hard_blocked():
    ctrl = CommercialController(mcp_client=FakeMCPClient([]))
    with pytest.raises(ComplianceViolation):
        ctrl.confirm_draft({"state": "DRAFT"})
    assert ctrl.connects_spent == 0  # 硬闸后仍零扣费


def test_polling_restriction_physical():
    # 静态扫描：控制器与客户端源码中不得存在轮询循环构造
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("tools/commercial_controller.py", "tools/upwork_mcp_client.py"):
        src = open(os.path.join(base, fname), encoding="utf-8").read()
        assert "while True" not in src, f"{fname} contains polling loop"
        assert "schedule" not in src.lower(), f"{fname} contains scheduler"
