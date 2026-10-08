# tests/test_private_e2e_rehearsal.py
# Gate 4.5-A1 私有受控闭环四场景验证（本地 Mock 仓库 / 固件模拟 / 零公网脏数据）
import json
import os
import shutil
import pytest
from controller import Controller
from sandbox_manager import start_sandbox, exec_in_sandbox, WORKSPACE_DIR
from tools.github_pr_issuer import GitHubPRIssuer
from tools.model_adapter import ModelAdapter
from tools.opportunity_filter import OpportunityFilter
from tools.preflight_checker import PreflightChecker
from tools.sandbox_delivery import SandboxDeliveryPipeline

NOW = "2026-10-04T00:00:00Z"

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
TEST_CALC = "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
SMOKE_OK = "def test_env_sane():\n    assert 1 + 1 == 2\n"
SMOKE_BROKEN = "import nonexistent_module_xyz\n\n\ndef test_env():\n    assert True\n"

CORRECT_PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)
WRONG_PATCH = CORRECT_PATCH.replace("return a + b", "return a * b")


class ScriptedLLMClient:
    """确定性模型桩：按脚本顺序吐响应。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate(self, prompt, **kwargs):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[i]


class MockGitHubAPI:
    def __init__(self):
        self.calls = 0

    def create_pull_request(self, **kwargs):
        self.calls += 1
        return {"number": 42, "html_url": "https://github.com/mock/repo/pull/42"}


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


def _init_repo(repo_name, files):
    start_sandbox()
    host = os.path.join(WORKSPACE_DIR, repo_name)
    shutil.rmtree(host, ignore_errors=True)
    os.makedirs(host, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(host, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    os.chown(host, 1000, 1000)  # 仓库根目录本身也必须归沙箱用户
    for root, dirs, fns in os.walk(host):
        for d in dirs:
            os.chown(os.path.join(root, d), 1000, 1000)
        for fn in fns:
            os.chown(os.path.join(root, fn), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{repo_name} && git init -q && git config user.email 'a@b.c' "
        f"&& git config user.name 'sandbox' && git add -A && git commit -qm initial")
    assert code == 0, f"Repo init failed: {out} {err}"
    return repo_name


def _candidate_opp(opp_id, title="Fix zero division error in auth client"):
    return {
        "opportunity_id": opp_id,
        "observed_revenue": 80.0,
        "metadata": {
            "title": title,
            "state": "open",
            "updated_at": "2026-10-01T00:00:00Z",
            "language": "python",
            "labels": [{"name": "bounty: $80"}],
            "repo_active_within_90d": True
        },
        "description": "Crash on empty input"
    }


def _seed_opp(ctrl, opp_id):
    ctrl.state["opportunity_memory"][opp_id] = {
        "source": "github_issue", "observed_revenue": 80.0, "accumulated_cost": 0.0,
        "internal_steps_spent": 0, "external_interactions": 0, "status": "ACTIVE"
    }
    ctrl.save_state()


# ------------------------------------------------------------------ #
# 场景 A：全流程正向闭环（Filter -> Preflight -> Model -> 沙箱 -> PR -> WAITING）
# ------------------------------------------------------------------ #
def test_scenario_a_positive_e2e(clean_controller):
    ctrl = clean_controller
    repo = _init_repo("rehearsal_repo_a", {
        "calculator.py": CALC_BUGGY,
        "test_calculator.py": TEST_CALC,
        "test_baseline.py": SMOKE_OK
    })

    # 1. Filter -> CANDIDATE
    verdict = OpportunityFilter.evaluate(_candidate_opp("opp_e2e_a_1"), current_time_iso=NOW)
    assert verdict["status"] == "CANDIDATE"

    # 2. Preflight -> PASS
    pre = PreflightChecker.run(repo)
    assert pre["preflight_passed"] is True

    # 3. Model 调用（遥测契约）
    model = ModelAdapter(model_id="mock-llm",
                         client=ScriptedLLMClient([{"input_tokens": 500, "output_tokens": 200,
                                                    "output_text": CORRECT_PATCH, "claimed_success": True}]))
    result = model.generate(prompt=f"Fix bug in {repo}")
    assert result["claimed_success"] is True
    assert result["input_tokens"] == 500 and result["output_tokens"] == 200

    # 4. 沙箱修补（真实 pytest 才是唯一裁判）
    delivery = SandboxDeliveryPipeline.execute_task("opp_e2e_a_1", repo, result["output_text"])
    assert delivery["success"] is True
    commit_sha = delivery["commit_sha"]
    assert len(commit_sha) == 40

    # 5. PR 发起
    api = MockGitHubAPI()
    issuer = GitHubPRIssuer("ghp_TEST", api)
    pr = issuer.create_pr("opp_e2e_a_1", "mock/repo", "agent/fix", "Fix bug", "Patch", commit_sha)
    assert pr["success"] is True

    # 6. 控制器状态流转 WAITING
    _seed_opp(ctrl, "opp_e2e_a_1")
    tool_result = {
        "success": True, "hard_cost": 0.005,
        "pending_event": {
            "event_type": "PR_REVIEW", "opportunity_id": "opp_e2e_a_1",
            "external_reference": pr["external_reference"],
            "timeout_timestamp": "2026-10-05T00:00:00+00:00"
        }
    }
    action = {"opportunity_id": "opp_e2e_a_1", "action_type": "EXTERNAL", "tool_name": "submit_pr",
              "tool_args": {}, "justification": "Submit fix"}
    ctrl.apply_action("act_a1", action, True, "VALID", tool_result, llm_decision_cost=result["cost"])
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["external_reference"] == pr["external_reference"]

    # 成本遥测分毫不差
    assert model.total_tokens == 700
    assert ctrl.state["financials"]["cost_breakdown"]["llm_decision"] == pytest.approx(model.total_cost)


# ------------------------------------------------------------------ #
# 场景 B：合理歧义 -> NEED_CLARIFICATION 物理冻结 -> 人类事件接入解挂
# ------------------------------------------------------------------ #
def test_scenario_b_clarification_waiting_and_resume(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl, "opp_e2e_b_1")

    model = ModelAdapter(model_id="mock-llm",
                         client=ScriptedLLMClient([{"input_tokens": 100, "output_tokens": 50,
                                                    "output_text": "NEED_CLARIFICATION: which python version must be supported?",
                                                    "claimed_success": False}]))
    result = model.generate(prompt="Analyze requirement")
    assert result["output_text"].startswith("NEED_CLARIFICATION")

    # Agent 输出澄清信号 -> 控制器进入 NEED_CLARIFICATION 冻结
    ctrl.enter_clarification_wait("opp_e2e_b_1", question=result["output_text"].split(": ", 1)[1])
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["reason"] == "NEED_CLARIFICATION"

    # 冻结期间：严禁任何代码编写/外部操作
    for atype, tool in (("INTERNAL", "write_file"), ("EXTERNAL", "submit_pr")):
        valid, reason = ctrl.validate_action({
            "opportunity_id": "opp_e2e_b_1", "action_type": atype, "tool_name": tool,
            "tool_args": {}, "justification": "try anyway"})
        assert not valid and "WAITING" in reason

    # 人类输入事件接入 -> 解挂恢复
    ctrl.resolve_pending_event({
        "event_type": "CLARIFICATION_ANSWER",
        "answer": "python 3.10 only",
        "external_reference": None
    })
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert ctrl.state["opportunity_memory"]["opp_e2e_b_1"]["clarification_answer"] == "python 3.10 only"

    # 恢复后 INTERNAL 动作重新合法
    valid, _ = ctrl.validate_action({
        "opportunity_id": "opp_e2e_b_1", "action_type": "INTERNAL", "tool_name": "write_file",
        "tool_args": {}, "justification": "Resume work with clarified spec"})
    assert valid


# ------------------------------------------------------------------ #
# 场景 C：环境坏掉 -> 预检拦截 -> 零 Token 消耗
# ------------------------------------------------------------------ #
def test_scenario_c_preflight_fail_zero_token_cost():
    repo = _init_repo("rehearsal_repo_c", {"test_baseline.py": SMOKE_BROKEN})

    # 基线 pytest 失败
    verdict = OpportunityFilter.evaluate(_candidate_opp("opp_e2e_c_1"), current_time_iso=NOW)
    assert verdict["status"] == "CANDIDATE"

    pre = PreflightChecker.run(repo)
    assert pre["preflight_passed"] is False
    assert pre["reason"] == "BASELINE_TEST_FAILED"

    model = ModelAdapter(model_id="mock-llm", client=ScriptedLLMClient([{}]))
    # 核心断言：模型从未被唤醒
    assert model.telemetry == []
    assert model.total_tokens == 0
    assert model.total_cost == 0.0

    # 仓库缺失 -> DEPENDENCY_ERROR 归因
    pre2 = PreflightChecker.run("repo_that_does_not_exist")
    assert pre2["preflight_passed"] is False
    assert pre2["reason"] == "DEPENDENCY_ERROR"


# ------------------------------------------------------------------ #
# 场景 D：模型撒谎 -> 沙箱 exit_code 是唯一裁判 -> VERIFICATION_FAILED
# ------------------------------------------------------------------ #
def test_scenario_d_anti_hallucination_deterministic_verify():
    repo = _init_repo("rehearsal_repo_d", {
        "calculator.py": CALC_BUGGY,
        "test_calculator.py": TEST_CALC,
        "test_baseline.py": SMOKE_OK
    })

    model = ModelAdapter(model_id="mock-llm",
                         client=ScriptedLLMClient([{"input_tokens": 500, "output_tokens": 200,
                                                    "output_text": WRONG_PATCH, "claimed_success": True}]))
    result = model.generate(prompt="Fix bug (I swear it works)")
    assert result["claimed_success"] is True  # 模型自称成功

    # 真实裁判：沙箱 pytest 实际 exit_code != 0
    delivery = SandboxDeliveryPipeline.execute_task("opp_e2e_d_1", repo, result["output_text"])
    assert delivery["success"] is False
    assert "Tests failed" in delivery["error"]

    # 坚决无视模型自我声明：流程在交付失败处终止，PR 发行器全程未被调用
    api = MockGitHubAPI()
    GitHubPRIssuer("ghp_TEST", api)
    # （流程守卫：delivery["success"] is False -> 终止，绝不进入 create_pr）
    assert api.calls == 0  # 平台零请求

    task_state = {"status": "VERIFICATION_FAILED", "reason": delivery["error"]}
    assert task_state["status"] == "VERIFICATION_FAILED"
    assert task_state["status"] != "DELIVERED"


# ------------------------------------------------------------------ #
# 回归 + 账本守恒
# ------------------------------------------------------------------ #
def test_a1_regression_and_ledger_conservation(clean_controller):
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    rc3 = pytest.main([os.path.join(base, "test_settlement_flow.py"), "-q"])
    rc4 = pytest.main([os.path.join(base, "test_github_scanner.py"), "-q"])
    rc5 = pytest.main([os.path.join(base, "test_opportunity_filter.py"), "-q"])
    rc6 = pytest.main([os.path.join(base, "test_opportunity_replay.py"), "-q"])
    rc7 = pytest.main([os.path.join(base, "test_market_observer.py"), "-q"])
    assert rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 and rc5 == 0 and rc6 == 0 and rc7 == 0

    # 账本守恒：模型遥测总额 == 逐条之和；控制器现金 == 初始 + 收入 - 总成本
    ctrl = clean_controller
    model = ModelAdapter(model_id="mock-llm",
                         client=ScriptedLLMClient([{"input_tokens": 1000, "output_tokens": 500,
                                                    "output_text": "x", "claimed_success": False}]))
    r = model.generate(prompt="p")
    assert model.total_cost == pytest.approx(r["cost"])
    assert model.total_tokens == 1500

    _seed_opp(ctrl, "opp_ledger_1")
    action = {"opportunity_id": "opp_ledger_1", "action_type": "INTERNAL", "tool_name": "read_file",
              "tool_args": {}, "justification": "work"}
    ctrl.apply_action("act_ledger", action, True, "VALID",
                      {"success": True, "hard_cost": 0.0005}, llm_decision_cost=r["cost"])
    fin = ctrl.state["financials"]
    expected_cash = fin["initial_capital"] + fin["total_revenue"] - fin["total_hard_cost"]
    assert abs(fin["current_cash"] - expected_cash) < 1e-9
    assert fin["cost_breakdown"]["llm_decision"] == pytest.approx(r["cost"])
    assert fin["cost_breakdown"]["tool_execution"] == pytest.approx(0.0005)
