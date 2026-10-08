# tools/targeted_pipeline.py
"""
Gate 4.5-B1 只读总控流水线：OrgPool -> AlgoraSource -> GitHubEnricher -> Filter -> 轻量预检。

铁律：全程纯只读（仅 GET），严禁任何 POST/PUT/PATCH/DELETE、严禁认领（claim）。
若探测到 Python Candidate：仅打印 Observation Record，严禁发起外部执行或 PR。
"""
import json
import os
from tools.opportunity_sources.python_org_pool import PYTHON_ORG_POOL
from tools.opportunity_sources.algora_source import AlgoraSource
from tools.opportunity_sources.base_source import OpportunitySource
from tools.github_enricher import GitHubEnricher
from tools.opportunity_filter import OpportunityFilter
from tools.market_funnel_evaluator import MarketFunnelEvaluator

# 轻量静态预检黑名单标记（与漏斗 Layer 3 同一事实口径；完整沙箱预检需仓库供给后另行触发）
STATIC_PREFLIGHT_BLOCKED_MARKERS = [
    "gpu", "cuda", "proprietary sdk", "api key required", "docker-in-docker", "fpga"
]


def _to_filter_input(o: dict, now_iso: str) -> dict:
    return {
        "opportunity_id": f"{o.get('source', 'src')}_{o.get('source_id', 'x')}",
        "observed_revenue": float(o.get("reward_amount") or 0.0),
        "description": o.get("body") or "",
        "metadata": {
            "title": o.get("title") or "",
            "state": "open",
            "updated_at": o.get("last_activity_at") or now_iso,
            "language": o.get("primary_language") or o.get("language") or None,
            "labels": [{"name": x} for x in (o.get("labels") or [])],
            "repo_active_within_90d": o.get("repo_active_within_90d")
        }
    }


def _static_preflight(o: dict) -> str:
    blob = f"{o.get('title') or ''} {o.get('body') or ''}".lower()
    if any(m in blob for m in STATIC_PREFLIGHT_BLOCKED_MARKERS):
        return "STATIC_BLOCKED"
    return "STATIC_PASS"


class TargetedPipeline:
    def __init__(self, org_pool=None, algora_source=None, enricher=None, current_time_iso=None):
        self.org_pool = org_pool or PYTHON_ORG_POOL
        self.algora = algora_source or AlgoraSource()
        self.enricher = enricher or GitHubEnricher()
        self.now = current_time_iso

    def run(self, out_path: str = None) -> dict:
        from datetime import datetime, timezone
        now = self.now or datetime.now(timezone.utc).isoformat()

        observations = []
        org_report = []
        errors = []
        for entry in self.org_pool:
            org = entry["org"]
            r = self.algora.fetch_bounties(org)
            org_row = {"org": org, "ecosystem": entry.get("ecosystem"),
                       "ok": r["success"], "count": len(r.get("items", [])),
                       "source_health": r.get("source_health")}
            org_report.append(org_row)
            if not r["success"]:
                errors.append({"org": org, "error": r.get("error")})
                continue
            for raw in r["items"]:
                try:
                    opp = self.algora.normalize(raw)
                    if not OpportunitySource.validate(opp):
                        continue
                except Exception:
                    continue
                observations.append(self.enricher.enrich(opp, now_iso=now))

        records = []
        candidates = []
        for o in observations:
            verdict = OpportunityFilter.evaluate(_to_filter_input(o, now), current_time_iso=now)
            preflight = _static_preflight(o)
            is_candidate = (verdict["status"] == "CANDIDATE" and preflight == "STATIC_PASS")
            record = {
                "observation": o,
                "filter_status": verdict["status"],
                "discard_reasons": verdict["discard_reasons"],
                "language_status": o.get("language_status"),
                "preflight": preflight,
                "candidate": is_candidate
            }
            records.append(record)
            if is_candidate:
                candidates.append(record)

        # 漏斗测量（语言事实已富集，Layer 2 可对 Python 样本放行）
        funnel_opps = []
        for o in observations:
            fo = dict(o)
            fo["language"] = o.get("primary_language") or o.get("language") or ""
            fo["last_activity_at"] = o.get("last_activity_at") or now
            funnel_opps.append(fo)
        funnel = MarketFunnelEvaluator.evaluate(funnel_opps, current_time_iso=now)

        report = {
            "pipeline": "targeted_pipeline_4.5b1",
            "read_only": True,
            "org_report": org_report,
            "errors": errors,
            "total_observations": len(observations),
            "candidate_count": len(candidates),
            "funnel": funnel,
            "candidates": candidates
        }

        if out_path:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            report["out_path"] = out_path
            report["records_written"] = len(records)
        return report
