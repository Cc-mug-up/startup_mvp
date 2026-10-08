# tools/github_enricher.py
"""
Gate 4.5-B1 GitHub 仓库元数据富集器（只读查询，零写操作）。

输入：标准化 observation（含 repo="owner/name" 或可解析的 issue_url）
调用：GET /repos/{owner}/{repo}（优先 .env 的 GITHUB_TOKEN，支持匿名降级）
输出富集事实：
  - primary_language: repo.language
  - language_status: "SUPPORTED"（Python）| "UNSUPPORTED"（明确非 Python）| "UNKNOWN"（API 失败/无语言）
  - repo_active_within_90d: pushed_at 距今 <= 90 天（API 失败为 None）
  - default_branch: repo.default_branch
"""
import os
import re
from datetime import datetime, timezone
import requests

GITHUB_API_BASE = "https://api.github.com"
USER_AGENT = "Startup-MVP-Scanner"


def _parse_iso(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def load_github_token() -> str:
    """仅宿主机内存使用；优先环境变量，其次项目 .env。"""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


class GitHubEnricher:
    def __init__(self, token: str = None, timeout: int = 10, transport=None):
        self.token = token if token is not None else load_github_token()  # 仅内存
        self.timeout = timeout
        self.transport = transport or requests

    @staticmethod
    def _parse_repo(observation: dict):
        repo = (observation.get("repo") or "").strip()
        m = re.match(r"^([^/]+)/([^/]+)$", repo)
        if m:
            return m.group(1), m.group(2)
        m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:/|$)", observation.get("issue_url") or "")
        if m:
            return m.group(1), m.group(2)
        return None, None

    def enrich(self, observation: dict, now_iso: str = None) -> dict:
        """只读富集：绝不修改原 observation 的其他事实，仅注入元数据。"""
        enriched = dict(observation)
        enriched["primary_language"] = None
        enriched["language_status"] = "UNKNOWN"
        enriched["repo_active_within_90d"] = None
        enriched["default_branch"] = None
        enriched["enrichment"] = {"ok": False, "http_status": None, "error": None}

        owner, repo = self._parse_repo(observation)
        if not owner or not repo:
            enriched["enrichment"]["error"] = "unresolvable repo"
            return enriched

        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self.transport.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}",
                                      headers=headers, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            enriched["enrichment"]["error"] = f"network error: {e}"
            return enriched

        enriched["enrichment"]["http_status"] = resp.status_code
        if resp.status_code != 200:
            enriched["enrichment"]["error"] = f"HTTP {resp.status_code}"
            return enriched

        data = resp.json()
        enriched["enrichment"]["ok"] = True
        lang = data.get("language")
        if lang:
            enriched["primary_language"] = str(lang)
            enriched["language_status"] = "SUPPORTED" if str(lang).lower() == "python" else "UNSUPPORTED"
        enriched["default_branch"] = data.get("default_branch")

        pushed = data.get("pushed_at")
        if pushed:
            pushed_dt = _parse_iso(pushed)
            now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
            if pushed_dt and now_dt:
                enriched["repo_active_within_90d"] = (now_dt - pushed_dt).days <= 90
        return enriched
