# tools/market_observer.py
"""
Gate 4.5-A0 真实市场勘探层（纯只读，零写操作）。

串联 GitHubScanner -> GitHubAdapter -> OpportunityFilter：
- 仅调用 GET /search/issues 与 GET /repos/{owner}/{repo}，严禁任何写操作；
- reward_explicit 仅认可结构化标签/指令提取出的赏金，口头承诺一律 False；
- 每条观察输出 19 字段事实契约；
- calculate_market_metrics 计算草场密度与剔除原因分布。
"""
import json
import os
import re
from datetime import datetime, timezone
import requests
from tools.github_scanner import GitHubScanner
from tools.github_adapter import GitHubAdapter
from tools.opportunity_filter import OpportunityFilter

# 19 字段事实契约
OBSERVATION_FIELDS = [
    "observation_id", "observed_at", "opportunity_id", "source", "repo", "issue_number",
    "title", "html_url", "issue_state", "issue_updated_at", "issue_age_days", "language",
    "repo_active_within_90d", "reward_explicit", "reward", "filter_status",
    "discard_reasons", "scope_tag", "hard_filter"
]


class MarketObserver:
    def __init__(self, scanner=None, token=None, timeout=10):
        self.scanner = scanner or GitHubScanner(token=token, timeout=timeout)
        self.token = token
        self.timeout = timeout
        self.base_url = "https://api.github.com"
        self.observations = []

    # ------------------------------------------------------------------ #
    # 纯函数：单条观察构建（单元测试直接调用，零网络）                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _derive_repo(issue: dict) -> str:
        m = re.search(r"repos/([^/]+/[^/]+?)(?:/|$)", issue.get("repository_url") or "") or \
            re.search(r"github\.com/([^/]+/[^/]+?)(?:/|$)", issue.get("html_url") or "")
        return m.group(1) if m else ""

    @staticmethod
    def build_observation(issue_meta: dict, now_iso: str) -> dict:
        """issue_meta = {"issue_json": {...}, "enrichment": {state/updated_at/language/repo_active_within_90d}}"""
        issue = issue_meta.get("issue_json") or {}
        env = issue_meta.get("enrichment") or {}

        cleaned = GitHubAdapter.parse_issue(issue)
        opp = cleaned["discovered_opportunities"][0] if cleaned.get("success") else None
        reward = float(opp["observed_revenue"]) if opp else 0.0
        reward_explicit = reward > 0.0  # 适配器严格提取成功才为 True，口头承诺已归零

        state = env.get("state", issue.get("state"))
        updated_at = env.get("updated_at", issue.get("updated_at"))
        language = env.get("language")
        repo_active = env.get("repo_active_within_90d")
        repo = MarketObserver._derive_repo(issue)

        if opp is not None:
            opp["metadata"]["state"] = state
            opp["metadata"]["updated_at"] = updated_at
            opp["metadata"]["language"] = language
            opp["metadata"]["repo_active_within_90d"] = repo_active
            opp["metadata"]["labels"] = issue.get("labels") or []
            opp["description"] = issue.get("body") or ""
            verdict = OpportunityFilter.evaluate(opp, current_time_iso=now_iso)
            status = verdict["status"]
            reasons = verdict["discard_reasons"]
            hard = verdict["hard_filter"]
            scope_tag = verdict["features"]["scope_tag"]
            age_days = verdict["features"]["days_since_update"]
            opp_id = opp["opportunity_id"]
        else:
            status = "DISCARDED"
            reasons = ["INVALID_SCHEMA"]
            hard = {"revenue_pass": False, "active_pass": False, "scope_pass": False, "stack_pass": False}
            scope_tag = "none"
            age_days = -1
            opp_id = ""

        return {
            "observation_id": f"obs_{repo.replace('/', '_')}_{issue.get('number', 'unknown')}",
            "observed_at": now_iso,
            "opportunity_id": opp_id,
            "source": "github_issue",
            "repo": repo,
            "issue_number": issue.get("number"),
            "title": str(issue.get("title") or ""),
            "html_url": str(issue.get("html_url") or ""),
            "issue_state": state,
            "issue_updated_at": updated_at,
            "issue_age_days": age_days,
            "language": language,
            "repo_active_within_90d": repo_active,
            "reward_explicit": reward_explicit,
            "reward": reward,
            "filter_status": status,
            "discard_reasons": reasons,
            "scope_tag": scope_tag,
            "hard_filter": hard
        }

    # ------------------------------------------------------------------ #
    # 指标汇总                                                              #
    # ------------------------------------------------------------------ #
    def calculate_market_metrics(self) -> dict:
        total = len(self.observations)
        explicit = sum(1 for o in self.observations if o["reward_explicit"])
        candidates = sum(1 for o in self.observations if o["filter_status"] == "CANDIDATE")
        density = (candidates / total) if total else 0.0
        dist = {}
        for o in self.observations:
            for r in o["discard_reasons"]:
                dist[r] = dist.get(r, 0) + 1
        return {
            "total_raw_count": total,
            "explicit_reward_count": explicit,
            "candidate_count": candidates,
            "opportunity_density": density,
            "discard_reason_distribution": dist
        }

    # ------------------------------------------------------------------ #
    # 真实受限扫描（只读；仓库富集受预算约束，绝不超发）                        #
    # ------------------------------------------------------------------ #
    def _enrich_repo(self, raw: dict, budget: int, now_iso: str) -> tuple:
        base = {
            "state": raw.get("state"),
            "updated_at": raw.get("updated_at"),
            "language": None,
            "repo_active_within_90d": None
        }
        if budget <= 0:
            return base, False
        repo = self._derive_repo(raw)
        if not repo:
            return base, False
        try:
            headers = {"User-Agent": "Startup-MVP-Scanner", "Accept": "application/vnd.github+json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            resp = requests.get(f"{self.base_url}/repos/{repo}", headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                base["language"] = data.get("language")
                pushed = data.get("pushed_at")
                if pushed:
                    d = datetime.fromisoformat(str(pushed).replace("Z", "+00:00"))
                    now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                    base["repo_active_within_90d"] = (now - d).days <= 90
        except requests.exceptions.RequestException:
            pass  # 富集失败保持未知事实，绝不影响主流程
        return base, True

    def observe_live(self, queries: list, per_query: int = 30,
                     enrich_repo_limit: int = 10, now_iso: str = None) -> dict:
        now = now_iso or datetime.now(timezone.utc).isoformat()
        seen = {o["opportunity_id"] for o in self.observations if o["opportunity_id"]}
        report = {"queries": [], "errors": []}
        repo_budget = enrich_repo_limit
        for q in queries:
            scan = self.scanner.scan_bounty_issues(query=q, max_results=per_query)
            report["queries"].append({
                "query": q,
                "success": bool(scan.get("success")),
                "rate_limited": bool(scan.get("rate_limited")),
                "total_scanned": scan.get("total_scanned")
            })
            if not scan.get("success"):
                report["errors"].append(scan)
                continue
            for raw in scan.get("_raw_items", []):
                enrichment, attempted = self._enrich_repo(raw, repo_budget, now)
                if attempted:
                    repo_budget -= 1
                obs = self.build_observation({"issue_json": raw, "enrichment": enrichment}, now)
                if obs["opportunity_id"] and obs["opportunity_id"] in seen:
                    continue  # 跨查询去重
                seen.add(obs["opportunity_id"])
                self.observations.append(obs)
        metrics = self.calculate_market_metrics()
        metrics["query_report"] = report
        return metrics

    def save_observations(self, out_path: str) -> int:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for obs in self.observations:
                f.write(json.dumps(obs, ensure_ascii=False) + "\n")
        return len(self.observations)
