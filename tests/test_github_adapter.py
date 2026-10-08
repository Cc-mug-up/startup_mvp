# tests/test_github_adapter.py
# Gate 4.1 确定性单测：全部使用静态 Mock 数据，零网络依赖
import pytest
from tools.github_adapter import GitHubAdapter

def _base_issue(**overrides):
    issue = {
        "number": 123,
        "title": "Fix parser crash",
        "body": "The parser crashes on empty input.",
        "html_url": "https://github.com/octocat/hello-world/issues/123",
        "repository_url": "https://api.github.com/repos/octocat/hello-world",
        "labels": []
    }
    issue.update(overrides)
    return issue

# 1. 标签带 bounty: $50 → observed_revenue == 50.0
def test_parse_explicit_label_bounty():
    issue = _base_issue(labels=[{"name": "bug"}, {"name": "bounty: $50"}])
    res = GitHubAdapter.parse_issue(issue)
    assert res["success"] is True
    assert res["discovered_opportunities"][0]["observed_revenue"] == 50.0

# 2. 正文包含 /bounty $25.5 → observed_revenue == 25.5
def test_parse_explicit_body_command():
    issue = _base_issue(body="Parser is broken.\n/bounty $25.5\nPlease fix asap.")
    res = GitHubAdapter.parse_issue(issue)
    assert res["success"] is True
    assert res["discovered_opportunities"][0]["observed_revenue"] == 25.5

# 3. 模糊承诺（"Will pay $100 if fixed quickly"）且无正规标签 → 0.0
def test_vague_language_falls_back_to_zero():
    issue = _base_issue(body="Will pay $100 if fixed quickly. Please help!")
    res = GitHubAdapter.parse_issue(issue)
    assert res["success"] is True
    assert res["discovered_opportunities"][0]["observed_revenue"] == 0.0

# 4. 普通 Bug Issue（无任何赏金信息）→ 0.0
def test_normal_issue_without_bounty():
    issue = _base_issue()
    res = GitHubAdapter.parse_issue(issue)
    assert res["success"] is True
    assert res["discovered_opportunities"][0]["observed_revenue"] == 0.0

# 5. 同一 Issue 多次解析，opportunity_id 必须字节级一致
def test_deterministic_opportunity_id():
    issue = _base_issue(labels=[{"name": "reward: $30"}])
    ids = {GitHubAdapter.parse_issue(issue)["discovered_opportunities"][0]["opportunity_id"]
           for _ in range(20)}
    assert len(ids) == 1
    assert ids == {"opp_gh_octocat_hello_world_123"}

# 6. 空字典或非法结构 → success=False，不崩溃
def test_malformed_input_graceful_handling():
    for bad in [{}, {"number": "not-an-int"}, None, [], "garbage"]:
        res = GitHubAdapter.parse_issue(bad)
        assert res["success"] is False
        assert res["error"] == "Invalid issue schema"
        assert res["discovered_opportunities"] == []

# 7. 特殊字符仓库名清洗（连字符/点 → 下划线，全小写）
def test_special_char_repo_sanitized_in_id():
    issue = _base_issue(
        repository_url="https://api.github.com/repos/My-Org/my.repo",
        html_url="https://github.com/My-Org/my.repo/issues/7",
        number=7
    )
    res = GitHubAdapter.parse_issue(issue)
    assert res["discovered_opportunities"][0]["opportunity_id"] == "opp_gh_my_org_my_repo_7"

# 8. 仓库解析优先级：repo_full_name 参数 > repository_url > html_url
def test_repo_full_name_param_has_priority():
    issue = _base_issue()
    res = GitHubAdapter.parse_issue(issue, repo_full_name="other/team-repo")
    assert res["discovered_opportunities"][0]["opportunity_id"] == "opp_gh_other_team_repo_123"

# 9. 标签优先级高于正文指令
def test_label_takes_precedence_over_body():
    issue = _base_issue(
        labels=[{"name": "bounty: $50"}],
        body="/bounty $25.5\n"
    )
    res = GitHubAdapter.parse_issue(issue)
    assert res["discovered_opportunities"][0]["observed_revenue"] == 50.0
