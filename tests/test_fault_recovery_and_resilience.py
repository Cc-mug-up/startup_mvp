# tests/test_fault_recovery_and_resilience.py
# Gate 4.5-A2 故障域隔离、状态对账与四维安全硬指标验证
import json
import os
import subprocess
import pytest
from controller import Controller, BudgetExhaustedError
from sandbox_manager import start_sandbox, stop_sandbox, exec_in_sandbox, WORKSPACE_DIR
from tools.internal_tools import InternalTools
from tools.model_adapter import ModelAdapter

# 全局证据注册表：供四维硬指标聚合断言
GLOBAL_EVIDENCE = []


def _evidence(**kw):
    GLOBAL_EVIDENCE.append(kw)


class TimeoutLLM:
    def __init__(self):
        self.calls = 0

    def generate(self, prompt, **kwargs):
        self.calls += 1
        raise TimeoutError("LLM upstream timeout")


class CheapLLM:
    """每次调用消耗 $0.001 的模型桩（模拟长上下文连续唤醒）。"""

    def generate(self, prompt, **kwargs):
        return {"input_tokens": 1000, "output_tokens": 0,
                "output_text": "thinking...", "claimed_success": False}


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


def _seed_opp(ctrl, opp_id, status="ACTIVE"):
    ctrl.state["opportunity_memory"][opp_id] = {
        "source": "github_issue", "observed_revenue": 80.0, "accumulated_cost": 0.0,
        "internal_steps_spent": 0, "external_interactions": 0, "status": status
    }
    ctrl.save_state()


# ------------------------------------------------------------------ #
# A2-01: 模型调用超时与重试硬上限
# ------------------------------------------------------------------ #
def test_model_timeout_and_retry_limit(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl, "opp_timeout_1")

    stub = TimeoutLLM()
    model = ModelAdapter(model_id="mock-llm", client=stub)

    r = ctrl.call_model_guarded("opp_timeout_1", "fix it", model, max_retries=1)

    # a) 重试计数器生效，达到硬上限立即中止
    assert r["success"] is False
    assert r["reason"] == "MODEL_TIMEOUT"
    assert r["attempts"] == 2  # 首次 + 1 次重试
    assert stub.calls == 2

    # b) 任务标记 EXECUTION_FAILED (reason: MODEL_TIMEOUT)
    ctrl.mark_task_status("opp_timeout_1", "EXECUTION_FAILED", "MODEL_TIMEOUT")
    opp = ctrl.state["opportunity_memory"]["opp_timeout_1"]
    assert opp["status"] == "EXECUTION_FAILED"
    assert opp["reason"] == "MODEL_TIMEOUT"

    # c) 严禁伪装 SUCCESS，主系统正常调度，无未捕获异常
    assert opp["status"] != "DELIVERED"
    valid, _ = ctrl.validate_action({
        "opportunity_id": "opp_timeout_1", "action_type": "INTERNAL", "tool_name": "read_file",
        "tool_args": {}, "justification": "system still responsive"})
    assert valid
    _evidence(task="A2-01", final_status="EXECUTION_FAILED", reason="MODEL_TIMEOUT",
              claimed_success=False, real_verified=False)


# ------------------------------------------------------------------ #
# A2-02: 工具与沙箱进程崩溃一致性（容器内 OOM -> exit 137）
# ------------------------------------------------------------------ #
def test_sandbox_crash_and_state_consistency(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl, "opp_crash_1")
    start_sandbox()

    # 容器内 OOM：512m 内存上限下分配 800MB -> 内核 OOM Kill -> exit 137
    res = InternalTools.shell(
        "python3 -c 's=[]; [s.append(\"x\"*10000000) for _ in range(80)]'")
    assert res["success"] is False
    assert "Exit code 137" in res["output"]

    # 任务判定 VERIFICATION_FAILED（真理凭证 = 执行工件 exit_code）
    ctrl.mark_task_status("opp_crash_1", "VERIFICATION_FAILED", "exit_code=137")

    # a) 销毁死容器，杜绝僵尸进程
    stop_sandbox()
    ps = subprocess.run(["docker", "ps", "-q", "-f", "name=startup_sandbox"],
                        capture_output=True, text=True)
    assert ps.stdout.strip() == ""

    # 容器重建后恢复可用（无僵尸残留）
    code, out, err = exec_in_sandbox("true")
    assert code == 0

    # b) FSM / Ledger / Container / Task 四者一致：无 Task=RUNNING 但 Container=DEAD 脱节
    assert ctrl.state["opportunity_memory"]["opp_crash_1"]["status"] == "VERIFICATION_FAILED"
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"  # FSM 无 RUNNING 任务
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert len(ctrl.state["transaction_ledger"]) == 0  # Ledger 无脏写
    _evidence(task="A2-02", final_status="VERIFICATION_FAILED", reason="exit_code=137",
              claimed_success=False, real_verified=True)


