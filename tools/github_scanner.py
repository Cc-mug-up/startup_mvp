# tools/github_scanner.py
"""
Gate 4.4-A 真实网络感知层：纯读（Read-Only）GitHub Issue 扫描器。

铁律：绝对只使用 GET 方法，严禁任何 POST/PUT/DELETE/PATCH 写操作
（禁止 Fork/Commit/PR）。Token 仅存在宿主机进程内存，仅用于提高 API 限额。
"""
import requests
from tools.github_adapter import GitHubAdapter


class GitHubScanner:
    def __init__(self, token: str = None, timeout: int = 10):
        # Token 仅存在宿主机进程内存，仅用于提高 GitHub API 限额（5000次/h）
        self.token = token
        self.timeout = timeout
        self.base_url = "https://api.github.com"

    def scan_bounty_issues(self, query: str = "label:bounty is:issue is:open", max_results: int = 5) -> dict:
        """
        调用 GitHub Search Issues API (GET /search/issues)。
        逻辑链条：
        1. 发起 GET 请求，携带 User-Agent: Startup-MVP-Scanner。
        2. 拦截与解析响应头：读取 x-ratelimit-remaining 与 x-ratelimit-reset。
        3. 错误防御：
           - 若 HTTP 403/429 (Rate Limit): 优雅返回 rate_limited 契约，严禁抛未捕获异常崩溃。
           - 若网络超时/连接中断: 优雅捕获，返回受控错误字典。
        4. 协议清洗：
           - 调用 Gate 4.1 已落盘的 GitHubAdapter.parse_issue 进行单条清洗。
           - 过滤掉解析失败或异常的脏数据。
        5. 返回标准化感知契约。
        """
        url = f"{self.base_url}/search/issues"
        headers = {
            "User-Agent": "Startup-MVP-Scanner",
            "Accept": "application/vnd.github+json"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        params = {"q": query, "per_page": str(max_results)}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            return {"success": False, "error": f"Request timed out: {e}", "opportunities": []}
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"Network error: {e}", "opportunities": []}

        rate_remaining = resp.headers.get("x-ratelimit-remaining")
        reset_at = resp.headers.get("x-ratelimit-reset")

        # 限流拦截（403/429）
        if resp.status_code in (403, 429):
            return {
                "success": False,
                "rate_limited": True,
                "reset_at": reset_at,
                "rate_limit_remaining": rate_remaining,
                "opportunities": []
            }
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}", "opportunities": []}

        try:
            data = resp.json()
        except ValueError as e:
            return {"success": False, "error": f"Invalid JSON response: {e}", "opportunities": []}

        items = data.get("items", []) if isinstance(data, dict) else []

        opportunities = []
        for item in items[:max_results]:
            cleaned = GitHubAdapter.parse_issue(item)
            if cleaned.get("success"):
                opportunities.append(cleaned["discovered_opportunities"][0])
            # 解析失败/异常脏数据：静默过滤

        return {
            "success": True,
            "total_scanned": len(items),
            "rate_limit_remaining": rate_remaining,
            "opportunities": opportunities,
            # 附加原始条目（供 MarketObserver 做仓库富集；对既有契约为纯增量字段）
            "_raw_items": items[:max_results]
        }
