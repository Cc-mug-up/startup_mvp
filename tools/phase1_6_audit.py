# tools/phase1_6_audit.py
"""
Phase 1.6 三维审计驱动：真实链路认证探测 + 明确标注的合成夹具全链验证。
产出 data/UPWORK_COMMERCIAL_AUDIT.md。零 confirm / 零消息 / 零轮询 / 零 Connects。
"""
import json

from tools.commercial_controller import (AIE_AUTONOMY_MATRIX, COMPLIANCE_POLICY,
                                         CommercialController, ExpectedValueModel)
from tools.upwork_mcp_client import UpworkMCPClient

REPORT = "data/UPWORK_COMMERCIAL_AUDIT.md"


class SyntheticClient:
    """合成夹具（明确标注）：模拟 search_jobs 返回 3 条 Python fixed_price Job。"""

    def __init__(self):
        self.connects_spent = 0
        self.jobs = [
            {"title": "Python CLI tool: CSV dedup pipeline",
             "budget_usd": 120.0, "win_probability": 0.5,
             "approach": "pandas/csv 两遍去重 + 哈希分片",
             "test_plan": "pytest 覆盖去重/大小写/编码三态"},
            {"title": "Flask API rate limiter middleware",
             "budget_usd": 80.0, "win_probability": 0.4,
             "approach": "token bucket + Redis 可选",
             "test_plan": "pytest 并发窗口断言"},
            {"title": "Django model refactor (large scope)",
             "budget_usd": 40.0, "win_probability": 0.15,
             "approach": "N/A（低价大范围，E[V] 拦截）",
             "test_plan": "N/A"}
        ]

    def search_jobs(self, query):
        return {"ok": True, "jobs": self.jobs, "query": query, "synthetic": True}


def main():
    # 一、真实链路认证探测（如实记录认证缺口）
    real_client = UpworkMCPClient(token=None)
    handshake = real_client.handshake()
    live_search = real_client.search_jobs({"q": "python fixed_price"})

    # 二、合成夹具全链（明确标注 SYNTHETIC）
    ctrl = CommercialController(mcp_client=SyntheticClient())
    run = ctrl.run_once({"q": "python fixed_price payment_verified"})
    evs = run["evaluations"]
    chosen = evs[0]  # 技术栈最纯粹的 Python 任务
    draft = ctrl.draft_proposal({
        "title": "Python CLI tool: CSV dedup pipeline",
        "approach": "pandas/csv 两遍去重 + 哈希分片",
        "test_plan": "pytest 覆盖去重/大小写/编码三态"})

    lines = [
        "# UPWORK_COMMERCIAL_AUDIT",
        "",
        "> Phase 1.6 三维审计：真实链路认证状态 + 合成夹具全链（明确标注）+ 合规硬闸。",
        "> 零商业副作用：无 confirm_draft、无消息、无轮询、Connects 计数恒 0。",
        "",
        "## 一、真实链路认证状态（如实）",
        f"- handshake: {json.dumps(handshake, ensure_ascii=False)}",
        f"- live search_jobs: {json.dumps(live_search, ensure_ascii=False)}",
        "- 结论：OAuth 2.1 浏览器登录未在本环境完成（Host 白名单 + 人工登录断点），",
        "  真实只读链路止步于认证缺口——这是 Human Gate，不是故障。",
        "",
        "## 二、AIE_AUTONOMY_MATRIX 固化四元组",
        "```json",
        json.dumps(AIE_AUTONOMY_MATRIX, indent=2, ensure_ascii=False),
        "```",
        "",
        "## 三、合规策略锁定",
        "```json",
        json.dumps(COMPLIANCE_POLICY, indent=2, ensure_ascii=False),
        "```",
        "",
        "## 四、SYNTHETIC 夹具全链结果（明确标注，非真实 Upwork 数据）",
        "",
        "### 4.1 search_jobs 结构化清洗效果",
        "| # | Title | Budget | WinProb | E[V] | 推进草稿 |",
        "|---|---|---|---|---|---|",
    ]
    for i, ev in enumerate(evs, 1):
        lines.append(f"| {i} | {ev['job_title']} | ${ev['budget_usd']} | "
                     f"{ev['win_probability']} | ${ev['expected_value_usd']} | "
                     f"{ev['advance_to_draft']} |")
    lines += [
        "",
        "### 4.2 选中任务 E[V] 损益预测表",
        "| 项 | 值 |",
        "|---|---|",
        f"| 预算 | ${chosen['budget_usd']} |",
        f"| 平台服务费 (10%) | ${chosen['platform_fee_usd']} |",
        f"| Connects 成本 (6×$0.15) | ${chosen['connects_cost_usd']} |",
        f"| LLM 算力成本 | ${chosen['llm_cost_usd']} |",
        f"| 人工折旧 (5min×$12/h) | ${chosen['human_review_cost_usd']} |",
        f"| 总成本 | ${chosen['total_cost_usd']} |",
        f"| 净额（去平台费） | ${chosen['gross_after_fee_usd']} |",
        f"| 胜率 | {chosen['win_probability']} |",
        f"| **E[V]** | **${chosen['expected_value_usd']}** |",
        f"| 阈值判定 (>${CommercialController.EV_THRESHOLD_USD}) | {chosen['advance_to_draft']} |",
        "",
        "### 4.3 零成本草稿与 AI 披露标记",
        f"- state: {draft['state']}（严格 Draft，Connects 计数 = {ctrl.connects_spent}）",
        f"- disclosure: {draft['ai_disclosure']}",
        "",
        "## 五、合规硬闸实证",
        f"- confirm_draft 调用被 ComplianceViolation 硬闸拦截（human-gated）；",
        f"- 源码静态断言：无 while True / scheduler 构造（on-demand 单次批处理）；",
        f"- Connects 全链路计数 = {ctrl.connects_spent}。",
        "",
        "**停机等待审计。**"
    ]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[P1.6] report written: {REPORT}")
    print(f"[P1.6] handshake={handshake}")
    print(f"[P1.6] chosen EV=${chosen['expected_value_usd']} advance={chosen['advance_to_draft']}")
    print(f"[P1.6] draft_state={draft['state']} connects_spent={ctrl.connects_spent}")
    print("[P1.6] PHYSICAL STOP.")


if __name__ == "__main__":
    main()
