# tools/commercial_controller.py
"""
Commercial Pilot Phase 1.6：Commercial Controller 本地极简模型。

固化 AIE_AUTONOMY_MATRIX 四元组、E[V] 预期经济模型、合规状态锁定：
- 持续轮询 = COMPLIANCE_RESTRICTED（仅支持 CLI 触发的单次批处理）；
- confirm_draft = HUMAN_GATED（严禁本控制器直接调用）；
- 一切金融绑定动作 = FORBIDDEN（站点认证边界）。
"""

AIE_AUTONOMY_MATRIX = {
    "read": {
        "autonomy": "FULL",
        "human_breakpoint": None,
        "tools": ["search_jobs", "get_job_posting", "get_rate_insights",
                  "get_freelancer_dashboard", "list_contracts", "get_messages"]
    },
    "draft": {
        "autonomy": "FULL",
        "human_breakpoint": None,
        "tools": ["draft_proposal", "draft_message", "draft_profile_update"]
    },
    "confirm": {
        "autonomy": "HUMAN_GATED",
        "human_breakpoint": "confirm_draft（Connects 在此时扣除；逐项人工确认）",
        "tools": ["confirm_draft"]
    },
    "binding": {
        "autonomy": "FORBIDDEN",
        "human_breakpoint": "upwork.com 站点认证（Accept Offer / Escrow / 里程碑资金）",
        "tools": []
    }
}

COMPLIANCE_POLICY = {
    "continuous_polling": "COMPLIANCE_RESTRICTED",   # 条款 4.1：禁止持续监测
    "trigger_model": "ON_DEMAND_SINGLE_BATCH",       # 仅 CLI 单次触发
    "ai_disclosure_required": True,                  # AI 生成内容强制披露标签
    "auto_decision": "COMPLIANCE_RESTRICTED"         # 条款 5.8：禁止纯自动决策
}


class ComplianceViolation(RuntimeError):
    """合规硬闸：confirm/polling/binding 越权即抛。"""


class ExpectedValueModel:
    """E[V] 预期经济模型：扣除平台费/Connects/LLM/人工折旧后取期望值。"""

    PLATFORM_FEE_RATE = 0.10
    CONNECTS_PER_PROPOSAL = 6
    CONNECTS_UNIT_COST_USD = 0.15
    LLM_COST_USD = 0.05
    HUMAN_REVIEW_MINUTES = 5
    HUMAN_HOURLY_USD = 12.0

    def expected_value(self, job: dict) -> dict:
        budget = float(job["budget_usd"])
        platform_fee = round(budget * self.PLATFORM_FEE_RATE, 4)
        connects_cost = round(self.CONNECTS_PER_PROPOSAL * self.CONNECTS_UNIT_COST_USD, 4)
        llm_cost = self.LLM_COST_USD
        human_cost = round(self.HUMAN_REVIEW_MINUTES / 60 * self.HUMAN_HOURLY_USD, 4)
        total_cost = round(platform_fee + connects_cost + llm_cost + human_cost, 4)
        gross = round(budget - platform_fee, 4)
        win_probability = float(job.get("win_probability", 0.0))
        ev = round(gross * win_probability - total_cost, 4)
        return {
            "budget_usd": budget,
            "platform_fee_usd": platform_fee,
            "connects_cost_usd": connects_cost,
            "llm_cost_usd": llm_cost,
            "human_review_cost_usd": human_cost,
            "total_cost_usd": total_cost,
            "gross_after_fee_usd": gross,
            "win_probability": win_probability,
            "expected_value_usd": ev
        }


class CommercialController:
    EV_THRESHOLD_USD = 5.0

    def __init__(self, mcp_client=None):
        self.mcp_client = mcp_client
        self.connects_spent = 0          # 硬断言：全链路恒 0
        self.drafts_produced = 0
        self.confirms_attempted = 0

    # ---------------- 合规状态锁定 ----------------
    @property
    def polling_status(self) -> str:
        # 物理约束：本模块不存在任何轮询循环，仅 on-demand 单次入口
        return COMPLIANCE_POLICY["continuous_polling"]

    def run_once(self, query: dict) -> dict:
        """唯一批处理入口：单次搜索 -> E[V] 评估 -> 零成本草稿（无循环、无守护）。"""
        if self.polling_status != "COMPLIANCE_RESTRICTED":
            raise ComplianceViolation("polling policy violated")
        results = self.mcp_client.search_jobs(query)
        if not results.get("ok"):
            return results
        evs = []
        for job in results["jobs"]:
            ev = ExpectedValueModel().expected_value(job)
            ev["job_title"] = job.get("title")
            ev["advance_to_draft"] = ev["expected_value_usd"] > self.EV_THRESHOLD_USD
            evs.append(ev)
        return {"ok": True, "evaluations": evs}

    def draft_proposal(self, job: dict) -> dict:
        """零成本草稿：携带合规 AI 披露标记，Connects 计数恒 0。"""
        if self.polling_status != "COMPLIANCE_RESTRICTED":
            raise ComplianceViolation("polling policy violated")
        draft = {
            "state": "DRAFT",  # 严格草稿态，绝不 confirm
            "ai_disclosure": ("[AI-GENERATED] This proposal draft was prepared with AI "
                              "assistance and requires human review before submission."),
            "title": f"Proposal for: {job.get('title', 'untitled')}",
            "approach": job.get("approach", "structured approach placeholder"),
            "test_plan": job.get("test_plan", "pytest regression plan placeholder")
        }
        self.drafts_produced += 1
        assert self.connects_spent == 0, "Connects must remain 0 in draft stage"
        return draft

    def confirm_draft(self, draft: dict):
        """HUMAN_GATED：控制器侧物理禁止，任何调用即抛合规硬闸。"""
        self.confirms_attempted += 1
        raise ComplianceViolation(
            "confirm_draft is HUMAN_GATED: requires explicit human confirmation; "
            "Connects are deducted at confirm time. Refusing to auto-confirm.")
