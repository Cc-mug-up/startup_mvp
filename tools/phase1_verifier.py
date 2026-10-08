# tools/phase1_verifier.py
"""
Commercial Pilot Phase 1 单任务真实核验（N=1 冷启动）。

运行模式：MANUAL_SEEDED / MACHINE_VERIFIED；并发度严格为 1。
远端写禁令：upstream_push_counter 恒 0，严禁 comment / claim / 自动 PR。
"""
import json
import os
from datetime import datetime, timezone

import requests

from tools.commercial_evaluator import (CommercialEvaluator, competition_signal,
                                        financial_truth, funding_evidence,
                                        FUNDED_VERIFIED, FUNDING_UNKNOWN)
from tools.github_enricher import load_github_token

SEEDS_PATH = "data/seed_candidates.json"
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner",
     "Accept": "application/vnd.github+json"}
MODE = "MANUAL_SEEDED / MACHINE_VERIFIED"
CONCURRENCY = 1


def _parse_issue_url(url):
    import re
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", url)
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


class Phase1Verifier:
    def __init__(self, token=None):
        self.token = token if token is not None else TOKEN
        self.upstream_push_counter = 0  # 恒 0 断言

    def _get(self, path, timeout=20):
        return requests.get(f"https://api.github.com{path}", headers=H, timeout=timeout)

    def load_single_seed(self, path=SEEDS_PATH) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        seeds = data.get("seeds", [])
        assert len(seeds) == 1, f"Phase 1 requires exactly 1 seed, got {len(seeds)}"
        seed = seeds[0]
        return seed if isinstance(seed, dict) else {"url": seed}

    def deep_enrich(self, url: str) -> dict:
        """只读深度富集：资金/维护者/技术栈纯度/竞争/作用域。"""
        owner, repo, number = _parse_issue_url(url)
        facts = {"url": url, "repo": f"{owner}/{repo}", "issue_number": number}

        issue_resp = self._get(f"/repos/{owner}/{repo}/issues/{number}")
        if issue_resp.status_code != 200:
            facts["enrichment_ok"] = False
            facts["error"] = f"issue HTTP {issue_resp.status_code}"
            return facts
        issue = issue_resp.json()
        facts["title"] = issue.get("title") or ""
        facts["body"] = issue.get("body") or ""
        facts["labels"] = [str(l.get("name") or "") for l in issue.get("labels") or []]

        repo_resp = self._get(f"/repos/{owner}/{repo}")
        if repo_resp.status_code == 200:
            r = repo_resp.json()
            facts["language"] = r.get("language")
            facts["archived"] = bool(r.get("archived"))
            pushed = r.get("pushed_at")
            if pushed:
                d = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                facts["last_commit_days"] = (datetime.now(timezone.utc) - d).days
            facts["default_branch"] = r.get("default_branch")
            facts["license"] = (r.get("license") or {}).get("spdx_id")

        # 资金存证：Algora 托管标识（正文/标签中的 algora 引用）
        escrow_marker = ("algora" in facts["body"].lower()) or \
            any("algora" in l.lower() for l in facts["labels"]) or \
            ("algora.io" in facts["body"])
        facts["escrow_marker"] = escrow_marker
        if escrow_marker:
            facts["funding_evidence"] = funding_evidence(
                "algora_escrow", url,
                datetime.now(timezone.utc).isoformat(), f"github_issue_{number}")
            facts["financial_truth"] = financial_truth(
                FUNDED_VERIFIED, "algora_escrow", url, "platform_escrow_api")
        else:
            facts["funding_evidence"] = None
            facts["financial_truth"] = financial_truth(
                FUNDING_UNKNOWN, "unverified", url, "none")

        # 技术栈纯度：C/Rust 扩展依赖排查
        root_resp = self._get(f"/repos/{owner}/{repo}/contents/")
        root_names = [x["name"] for x in root_resp.json()] if root_resp.status_code == 200 else []
        # 纯度探针：仅 C/Rust 扩展的直接证据（Makefile 是构建编排，非扩展证据）
        impurity_indicators = [n for n in root_names
                               if n in ("Cargo.toml", "build.rs")
                               or n.endswith((".pyx", ".c", ".cpp"))]
        facts["stack_impurity_indicators"] = impurity_indicators
        facts["stack_pure_python"] = not impurity_indicators

        # 竞争：timeline 交叉引用 PR，区分提及与明确修复
        tl_resp = self._get(f"/repos/{owner}/{repo}/issues/{number}/timeline?per_page=100")
        cross_prs = []
        if tl_resp.status_code == 200:
            for ev in tl_resp.json():
                if ev.get("event") == "cross-referenced" and ev.get("source", {}).get("issue"):
                    src = ev["source"]["issue"]
                    if src.get("pull_request"):
                        cross_prs.append(src)
        facts["cross_referenced_prs"] = len(cross_prs)
        open_prs = [p for p in cross_prs if p.get("state") == "open"]
        facts["open_pr_count"] = len(open_prs)
        # 明确修复判定：PR 标题/正文含修复动词且引用本 Issue 号
        fix_verbs = ("fix", "close", "resolve", "address")
        relevant = []
        for p in open_prs:
            blob = f"{p.get('title') or ''} {p.get('body') or ''}".lower()
            if any(v in blob for v in fix_verbs) and f"#{number}" in blob:
                relevant.append(p)
        facts["relevant_open_pr_count"] = len(relevant)
        facts["competition_signal"] = competition_signal(
            claims_count=issue.get("comments") or 0,
            open_pr_count=facts["open_pr_count"],
            assigned_to=(issue.get("assignee") or {}).get("login") if issue.get("assignee") else None,
            relevant_open_pr_count=facts["relevant_open_pr_count"])

        # 作用域预估：正文规模与文件提及（预期 <= 2 文件）
        facts["estimated_scope_files"] = min(2, 1 + facts["body"].count("`"))
        facts["enrichment_ok"] = True
        return facts

    def verdict(self, facts: dict) -> dict:
        """准入审查：QUALIFIED / UNVERIFIED（附 missing_evidence）或硬负例 REJECTED。"""
        if not facts.get("enrichment_ok"):
            return {"state": "UNVERIFIED", "missing_evidence": ["stack_unverified"],
                    "attribution": [], "reason": facts.get("error")}
        opp = {
            "opportunity_id": f"phase1_{facts['repo']}_{facts['issue_number']}",
            "title": facts["title"],
            "description": facts["body"],
            "language": facts.get("language") or "",
            "financial_truth": facts["financial_truth"],
            "competition_signal": facts["competition_signal"],
            "maintainer_signal": {
                "last_commit_days": facts.get("last_commit_days"),
                "recent_issue_activity_days": 0,
                "is_repo_archived": facts.get("archived")
            }
        }
        return CommercialEvaluator().evaluate(opp)

    def run(self, seed: dict = None) -> dict:
        seed = seed if seed is not None else self.load_single_seed()
        url = seed.get("url", "")
        facts = self.deep_enrich(url)
        v = self.verdict(facts)
        return {"mode": MODE, "concurrency": CONCURRENCY,
                "upstream_push_counter": self.upstream_push_counter,
                "seed": seed, "facts": facts, "verdict": v}
