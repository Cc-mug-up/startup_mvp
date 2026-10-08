# tools/opportunity_sources/algora_source.py
"""
Gate 4.5-B0.1 校准后的 Algora 结构化源适配器（纯只读 / 零认领 / 零评论 / 零写入）。

校准结论（诊断脚本 diagnose_algora.py 排查，Ground Truth = projectdiscovery 2 悬赏）：
- tRPC 路由 bounty.list 协议正确（合法响应结构），但参数语义未能命中真实数据
  （全部字段名/status 组合返回 0 条）——此前"眼睛没对准现实"的偏差所在。
- 真实公开数据通道 = 官网公开页 https://console.algora.io/org/{org}/bounties
  （Phoenix LiveView 服务器渲染 HTML，无鉴权，实测包含 nuclei#6674「33 claims」
  与 nuclei#6532「21 claims」）。
- 故：HTML 页面为主通道，tRPC 为降级通道。
- source_health：SOURCE_OK | SOURCE_EMPTY | SOURCE_INCONSISTENT | SOURCE_DEGRADED
  （expected_min 校准时，抓取量低于已知 Ground Truth -> SOURCE_INCONSISTENT，
   禁止据此产出错误的 Density=0）。
"""
import json
import re
import urllib.parse
import requests
from tools.opportunity_sources.base_source import OpportunitySource


