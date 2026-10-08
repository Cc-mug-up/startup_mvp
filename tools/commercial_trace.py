# tools/commercial_trace.py
"""
Commercial Pilot v0.1 商业审计轨迹跟踪器。

八阶段全生命周期（严格单调递增）：
  t0_discovered -> t1_filtered -> t2_reviewed -> t3_approved -> t4_delivery_started
  -> t5_delivery_verified -> t6_pr_opened -> t7_settlement_confirmed
记录每阶段时间戳、阶段间耗时与真实经济损益。
"""
from datetime import datetime, timezone

STAGES = [
    "t0_discovered", "t1_human_qualified", "t2_reviewed", "t3_work_started",
    "t4_pr_submitted", "t5_delivery_verified", "t6_pr_merged",
    "t7_settlement_confirmed"
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class CommercialTrace:
    def __init__(self, opportunity_id: str):
        self.opportunity_id = opportunity_id
        self.stage_times = {}
        self.economic = {"revenue": 0.0, "costs": []}  # costs: [{kind, amount, note}]

    def mark(self, stage: str, ts: str = None) -> str:
        """阶段标记：必须严格按序（前置阶段未标记则拒绝）。"""
        assert stage in STAGES, f"unknown stage {stage}"
        idx = STAGES.index(stage)
        missing = [s for s in STAGES[:idx] if s not in self.stage_times]
        assert not missing, f"stage order violated: {stage} requires {missing}"
        self.stage_times[stage] = ts or _now_iso()
        return self.stage_times[stage]

    def stage_elapsed(self) -> dict:
        """相邻已标记阶段之间的真实耗时（秒）。"""
        marked = [s for s in STAGES if s in self.stage_times]
        deltas = {}
        for a, b in zip(marked, marked[1:]):
            ta = datetime.fromisoformat(self.stage_times[a].replace("Z", "+00:00"))
            tb = datetime.fromisoformat(self.stage_times[b].replace("Z", "+00:00"))
            deltas[f"{a}->{b}"] = round((tb - ta).total_seconds(), 3)
        return deltas

    def record_cost(self, kind: str, amount: float, note: str = "") -> None:
        assert amount >= 0, "cost must be non-negative"
        self.economic["costs"].append({"kind": kind, "amount": float(amount), "note": note})

    def confirm_settlement(self, revenue: float, settled_at: str = None) -> None:
        assert revenue > 0, "settlement revenue must be positive"
        self.economic["revenue"] = float(revenue)
        self.mark("t7_settlement_confirmed", ts=settled_at)

    def economic_pnl(self) -> dict:
        """真实经济损益：净收益与 ROI（无收入时为 0）。"""
        total_cost = sum(c["amount"] for c in self.economic["costs"])
        revenue = self.economic["revenue"]
        net = revenue - total_cost
        roi = (net / total_cost) if total_cost > 0 else 0.0
        return {
            "revenue": revenue,
            "total_cost": round(total_cost, 6),
            "net_profit": round(net, 6),
            "roi": round(roi, 6)
        }

    def metrics(self) -> dict:
        """审计指标：TTC（关闭时长，单调递增）与经济净值。"""
        t0 = self.stage_times.get("t0_discovered")
        t7 = self.stage_times.get("t7_settlement_confirmed")
        ttc = None
        if t0 and t7:
            d0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
            d7 = datetime.fromisoformat(t7.replace("Z", "+00:00"))
            ttc = round((d7 - d0).total_seconds(), 3)
        pnl = self.economic_pnl()
        return {
            "ttc_seconds": ttc,
            "revenue_usd": pnl["revenue"],
            "total_cost_usd": pnl["total_cost"],
            "net_economic_value_usd": pnl["net_profit"],
            "roi": pnl["roi"]
        }

    def to_dict(self) -> dict:
        return {
            "opportunity_id": self.opportunity_id,
            "stage_times": self.stage_times,
            "stage_elapsed": self.stage_elapsed(),
            "economic": self.economic,
            "pnl": self.economic_pnl(),
            "metrics": self.metrics()
        }