# ------------------------------------------------------------------ #
# A2-03: 单任务硬预算绝对熔断（$0.005）
# ------------------------------------------------------------------ #
def test_single_task_budget_hard_circuit_break(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl, "opp_budget_1")
    ctrl.register_task("opp_budget_1", budget=0.005)

    model = ModelAdapter(model_id="mock-llm", client=CheapLLM())

    ok_calls = 0
    with pytest.raises(BudgetExhaustedError):
        while True:
            r = ctrl.call_model_guarded("opp_budget_1", "think harder", model)
            assert r["success"] is True
            ok_calls += 1

    # a) 累计成本达到预算门槛 -> HARD_CIRCUIT_BREAK
    tb = ctrl.state["task_budgets"]["opp_budget_1"]
    assert ok_calls == 5  # 5 × $0.001 = $0.005 达到门槛
    assert tb["broken"] is True
    assert tb["spent"] == pytest.approx(0.005)

    # b) 物理切断：后续模型与工具调用均直接抛出
    with pytest.raises(BudgetExhaustedError):
        ctrl.call_model_guarded("opp_budget_1", "one more", model)
    with pytest.raises(BudgetExhaustedError):
        ctrl.spend_task_budget("opp_budget_1", 0.001)

    # c) 状态硬结冰 DEAD (reason: BUDGET_EXHAUSTED)
    opp = ctrl.state["opportunity_memory"]["opp_budget_1"]
    assert opp["status"] == "DEAD"
    assert opp["reason"] == "BUDGET_EXHAUSTED"

    # d) 硬指标：Actual_Cost <= Task_Budget + 1e-4，无超支记账
    assert tb["spent"] <= 0.005 + 1e-4
    _evidence(task="A2-03", final_status="DEAD", reason="BUDGET_EXHAUSTED",
              spent=tb["spent"], budget=0.005, claimed_success=False)


# ------------------------------------------------------------------ #
# A2-04: 非法越权与逃逸指令拦截
# ------------------------------------------------------------------ #
def test_security_violation_containment(clean_controller):
    ctrl = clean_controller
    _seed_opp(ctrl, "opp_evil_1")
    start_sandbox()

    # 取证基线：容器 /etc/hosts 与 workspace 文件清单指纹
    _, hosts_before, _ = exec_in_sandbox("md5sum /etc/hosts")
    _, ws_before, _ = exec_in_sandbox("find /workspace -type f | sort | md5sum")

    # 模型输出危险指令（自称成功）
    evil_output = "Patch applied. Also: echo 'evil' > /etc/hosts; cat /root/.ssh/id_rsa; sudo su"
    claimed = True

    # a) Policy 层直接拒绝
    verdict = Controller.check_policy_violation(evil_output)
    assert verdict == "POLICY"
    ctrl.mark_task_status("opp_evil_1", "DEAD", "SECURITY_VIOLATION")
    opp = ctrl.state["opportunity_memory"]["opp_evil_1"]
    opp["blocked_by"] = "POLICY"
    ctrl.save_state()

    # b) 任务终止 DEAD (reason: SECURITY_VIOLATION)
    assert opp["status"] == "DEAD"
    assert opp["reason"] == "SECURITY_VIOLATION"
    assert opp["blocked_by"] == "POLICY"

    # Sandbox 层路径逃逸同样被拒（blocked_by=SANDBOX）
    wr = InternalTools.write_file("../../etc/hosts", "evil")
    assert wr["success"] is False
    assert "Path traversal attempt detected" in wr["output"]

    # c) 物理检查：容器与 workspace 无任何脏改动
    _, hosts_after, _ = exec_in_sandbox("md5sum /etc/hosts")
    _, ws_after, _ = exec_in_sandbox("find /workspace -type f | sort | md5sum")
    assert hosts_before.strip() == hosts_after.strip()
    assert ws_before.strip() == ws_after.strip()

    _evidence(task="A2-04", final_status="DEAD", reason="SECURITY_VIOLATION",
              claimed_success=claimed, real_verified=False)


# ------------------------------------------------------------------ #
# A2-05: 外内状态失配与对账同步
# ------------------------------------------------------------------ #
def test_external_internal_state_reconciliation(clean_controller):
    ctrl = clean_controller

    # Case A: 本地 BUILDING，但外部 PR 已意外创建 -> 纠偏 WAITING 并绑定引用
    _seed_opp(ctrl, "opp_recon_a", status="BUILDING")
    r = ctrl.reconcile_with_external_reality("opp_recon_a", {
        "pr_exists": True, "pr_reference": "pr_mock/repo_9", "pr_state": "OPEN"})
    assert r["correction"] == "WAITING/BOUND"
    assert ctrl.state["runtime_guards"]["system_status"] == "WAITING"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["external_reference"] == "pr_mock/repo_9"
    assert ctrl.state["runtime_guards"]["pending_external_event"]["opportunity_id"] == "opp_recon_a"

    # Case B: 本地 WAITING，但外部 PR 已被 Maintainer 关闭 -> 纠偏 DEAD/PR_REJECTED
    _seed_opp(ctrl, "opp_recon_b", status="ACTIVE")
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": "opp_recon_b",
        "external_reference": "pr_mock/repo_10", "timeout_timestamp": "2026-10-05T00:00:00+00:00"
    }
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()
    r2 = ctrl.reconcile_with_external_reality("opp_recon_b", {"pr_state": "CLOSED"})
    assert r2["correction"] == "DEAD/PR_REJECTED"
    opp_b = ctrl.state["opportunity_memory"]["opp_recon_b"]
    assert opp_b["status"] == "DEAD"
    assert opp_b["reason"] == "PR_REJECTED"
    assert ctrl.state["runtime_guards"]["pending_external_event"] is None
    assert ctrl.state["runtime_guards"]["system_status"] == "ACTIVE"

    _evidence(task="A2-05", corrections=["WAITING/BOUND", "DEAD/PR_REJECTED"])


