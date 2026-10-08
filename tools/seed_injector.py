# tools/seed_injector.py
"""
Commercial Pilot 人工种子注入接口：读取 data/seed_candidates.json（1~10 个 URL），
经 GitHub API 只读富集（Issue 元数据/标签/语言/PR 竞态信号）后逐条评估，
落盘至 data/commercial_pilot_opportunities.json。严禁任何外部写操作。
"""
import json
import os
import re

import requests

from tools.commercial_evaluator import (CommercialEvaluator, OpportunityFunnel,
                                        competition_signal, financial_truth,
                                        FUNDING_UNKNOWN, FUNDING_UNVERIFIED,
                                        FUNDED_VERIFIED)
from tools.github_enricher import load_github_token

SEEDS_PATH = "data/seed_candidates.json"
OUT_PATH = "data/commercial_pilot_opportunities.json"

_ISSUE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")


class SeedInjector:
    def __init__(self, token=None, timeout=15):
        self.token = token if token is not None else load_github_token()
        self.timeout = timeout

    def _get(self, path):
        resp = requests.get(f"https://api.github.com{path}", timeout=self.timeout,
                            headers={"Authorization": f"Bearer {self.token}",
                                     "User-Agent": "Startup-MVP-Scanner",
                                     "Accept": "application/vnd.github+json"})
        return resp

    def load_seeds(self, path: str = SEEDS_PATH) -> list:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        seeds = []
        for s in data.get("seeds", []):
            if isinstance(s, dict):
                url = str(s.get("url") or "").strip()
                meta = {k: v for k, v in s.items() if k != "url"}
            else:
                url = str(s).strip()
                meta = {}
            if url:
                seeds.append({"url": url, "meta": meta})
        assert 0 <= len(seeds) <= 10, "seeds must be 0~10 URLs"
        return seeds

    def enrich(self, url: str) -> dict:
        """只读富集：Issue 元数据 + 标签 + 语言 + PR 竞态信号。"""
        m = _ISSUE_RE.search(url)
        if not m:
            return {"url": url, "enrichment_ok": False,
                    "error": "unsupported URL (expect github issue)"}
        owner, repo, number = m.group(1), m.group(2), m.group(3)
        issue_resp = self._get(f"/repos/{owner}/{repo}/issues/{number}")
        if issue_resp.status_code != 200:
            return {"url": url, "enrichment_ok": False, "error": f"issue HTTP {issue_resp.status_code}"}
        issue = issue_resp.json()
        repo_resp = self._get(f"/repos/{owner}/{repo}")
        language = (repo_resp.json().get("language") if repo_resp.status_code == 200 else None)

        # 托管背书证据：Algora 托管链接或 escrow 标签才视为可验证
        body = issue.get("body") or ""
        labels = [str(l.get("name") or "") for l in issue.get("labels") or []]
        escrow_marker = any("algora" in body.lower() for _ in [0]) or \
            any("algora" in l.lower() for l in labels) or \
            ("algora.io" in body)
        # 竞争信号：指派人与评论热度
        assignee = (issue.get("assignee") or {}).get("login") if issue.get("assignee") else None

        funding = FUNDED_VERIFIED if escrow_marker else FUNDING_UNKNOWN
        return {
            "url": url,
            "enrichment_ok": True,
            "repo": f"{owner}/{repo}",
            "issue_number": int(number),
            "title": issue.get("title") or "",
            "body": body,
            "labels": labels,
            "language": language,
            "issue_state": issue.get("state"),
            "escrow_marker": escrow_marker,
            "assignee": assignee,
            "financial_truth": financial_truth(
                funding,
                "algora_escrow" if escrow_marker else "unverified",
                url,
                "platform_escrow_api" if escrow_marker else "none"),
            "competition_signal": competition_signal(
                claims_count=issue.get("comments") or 0,
                open_pr_count=0,
                assigned_to=assignee)
        }

    def run(self, seeds: list = None, out_path: str = OUT_PATH) -> dict:
        """批量注入 -> 富集 -> 评估 -> 漏斗统计 -> 落盘。"""
        seeds = seeds if seeds is not None else self.load_seeds()
        funnel = OpportunityFunnel()
        evaluator = CommercialEvaluator()
        records = []
        for seed in seeds:
            if isinstance(seed, str):  # 兼容纯字符串种子
                seed = {"url": seed, "meta": {}}
            url = seed["url"]
            meta = seed.get("meta", {})
            funnel.record_discovered()
            enriched = self.enrich(url)
            if not enriched.get("enrichment_ok"):
                records.append({"url": url, "seed_meta": meta, "state": "UNVERIFIED",
                                "missing_evidence": ["stack_unverified"],
                                "enrichment_error": enriched.get("error")})
                funnel.record_verdict({"state": "UNVERIFIED",
                                       "missing_evidence": ["stack_unverified"],
                                       "attribution": []})
                continue
            verdict = evaluator.evaluate({
                "opportunity_id": f"seed_{enriched['repo']}_{enriched['issue_number']}",
                "title": enriched["title"],
                "description": enriched["body"],
                "language": enriched["language"],
                "financial_truth": enriched["financial_truth"],
                "competition_signal": enriched["competition_signal"]
            })
            funnel.record_verdict(verdict)
            records.append({"url": url, "seed_meta": meta, **enriched, "verdict": verdict})

        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        return {"seeds": len(seeds), "records": records, "funnel": funnel.to_dict()}
