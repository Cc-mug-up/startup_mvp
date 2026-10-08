# tools/opportunity_sources/base_source.py
"""
Gate 4.5-B0 机会源抽象基类：统一 Opportunity 契约（扩展 Freshness 与 Competition 字段）。

所有源适配器（Algora / github_bounty 等）必须输出该契约，
市场漏斗评估器只消费契约字段，与具体源解耦。
"""

# 统一 Opportunity 契约字段清单（16 字段）
OPPORTUNITY_CONTRACT_FIELDS = [
    "source",            # "algora" | "github_bounty"
    "source_id",         # 源内唯一 ID
    "repo",
    "issue_id",
    "issue_url",
    "title",
    "body",
    "reward_amount",     # float
    "reward_currency",   # 如 "USD"
    "reward_verified",   # 是否有平台官方托管背书
    "bounty_created_at", # ISO8601
    "last_activity_at",  # ISO8601（新鲜度信号）
    "claim_count",       # 认领人数（竞争信号）
    "open_pr_count",     # 竞标 PR 数（竞争信号）
    "language",
    "labels"             # list[str]
]


class OpportunitySource:
    """源基类：所有机会源适配器的抽象契约。纯只读，严禁任何写/认领/评论调用。"""
    name = "base"

    def fetch_opportunities(self, limit: int = 30) -> dict:
        raise NotImplementedError

    def normalize(self, raw: dict) -> dict:
        raise NotImplementedError

    @staticmethod
    def validate(opp: dict) -> bool:
        """契约完整性校验：16 字段全部存在。"""
        return isinstance(opp, dict) and all(k in opp for k in OPPORTUNITY_CONTRACT_FIELDS)
