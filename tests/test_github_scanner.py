# tests/test_github_scanner.py
# Gate 4.4-A 确定性单测：真实网络感知层（静态 Mock 为主 + 1 个条件真实探测）
import os
import json
import pytest
import requests
from tools.github_scanner import GitHubScanner


class FakeResp:
    def __init__(self, status_code=200, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


def _bounty_item(num):
    return {
        "number": num,
        "title": "Fix parser crash",
        "body": "The parser crashes on empty input.",
        "html_url": f"https://github.com/octo/repo/issues/{num}",
        "repository_url": "https://api.github.com/repos/octo/repo",
        "labels": [{"name": "bug"}, {"name": "bounty: $50"}]
    }


# 1. 全流程解析管道：3 条可清洗结构 + 1 条真脏数据被过滤，收益精准映射，不崩溃
def test_search_issues_parsing_pipeline(monkeypatch):
    items = [
        _bounty_item(101),                                  # 有效赏金标签 -> 50.0
        {**{k: v for k, v in _bounty_item(102).items() if k != "labels"}, "labels": []},  # 普通 Issue -> 0.0
        {"number": 103, "repository_url": "https://api.github.com/repos/octo/repo"},       # 格式残缺（缺 title/labels）-> 0.0
        {"number": "not-an-int", "title": "broken"}                                        # 真脏数据 -> 过滤
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url == "https://api.github.com/search/issues"
        assert headers["User-Agent"] == "Startup-MVP-Scanner"
        assert params["q"] == "label:bounty is:issue is:open"
        return FakeResp(
            200,
            headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1756600000"},
            payload={"total_count": 4, "items": items}
        )

    monkeypatch.setattr("tools.github_scanner.requests.get", fake_get)

    res = GitHubScanner().scan_bounty_issues()

    assert res["success"] is True
    assert res["total_scanned"] == 4          # 原始扫描 4 条
    assert res["rate_limit_remaining"] == "4999"
    opps = res["opportunities"]
    assert len(opps) == 3                     # 真脏数据被过滤，其余 3 条全部清洗成功
    assert opps[0]["observed_revenue"] == 50.0
    assert opps[0]["opportunity_id"] == "opp_gh_octo_repo_101"
    assert opps[1]["observed_revenue"] == 0.0
    assert opps[2]["observed_revenue"] == 0.0
    assert opps[2]["opportunity_id"] == "opp_gh_octo_repo_103"  # 残缺数据不崩溃


# 2. 限流 403：优雅降级，rate_limited=True 且准确提取 reset_at
def test_rate_limit_403_graceful_handling(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return FakeResp(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1756600000"},
            payload={"message": "API rate limit exceeded"}
        )

    monkeypatch.setattr("tools.github_scanner.requests.get", fake_get)

    res = GitHubScanner().scan_bounty_issues()

    assert res["success"] is False
    assert res["rate_limited"] is True
    assert res["reset_at"] == "1756600000"
    assert res["rate_limit_remaining"] == "0"
    assert res["opportunities"] == []


# 3. 网络超时韧性：捕获 requests.exceptions.Timeout，返回受控错误字典
def test_network_timeout_resilience(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        raise requests.exceptions.Timeout("Connection to api.github.com timed out")

    monkeypatch.setattr("tools.github_scanner.requests.get", fake_get)

    res = GitHubScanner(timeout=10).scan_bounty_issues()

    assert res["success"] is False
    assert "timed out" in res["error"]
    assert res["opportunities"] == []


# 4. 条件真实连通性探测：有 GITHUB_TOKEN 才发起受限真实查询，否则 skip
def _load_github_token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def test_live_github_connectivity():
    token = _load_github_token()
    if not token:
        pytest.skip("GITHUB_TOKEN not configured; live connectivity probe skipped")
    scanner = GitHubScanner(token=token, timeout=15)
    res = scanner.scan_bounty_issues(query="is:issue is:open", max_results=1)
    assert res.get("success") is True, res
    assert res["total_scanned"] == 1
    assert len(res["opportunities"]) == 1  # 真实结构解析成功


# 5. 全套回归零破损（Gate 1 / Gate 4.2-C / Gate 4.3-A）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    rc1 = pytest.main([os.path.join(base, "test_security_bounds.py"), "-q"])
    rc2 = pytest.main([os.path.join(base, "test_waiting_resolution.py"), "-q"])
    rc3 = pytest.main([os.path.join(base, "test_settlement_flow.py"), "-q"])
    assert rc1 == 0 and rc2 == 0 and rc3 == 0
