# tools/market_funnel_evaluator.py
"""
Gate 4.5-B0 四层机会漏斗与双维信号评估器。

Layer 1 (Reward):    reward_verified == True 且 reward_amount >= 50.0
Layer 2 (Rules):     经 tools/opportunity_filter.OpportunityFilter 判定 CANDIDATE
                     （年龄策略委托给 Layer 4 新鲜度检验）
Layer 3 (Preflight): 静态环境依赖完备度模拟（无真实沙箱时的信息检验）
Layer 4 (Economic):  新鲜度（last_activity <= 30 天）+ 竞争度（claim < 5 且 open_pr < 2）
"""
from datetime import datetime
from tools.opportunity_filter import OpportunityFilter

# Layer 3 静态预检黑名单标记：出现即视为环境依赖不完备
PREFLIGHT_BLOCKED_MARKERS = [
    "gpu", "cuda", "proprietary sdk", "api key required", "docker-in-docker", "fpga"
]


def _parse_iso(ts) -> datetime:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _to_filter_input(o: dict, now_iso: str) -> dict:
    """统一契约 -> 过滤器输入（年龄策略委托 Layer 4，故以当前时间填充 updated_at）。"""
    return {
        "opportunity_id": f"{o.get('source', 'src')}_{o.get('source_id', 'x')}",
        "observed_revenue": float(o.get("reward_amount") or 0.0),
        "description": o.get("body") or "",
        "metadata": {
            "title": o.get("title") or "",
            "state": "open",
            "updated_at": now_iso,
            "language": o.get("language") or None,
            "labels": [{"name": x} for x in (o.get("labels") or [])],
            "repo_active_within_90d": None  # 未知事实：不拦截
        }
    }


class MarketFunnelEvaluator:
    MIN_REWARD = 50.0
    MAX_STALE_DAYS = 30
    MAX_CLAIMS = 5    # claim_count < 5
    MAX_OPEN_PRS = 2  # open_pr_count < 2

    @classmethod
    def _layer1_pass(cls, o: dict) -> bool:
        return bool(o.get("reward_verified")) and float(o.get("reward_amount") or 0.0) >= cls.MIN_REWARD

    @classmethod
    def _layer3_pass(cls, o: dict) -> bool:
        blob = f"{o.get('title') or ''} {o.get('body') or ''}".lower()
        return not any(m in blob for m in PREFLIGHT_BLOCKED_MARKERS)

    @staticmethod
    def competition_level(o: dict) -> str:
        """EV 特征分级（4.5-B0.1 裁决：竞争度不再硬丢弃，改为特征信号）。"""
        claims = int(o.get("claim_count") or 0)
        if claims <= 1:
            return "LOW"
        if claims <= 4:
            return "MEDIUM"
        if claims <= 15:
            return "HIGH"
        return "SATURATED"

    @classmethod
    def evaluate(cls, opportunities: list, current_time_iso: str = None) -> dict:
        now = current_time_iso or datetime.utcnow().isoformat()
        total_raw = len(opportunities)

        layer1 = [o for o in opportunities if cls._layer1_pass(o)]

        layer2 = []
        for o in layer1:
            verdict = OpportunityFilter.evaluate(
                _to_filter_input(o, now),
                config={"max_issue_age_days": 10 ** 9},  # 年龄策略委托 Layer 4
                current_time_iso=now)
            if verdict["status"] == "CANDIDATE":
                layer2.append(o)

        layer3 = [o for o in layer2 if cls._layer3_pass(o)]

        # Layer 4 经济评估（4.5-B0.1 裁决）：
        #   - 竞争度（claim/open_pr）不再硬丢弃，改为 competition_level EV 特征保留；
        #   - 仅在"极端化石且完全无互动"时才丢弃（陈年且零认领零 PR 的悬赏）。
        layer4 = []
        stale_dropped = 0
        for o in layer3:
            last_act = _parse_iso(o.get("last_activity_at"))
            age_days = None
            if last_act is not None:
                now_dt = _parse_iso(now)
                if now_dt is not None:
                    age_days = (now_dt - last_act).days
            interaction = int(o.get("claim_count") or 0) + int(o.get("open_pr_count") or 0)
            fossil = (last_act is None) or (age_days is not None and age_days > cls.MAX_STALE_DAYS)
            if fossil and interaction == 0:
                stale_dropped += 1
                continue
            item = dict(o)
            item["competition_level"] = cls.competition_level(o)
            layer4.append(item)

        level_dist = {}
        for item in layer4:
            lv = item["competition_level"]
            level_dist[lv] = level_dist.get(lv, 0) + 1

        def _ratio(num: int, den: int) -> float:
            return (num / den) if den else 0.0

        return {
            "total_raw": total_raw,
            "reward_verified_count": len(layer1),
            "rule_qualified_count": len(layer2),
            "preflight_qualified_count": len(layer3),
            "economically_plausible_count": len(layer4),
            "funnel_densities": {
                "d_reward": _ratio(len(layer1), total_raw),
                "d_qualified": _ratio(len(layer2), len(layer1)),
                "d_preflight": _ratio(len(layer3), len(layer2)),
                "d_economic": _ratio(len(layer4), len(layer3))
            },
            "stale_bounties_dropped": stale_dropped,
            "high_competition_dropped": 0,  # 4.5-B0.1 起竞争度不再硬丢弃（保留键名兼容）
            "competition_level_distribution": level_dist,
            "economically_plausible": layer4
        }
