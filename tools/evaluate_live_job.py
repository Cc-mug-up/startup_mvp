# tools/evaluate_live_job.py
"""
Commercial Reality Test — 真实 Job 离线商业决策卡生成（MANUAL_JOB_INGESTION）。

冻结 OAuth 连接脚本与爬虫；零外部网络写。接收人工粘贴的真实 Upwork Job 原始文本，
执行结构化抽取 -> E[V] 精算 -> 决策卡（含 [AI-GENERATED] 合规披露的提案草稿）。
"""
import json
import os
import re

from tools.commercial_controller import ExpectedValueModel

INPUT_PATH = "data/live_job_input.json"
# 影子时薪（本阶段规格：$20/h 人工审批折旧）
SHADOW_HOURLY_USD = 20.0
HUMAN_REVIEW_MINUTES = 5
EV_THRESHOLD_USD = 5.0

COMPETITION_WIN_PROB = {
    "less_than_5": 0.35, "5_to_10": 0.25, "10_to_15": 0.15,
    "15_to_20": 0.08, "20_to_50": 0.04, "50_plus": 0.02, "unknown": 0.20
}

PYTHON_SKILLS = ("python", "django", "flask", "fastapi", "pandas", "numpy",
                 "scrapy", "celery", "pytest", "sqlalchemy", "asyncio")
NON_PYTHON_STACK = ("c++", "rust", "golang", "java ", "c#", "kotlin", "swift",
                    "react", "node.js", "php")


class LiveJobEvaluator:
    # ------------------------------------------------------------------ #
    def load_input(self, path: str = INPUT_PATH) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def extract(self, raw_text: str) -> dict:
        """结构化抽取：标题/预算/付款验证/竞争区间/客户历史/技能。"""
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
        title = lines[0] if lines else ""
        low = raw_text.lower()

        # 预算
        budget = None
        m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", raw_text)
        if m:
            budget = float(m.group(1).replace(",", ""))
        hourly = "hourly" in low or ("/hr" in low)

        payment_verified = bool(re.search(r"payment\s*(method)?\s*verified", low))

        # 竞争区间
        bracket = "unknown"
        m = re.search(r"proposals?[:\s]*(less than \d+|[\d\s]*to \d+|[\d]+-[\d]+|[\d]+\+)",
                      low, re.IGNORECASE)
        if m:
            b = re.sub(r"\s+", "_", m.group(1).strip())
            if "less_than" in b:
                bracket = "less_than_5"
            elif b in ("5_to_10", "10_to_15", "15_to_20", "20_to_50"):
                bracket = b
            elif b == "50+":
                bracket = "50_plus"

        # 客户历史
        hire_rate = None
        m = re.search(r"hire\s*rate[:\s]*([\d.]+)\s*%", low)
        if m:
            hire_rate = float(m.group(1)) / 100.0
        rating = None
        m = re.search(r"rating[:\s]*([\d.]+)", low)
        if m:
            rating = float(m.group(1))

        # 技能栈
        skills = [s for s in PYTHON_SKILLS if s in low]
        non_python = [s for s in NON_PYTHON_STACK if s in low]

        return {
            "title": title,
            "raw_text": raw_text,
            "budget_usd": budget,
            "hourly": hourly,
            "payment_verified": payment_verified,
            "competition_bracket": bracket,
            "hire_rate": hire_rate,
            "rating": rating,
            "python_skills": skills,
            "non_python_stack_hits": non_python,
            "pure_python": bool(skills) and not non_python
        }

    # ------------------------------------------------------------------ #
    def scope_assessment(self, ext: dict) -> dict:
        """技术栈与 Scope 评估：纯 Python、预估文件数 <= 2、本地脱机单测条件。"""
        low = ext["raw_text"].lower()
        offline_testable = any(k in low for k in ("pytest", "unit test", "test coverage",
                                                  "test case")) or "test" in low
        estimated_files = 1 if any(k in low for k in ("fix", "bug", "small", "minor")) else 2
        return {
            "pure_python": ext["pure_python"],
            "estimated_files": estimated_files,
            "offline_testable": offline_testable
        }

    def economic_ev(self, ext: dict) -> dict:
        """E[V] 精算：平台费 10% / Connects / LLM / 5 分钟人工折旧（$20/h 影子时薪）。"""
        win_prob = COMPETITION_WIN_PROB.get(ext["competition_bracket"], 0.20)
        if ext.get("hire_rate") is not None:
            win_prob = min(0.6, win_prob * (1.0 + ext["hire_rate"] * 0.5))
        job = {"budget_usd": ext.get("budget_usd") or 0.0, "win_probability": win_prob}
        base = ExpectedValueModel().expected_value(job)
        # 影子时薪覆盖人工折旧（$20/h）
        human_cost = round(HUMAN_REVIEW_MINUTES / 60 * SHADOW_HOURLY_USD, 4)
        total = round(base["platform_fee_usd"] + base["connects_cost_usd"] +
                      base["llm_cost_usd"] + human_cost, 4)
        ev = round((base["budget_usd"] - base["platform_fee_usd"]) * win_prob - total, 4)
        return {
            **base, "human_review_cost_usd": human_cost,
            "total_cost_usd": total, "expected_value_usd": ev
        }

    # ------------------------------------------------------------------ #
    def decision(self, ext: dict, scope: dict, ev: dict) -> str:
        """准入结论：PASS / REJECT / REVIEW。"""
        if ext.get("hourly"):
            return "REJECT"  # 固定价任务仅准入（本模式约束）
        if not ext.get("payment_verified"):
            return "REJECT"
        if not scope["pure_python"]:
            return "REJECT"
        if scope["estimated_files"] > 2:
            return "REVIEW"
        if ev["expected_value_usd"] > EV_THRESHOLD_USD:
            return "PASS"
        return "REVIEW" if ev["expected_value_usd"] > 0 else "REJECT"

    def proposal_draft(self, ext: dict) -> dict:
        """可投递提案草稿（[AI-GENERATED] 合规披露，绝不自动提交）。"""
        return {
            "state": "DRAFT",
            "ai_disclosure": ("[AI-GENERATED] This proposal was drafted with AI assistance "
                              "and requires human review before submission."),
            "title": f"Proposal for: {ext['title'][:80]}",
            "technical_approach": ("Pure-Python implementation with minimal surface change "
                                   "and backward-compatible behavior."),
            "test_plan": ("Local pytest regression suite covering happy path, boundary "
                          "conditions and failure modes; offline runnable."),
            "delivery_notes": "Estimated scope: <= 2 files; no external service dependency."
        }

    # ------------------------------------------------------------------ #
    def run(self, raw_text: str) -> dict:
        if not raw_text.strip():
            return {"ok": False, "state": "NO_INPUT",
                    "reason": "raw_text is empty: await human-pasted real job text"}
        ext = self.extract(raw_text)
        scope = self.scope_assessment(ext)
        ev = self.economic_ev(ext)
        verdict = self.decision(ext, scope, ev)
        draft = self.proposal_draft(ext)
        return {"ok": True, "extracted": ext, "scope": scope, "ev": ev,
                "verdict": verdict, "proposal_draft": draft}


