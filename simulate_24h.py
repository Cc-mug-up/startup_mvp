# simulate_24h.py
import json
import os
import sys
from datetime import datetime
from controller import Controller
from mock_tools import MockToolDispatcher
from mock_agent import ScriptedMockAgent

STATE_FILE = "sim_state.json"
LOG_FILE = "logs/sim_audit.jsonl"

def init_clean_state():
    state = {
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
            "current_time": "2026-10-01T00:00:00+00:00",
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def run_24h_simulation():
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    init_clean_state()

    ctrl = Controller(state_path=STATE_FILE, log_path=LOG_FILE)
    agent = ScriptedMockAgent()
    dispatcher = MockToolDispatcher()

    start_dt = datetime.fromisoformat(ctrl.state["runtime_guards"]["current_time"])
    target_dt = datetime.fromisoformat("2026-10-02T00:00:00+00:00")

    print(f"[*] Starting 24h Deterministic Simulation from {start_dt.isoformat()}...")

    step_count = 0
    while True:
        cur_dt = datetime.fromisoformat(ctrl.state["runtime_guards"]["current_time"])
        if cur_dt >= target_dt:
            print("[*] Reached 24h simulation boundary.")
            break

        step_count += 1

        # 如果处于 ACTIVE，每次动作推进 300 秒模拟时间
        if ctrl.state["runtime_guards"]["system_status"] == "ACTIVE":
            ctrl.tick(agent, dispatcher)
            ctrl.advance_time(300)
        # 如果处于 WAITING，tick 内部会自动轮询并步进 300 秒
        elif ctrl.state["runtime_guards"]["system_status"] == "WAITING":
            ctrl.tick(agent, dispatcher)

        if ctrl.state["runtime_guards"]["system_status"] in ["BANKRUPT", "HALTED_INVALID_LOOP"]:
            print(f"[!] System halted unexpectedly: {ctrl.state['runtime_guards']['system_status']}")
            sys.exit(1)

    # =========================================================================
    # 严格对账审计 (Financial & Logic Invariance Assertions)
    # =========================================================================
    final_state = ctrl.load_state()
    fin = final_state["financials"]
    guards = final_state["runtime_guards"]
    opps = final_state["opportunity_memory"]

    print("\n--- Final Simulation Audit ---")
    print(f"Final System Time : {guards['current_time']}")
    print(f"Total Steps Taken : {step_count}")
    print(f"Total Revenue     : ${fin['total_revenue']:.4f}")
    print(f"Total Hard Cost   : ${fin['total_hard_cost']:.4f}")
    print(f"Ending Cash       : ${fin['current_cash']:.4f}")
    print(f"Cost Breakdown    : {fin['cost_breakdown']}")

    # 1. 24小时固定基础设施折旧对账
    # 86400 秒 / 3600 * 0.0001 = $0.0024
    expected_infra_cost = 0.0024
    assert abs(fin["cost_breakdown"]["infrastructure"] - expected_infra_cost) < 1e-6, \
        f"Infra cost mismatch: {fin['cost_breakdown']['infrastructure']} vs {expected_infra_cost}"

    # 2. 真实收款对账 (opp_bounty_101 必须成功入账 $20)
    assert fin["total_revenue"] == 20.0, "Revenue was not properly credited."
    assert len(final_state["transaction_ledger"]) == 1
    assert final_state["transaction_ledger"][0]["amount"] == 20.0
    assert final_state["transaction_ledger"][0]["external_ref"] == "github_pr_42"

    # 3. 机会状态对账
    assert opps["opp_bounty_101"]["status"] == "ACTIVE"
    assert opps["opp_scrape_202"]["status"] == "DEAD", "opp_scrape_202 should be DEAD after ABANDON"

    # 4. 步数限制拦截测试对账
    # 检查 audit 日志，确认第 16 步确实被拦截过
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        logs = [json.loads(line) for line in f]

    interceptions = [l for l in logs if not l["validation"]["valid"]]
    assert len(interceptions) >= 1, "Agent 10-step violation was not intercepted!"
    assert "Internal limit" in interceptions[0]["validation"]["reason"]
    print("[✓] 10-step violation interception verified in logs.")

    # 5. 现金守恒公理：Ending Cash == Initial + Revenue - Total Cost
    expected_cash = fin["initial_capital"] + fin["total_revenue"] - fin["total_hard_cost"]
    assert abs(fin["current_cash"] - expected_cash) < 1e-6, "Cash balance leak detected!"

    print("[✓] All 24h deterministic assertions PASSED. No state or cash leaks detected.")

    # 清理临时仿真状态文件
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

if __name__ == "__main__":
    # Windows 控制台默认 GBK，[✓] 等字符无法编码输出，强制 UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_24h_simulation()