class AlgoraSource(OpportunitySource):
    name = "algora"
    TRPC_URL = "https://console.algora.io/api/trpc/bounty.list"
    PAGE_URL = "https://console.algora.io/org/{org}/bounties"
    USER_AGENT = "Mozilla/5.0 (Startup-MVP-Scanner calibration v0.1)"

    DEFAULT_ORGS = [
        "cal", "supabase", "projectdiscovery", "calcom", "vercel",
        "unkeyed", "openstatus", "dubinc", "t3-oss", "electric-sql"
    ]

    def __init__(self, timeout: int = 15, transport=None):
        self.timeout = timeout
        self.transport = transport or requests  # 测试注入桩；默认真实 requests

    # ------------------------------------------------------------------ #
    # 抓取通道
    # ------------------------------------------------------------------ #
    def _get(self, url: str):
        return self.transport.get(url, timeout=self.timeout,
                                  headers={"User-Agent": self.USER_AGENT})

    @staticmethod
    def _parse_bounty_rows(html: str) -> list:
        """公开页服务器渲染 HTML -> 悬赏原始条目（真实数据通道解析）。"""
        items = []
        for row in re.findall(r'<tr [^>]*>.*?</tr>', html, re.S):
            m = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', row)
            if not m:
                continue
            repo, num = m.group(1), m.group(2)
            amount_m = re.search(r'\$\s*([\d,]+(?:\.\d+)?)', row)
            amount = float(amount_m.group(1).replace(',', '')) if amount_m else 0.0
            title_m = re.search(r'text-gray-100[^>]*>\s*([^<]{3,200})', row)
            title = title_m.group(1).strip() if title_m else ""
            claims_m = re.search(r'(\d+)\s*claims', row)
            claims = int(claims_m.group(1)) if claims_m else 0
            items.append({
                "id": f"{repo}#{num}",
                "title": title,
                "body": "",
                "repo": {"name": repo},
                "issue": {"number": int(num),
                          "url": f"https://github.com/{repo}/issues/{num}"},
                "reward": {"amount": amount, "currency": "USD"},
                "claimers": [None] * claims,
                "pull_requests": [],
                "created_at": "",
                "last_activity_at": "",
                "language": "",
                "labels": []
            })
        return items

    def _fetch_trpc(self, org: str, limit: int, status: str, expected_min) -> dict:
        """tRPC 降级通道（协议已验证，参数语义未校准）。"""
        input_payload = {"org": org, "limit": int(limit)}
        if status:
            input_payload["status"] = status
        url = f"{self.TRPC_URL}?input=" + urllib.parse.quote(json.dumps({"json": input_payload}))
        try:
            resp = self._get(url)
        except requests.exceptions.RequestException as e:
            return {"success": False, "source_health": "SOURCE_DEGRADED",
                    "channel": "trpc", "error": f"Network error: {e}", "items": []}
        if resp.status_code != 200:
            return {"success": False, "source_health": "SOURCE_DEGRADED",
                    "channel": "trpc", "error": f"HTTP {resp.status_code}", "items": []}
        try:
            payload = resp.json()
            items = payload[0]["result"]["data"]["json"].get("items", [])
        except (ValueError, KeyError, IndexError, TypeError) as e:
            return {"success": False, "source_health": "SOURCE_DEGRADED",
                    "channel": "trpc", "error": f"Unexpected response shape: {e}", "items": []}
        return self._health_result(items, "trpc", expected_min)

    @staticmethod
    def _health_result(items: list, channel: str, expected_min) -> dict:
        health = "SOURCE_OK" if items else "SOURCE_EMPTY"
        if expected_min is not None and len(items) < int(expected_min):
            health = "SOURCE_INCONSISTENT"
        return {"success": True, "source_health": health, "channel": channel,
                "error": None, "items": items}

    def fetch_bounties(self, org: str, limit: int = 30, status: str = None,
                       expected_min: int = None) -> dict:
        """单 org 悬赏列表（只读）。HTML 公开页为主通道，tRPC 降级。"""
        try:
            resp = self._get(self.PAGE_URL.format(org=org))
        except requests.exceptions.RequestException:
            return self._fetch_trpc(org, limit, status, expected_min)
        if resp.status_code == 200:
            items = self._parse_bounty_rows(resp.text)
            return self._health_result(items, "html", expected_min)
        return self._fetch_trpc(org, limit, status, expected_min)

    def fetch_market(self, orgs: list = None, limit_per_org: int = 30,
                     expected_min: dict = None) -> dict:
        """遍历 org 聚合采样（纯只读）。expected_min: {org: 已知最低样本数} 用于校准。"""
        report = {"success": True, "raw_items": [], "errors": [], "org_report": {},
                  "source_health": "SOURCE_OK", "evaluable": True, "alert": None}
        exp = expected_min or {}
        for org in (orgs or self.DEFAULT_ORGS):
            r = self.fetch_bounties(org, limit=limit_per_org, expected_min=exp.get(org))
            report["org_report"][org] = {
                "ok": r["success"], "count": len(r.get("items", [])),
                "source_health": r.get("source_health"), "error": r.get("error")
            }
            if not r["success"]:
                report["errors"].append({"org": org, "error": r.get("error")})
                continue
            if r.get("source_health") == "SOURCE_INCONSISTENT":
                report["source_health"] = "SOURCE_INCONSISTENT"
            report["raw_items"].extend(r["items"])
        # 校准告警截断：已知存在数据但通道返回不足 -> 禁止产出错误的 Density=0
        if report["source_health"] == "SOURCE_INCONSISTENT":
            report["evaluable"] = False
            report["alert"] = ("SOURCE_INCONSISTENT: fetched below known ground truth. "
                               "Funnel evaluation suppressed to avoid false zero density.")
        return report

    # ------------------------------------------------------------------ #
    # 契约归一化
    # ------------------------------------------------------------------ #
    def normalize(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("Algora raw item must be a dict")

        def _pick(*keys, default=None):
            for k in keys:
                if isinstance(k, str) and k in raw and raw[k] is not None:
                    return raw[k]
            return default

        reward = _pick("reward", "bounty", default={}) or {}
        repo = _pick("repo", default={}) or {}
        issue = _pick("issue", default={}) or {}

        if isinstance(reward, dict) and "amount_in_cents" in reward:
            amount = float(reward["amount_in_cents"]) / 100.0
        elif isinstance(reward, dict) and reward.get("amount") is not None:
            amount = float(reward["amount"])
        else:
            amount = float(_pick("amount", "reward_amount", default=0) or 0)

        currency = (reward.get("currency") if isinstance(reward, dict) else None) \
            or _pick("currency", "reward_currency", default="USD") or "USD"

        claimers = _pick("claimers", "claims", default=[]) or []
        prs = _pick("pull_requests", "prs", "submissions", default=[]) or []

        repo_name = repo.get("name") if isinstance(repo, dict) else str(repo)
        issue_num = issue.get("number") if isinstance(issue, dict) else issue

        return {
            "source": self.name,
            "source_id": str(_pick("id", "bounty_id", default="") or ""),
            "repo": str(repo_name or ""),
            "issue_id": str(issue_num) if issue_num is not None else "",
            "issue_url": str(_pick("issue_url", default=issue.get("url", "") if isinstance(issue, dict) else "") or ""),
            "title": str(_pick("title", default="") or ""),
            "body": str(_pick("body", "description", default="") or ""),
            "reward_amount": amount,
            "reward_currency": str(currency),
            "reward_verified": True,  # Algora 平台官方托管背书
            "bounty_created_at": str(_pick("created_at", "createdAt", default="") or ""),
            "last_activity_at": str(_pick("last_activity_at", "activity_at", "updated_at", "updatedAt",
                                          default="") or ""),
            "claim_count": int(len(claimers) if isinstance(claimers, (list, dict)) else (claimers or 0)),
            "open_pr_count": int(len(prs) if isinstance(prs, (list, dict)) else (prs or 0)),
            "language": str(_pick("language", "tech", default="") or ""),
            "labels": [str(x) for x in (_pick("labels", "tags", default=[]) or [])]
        }