def render_card(result: dict) -> str:
    if not result.get("ok"):
        return f"[DECISION CARD] {result.get('state')}: {result.get('reason')}"
    ext, scope, ev, draft = result["extracted"], result["scope"], result["ev"], result["proposal_draft"]
    return "\n".join([
        "==================== [ COMMERCIAL DECISION CARD #001 ] ====================",
        f"title: {ext['title']}",
        f"budget: ${ev['budget_usd']} ({'hourly' if ext['hourly'] else 'fixed_price'})",
        f"payment_verified: {ext['payment_verified']}",
        f"competition_bracket: {ext['competition_bracket']} (win_prob={ev['win_probability']})",
        f"hire_rate: {ext.get('hire_rate')} | rating: {ext.get('rating')}",
        f"scope: pure_python={scope['pure_python']} files~{scope['estimated_files']} "
        f"offline_testable={scope['offline_testable']}",
        "--- 损益预估 ---",
        f"gross_after_fee: ${ev['gross_after_fee_usd']}",
        f"hard_costs: platform_fee=${ev['platform_fee_usd']} connects=${ev['connects_cost_usd']} "
        f"llm=${ev['llm_cost_usd']}",
        f"labor_depreciation: ${ev['human_review_cost_usd']} (5min @ $20/h shadow wage)",
        f"total_cost: ${ev['total_cost_usd']}",
        f"E[V]: ${ev['expected_value_usd']}",
        f"verdict: {result['verdict']}",
        "--- proposal draft ---",
        f"state: {draft['state']} | disclosure: {draft['ai_disclosure']}",
        f"approach: {draft['technical_approach']}",
        f"test_plan: {draft['test_plan']}",
        "=============================================================================="
    ])


def main():
    evaluator = LiveJobEvaluator()
    if os.path.exists(INPUT_PATH):
        data = evaluator.load_input()
        raw = data.get("raw_text", "")
    else:
        raw = ""
    result = evaluator.run(raw)
    print(render_card(result))
    print("[LIVE] PHYSICAL STOP. Zero external writes. Awaiting human review.")


if __name__ == "__main__":
    main()