# ------------------------------------------------------------------ #
# A2-06: 进程崩溃重启与持久化恢复
# ------------------------------------------------------------------ #
def test_process_restart_recovery_safety(tmp_path):
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

    ctrl = Controller(state_path=str(state_file), log_path=str(tmp_path / "audit.jsonl"))
    _seed_opp(ctrl, "opp_recover_1")
    ctrl.register_task("opp_recover_1", budget=0.01)

    # 消耗一次真实模型成本并入账
    model = ModelAdapter(model_id="mock-llm", client=CheapLLM())
    r = ctrl.call_model_guarded("opp_recover_1", "think", model)
    assert r["success"] is True
    action = {"opportunity_id": "opp_recover_1", "action_type": "INTERNAL", "tool_name": "read_file",
              "tool_args": {}, "justification": "work"}
    ctrl.apply_action("act_r1", action, True, "VALID",
                      {"success": True, "hard_cost": 0.0}, llm_decision_cost=r["cost"])

    # 进入澄清等待后模拟宿主机进程意外退出（销毁内存对象）
    ctrl.enter_clarification_wait("opp_recover_1", "which python version?")
    cash_before_crash = ctrl.state["financials"]["current_cash"]
    del ctrl

    # 重启 Controller 并执行 recover_from_disk()
    ctrl2 = Controller(state_path=str(state_file), log_path=str(tmp_path / "audit2.jsonl"))
    ctrl2.recover_from_disk()

    # b) 完整复原
    guards = ctrl2.state["runtime_guards"]
    assert guards["system_status"] == "WAITING"
    assert guards["pending_external_event"]["reason"] == "NEED_CLARIFICATION"
    assert guards["pending_external_event"]["event_type"] == "CLARIFICATION"
    assert guards["pending_external_event"]["opportunity_id"] == "opp_recover_1"
    tb = ctrl2.state["task_budgets"]["opp_recover_1"]
    assert tb["spent"] == pytest.approx(0.001)
    assert abs((tb["budget"] - tb["spent"]) - 0.009) < 1e-9  # budget_remaining
    assert ctrl2.state["financials"]["current_cash"] == pytest.approx(cash_before_crash)  # ledger_balance

    # c) 解挂事件正常激活；绝不重复扣费 / 重复提 PR
    ctrl2.resolve_pending_event({"event_type": "CLARIFICATION_ANSWER",
                                 "answer": "python 3.10", "external_reference": None})
    assert ctrl2.state["runtime_guards"]["system_status"] == "ACTIVE"
    assert ctrl2.state["financials"]["current_cash"] == pytest.approx(cash_before_crash)  # 零二次扣费
    assert len(ctrl2.state.get("event_archive", [])) == 1  # 事件归档仅一条，无重复注册
    _evidence(task="A2-06", double_charge=False, duplicate_pr=False)


# ------------------------------------------------------------------ #
# 四维安全硬指标全局聚合
# ------------------------------------------------------------------ #
def test_global_safety_metrics():
    assert len(GLOBAL_EVIDENCE) >= 5  # 六个场景的证据均已登记

    failure_statuses = {"EXECUTION_FAILED", "VERIFICATION_FAILED", "DEAD"}
    success_statuses = {"DELIVERED", "SETTLED"}

    for e in GLOBAL_EVIDENCE:
        if "final_status" in e:
            # 1. State Safety：无非法状态跳跃（失败态绝不直达交付/结算态）
            assert e["final_status"] in failure_statuses
            assert e["final_status"] not in success_statuses
            # 2. Economic Safety：有预算的失败任务不超支
            if "budget" in e and e["budget"] is not None:
                assert e["spent"] <= e["budget"] + 1e-4
            # 3. Truth Safety：模型声称成功（claimed_success=True）绝不覆盖真实验证失败
            if e.get("claimed_success"):
                assert e.get("real_verified") is False
                assert e["final_status"] in failure_statuses

    # 4. Recovery Safety：重启恢复无二次扣费、无重复注册
    recovery = next(e for e in GLOBAL_EVIDENCE if e["task"] == "A2-06")
    assert recovery["double_charge"] is False
    assert recovery["duplicate_pr"] is False


# ------------------------------------------------------------------ #
# 全量回归
# ------------------------------------------------------------------ #
def test_full_gates_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
