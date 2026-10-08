# tools/github_adapter.py
"""
Gate 4.1 静态协议适配器：GitHub Issue 原始 JSON -> Controller opportunity 结构。
纯内存数据转换，严禁任何网络 I/O、LLM 调用与容器依赖。
事实与推测严格隔离：只有结构化白名单（标签 / 行首显式指令）才算有效赏金事实。
"""
import re

# 标签形态白名单：bounty: $XX / reward: $XX / $XX（容忍中文冒号与大小写）
_BOUNTY_LABEL_RE = re.compile(r"^(?:bounty|reward)\s*[:：]?\s*\$(\d+(?:\.\d+)?)$", re.IGNORECASE)
_BARE_DOLLAR_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")

# 正文严格指令语法：仅行首的 /bounty $XX 或 Bounty: $XX
_BODY_SLASH_BOUNTY_RE = re.compile(r"(?im)^\s*/bounty\s+\$(\d+(?:\.\d+)?)\s*$")
_BODY_COLON_BOUNTY_RE = re.compile(r"(?im)^\s*bounty\s*[:：]\s*\$(\d+(?:\.\d+)?)\s*$")

_INVALID = {
    "success": False,
    "hard_cost": 0.0,
    "error": "Invalid issue schema",
    "discovered_opportunities": []
}


def _extract_owner_repo(issue_dict: dict, repo_full_name) -> str:
    if repo_full_name:
        return str(repo_full_name)
    # repository_url: https://api.github.com/repos/owner/repo
    m = re.search(r"repos/([^/]+)/([^/]+?)(?:/|$)", issue_dict.get("repository_url") or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # html_url: https://github.com/owner/repo/issues/123
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:/|$)", issue_dict.get("html_url") or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ""


def _extract_revenue(issue_dict: dict) -> float:
    # 白名单规则 1：标签（优先级最高，先命中先取）
    for label in issue_dict.get("labels") or []:
        if not isinstance(label, dict):
            continue
        name = str(label.get("name") or "").strip()
        m = _BOUNTY_LABEL_RE.match(name) or _BARE_DOLLAR_RE.match(name)
        if m:
            return float(m.group(1))
    # 白名单规则 2：正文行首显式指令
    body = issue_dict.get("body")
    if isinstance(body, str):
        m = _BODY_SLASH_BOUNTY_RE.search(body) or _BODY_COLON_BOUNTY_RE.search(body)
        if m:
            return float(m.group(1))
    # 归零规则：自然语言承诺（"Will pay $100" 等）不构成付费事实
    return 0.0


def _sanitize_segment(segment: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", segment.lower()).strip("_")


class GitHubAdapter:
    @staticmethod
    def parse_issue(issue_dict: dict, repo_full_name: str = None) -> dict:
        """
        纯内存数据转换函数，严禁发起任何网络 I/O。
        """
        try:
            if not isinstance(issue_dict, dict):
                return _INVALID
            number = issue_dict.get("number")
            if not isinstance(number, int):
                return _INVALID
            owner_repo = _extract_owner_repo(issue_dict, repo_full_name)
            if not owner_repo or "/" not in owner_repo:
                return _INVALID
            owner, repo = owner_repo.split("/", 1)
            opp_id = f"opp_gh_{_sanitize_segment(owner)}_{_sanitize_segment(repo)}_{number}"

            observed_revenue = _extract_revenue(issue_dict)

            return {
                "success": True,
                "hard_cost": 0.0,
                "discovered_opportunities": [
                    {
                        "opportunity_id": opp_id,
                        "source": "github_issue",
                        "observed_revenue": observed_revenue,
                        "requirements": ["submit_pr"],
                        "metadata": {
                            "html_url": str(issue_dict.get("html_url") or ""),
                            "title": str(issue_dict.get("title") or ""),
                            "number": number
                        }
                    }
                ]
            }
        except Exception:
            # 容错保障：任何未预期异常一律收敛为无效结构，绝不外抛
            return _INVALID
