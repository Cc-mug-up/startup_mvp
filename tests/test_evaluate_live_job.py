# tests/test_evaluate_live_job.py
# Commercial Reality Test 离线决策卡单元测试（纯本地 / 合成夹具明确标注）
from tools.evaluate_live_job import LiveJobEvaluator

JOB_PASS = """
Fix python CSV dedup bug
Fixed price: $120
Payment verified
Proposals: Less than 5
Hires: 12 · Hire rate: 80%
Rating: 4.9
We need a small fix in our pandas dedup pipeline; unit tests included (pytest).
"""

JOB_NON_PYTHON = """
Rewrite Go service auth middleware
Fixed price: $500
Payment verified
Proposals: 5 to 10
We need a golang middleware rewrite with performance tuning.
"""

JOB_LOW_EV = """
Python one-liner fix
Fixed price: $15
Payment verified
Proposals: 10 to 15
Tiny python fix, no tests.
"""


def _run(text):
    return LiveJobEvaluator().run(text)


# ---------- 结构化抽取 ----------
def test_extraction_fields_pass_case():
    r = _run(JOB_PASS)
    ext = r["extracted"]
    assert ext["title"].startswith("Fix python CSV dedup bug")
    assert ext["budget_usd"] == 120.0
    assert ext["hourly"] is False
    assert ext["payment_verified"] is True
    assert ext["competition_bracket"] == "less_than_5"
    assert ext["hire_rate"] == 0.8
    assert ext["rating"] == 4.9
    assert ext["pure_python"] is True


def test_extraction_hourly_and_non_python():
    r = _run(JOB_NON_PYTHON)
    assert r["extracted"]["pure_python"] is False
    assert r["extracted"]["non_python_stack_hits"]
    assert r["verdict"] == "REJECT"


# ---------- E[V] 精算 ----------
def test_ev_precision_pass_case():
    r = _run(JOB_PASS)
    ev = r["ev"]
    assert ev["platform_fee_usd"] == 12.0
    assert ev["connects_cost_usd"] == 0.9
    assert ev["llm_cost_usd"] == 0.05
    # 5min @ $20/h = 1.6667
    assert abs(ev["human_review_cost_usd"] - 1.6667) < 0.001
    # 胜率 = 0.35 * (1 + 0.8*0.5) = 0.49
    assert abs(ev["win_probability"] - 0.49) < 0.001
    gross = 120 - 12
    total = 12 + 0.9 + 0.05 + 1.6667
    assert abs(ev["expected_value_usd"] - (gross * 0.49 - total)) < 0.01
    assert ev["expected_value_usd"] > 5.0


# ---------- 决策 ----------
def test_verdicts():
    assert _run(JOB_PASS)["verdict"] == "PASS"
    assert _run(JOB_NON_PYTHON)["verdict"] == "REJECT"
    assert _run(JOB_LOW_EV)["verdict"] == "REJECT"  # 低价 E[V] 为负


# ---------- 草稿合规 ----------
def test_proposal_draft_disclosure():
    draft = _run(JOB_PASS)["proposal_draft"]
    assert draft["state"] == "DRAFT"
    assert "[AI-GENERATED]" in draft["ai_disclosure"]


# ---------- 空输入守卫 ----------
def test_empty_input_guard():
    r = LiveJobEvaluator().run("")
    assert r["ok"] is False and r["state"] == "NO_INPUT"
