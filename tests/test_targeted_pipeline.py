# tests/test_targeted_pipeline.py
# Gate 4.5-B1 管线与富集解耦测试套件（确定性 Mock / 零网络）
import os
import pytest
from tools.github_enricher import GitHubEnricher
from tools.targeted_pipeline import TargetedPipeline


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses  # {url: (status, payload)}
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        status, payload = self.responses.get(url, (404, {}))
        return FakeResp(status, payload)


def _observation(repo, issue_id="101", title="Fix null pointer in parser", reward=100.0):
    return {
        "source": "algora", "source_id": f"bnt_{issue_id}",
        "repo": repo, "issue_id": issue_id,
        "issue_url": f"https://github.com/{repo}/issues/{issue_id}",
        "title": title, "body": "",
        "reward_amount": reward, "reward_currency": "USD",
        "reward_verified": True,
        "bounty_created_at": "2026-09-01T00:00:00Z",
        "last_activity_at": "2026-10-02T00:00:00Z",
        "claim_count": 1, "open_pr_count": 0, "language": "", "labels": []
    }


# 1. 富集器语言三态：Python -> SUPPORTED，Go -> UNSUPPORTED，404/500 -> UNKNOWN
def test_enricher_correctly_classifies_language_states():
    transport = FakeTransport({
        "https://api.github.com/repos/acme/py-repo": (
            200, {"language": "Python", "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main"}),
        "https://api.github.com/repos/acme/go-repo": (
            200, {"language": "Go", "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main"}),
        "https://api.github.com/repos/acme/missing": (404, {}),
        "https://api.github.com/repos/acme/broken": (500, {}),
    })
    enricher = GitHubEnricher(token=None, transport=transport)
    now = "2026-10-04T00:00:00Z"

    py = enricher.enrich(_observation("acme/py-repo"), now_iso=now)
    assert py["language_status"] == "SUPPORTED"
    assert py["primary_language"] == "Python"
    assert py["repo_active_within_90d"] is True
    assert py["default_branch"] == "main"

    go = enricher.enrich(_observation("acme/go-repo"), now_iso=now)
    assert go["language_status"] == "UNSUPPORTED"
    assert go["primary_language"] == "Go"

    for repo in ("acme/missing", "acme/broken"):
        r = enricher.enrich(_observation(repo), now_iso=now)
        assert r["language_status"] == "UNKNOWN"  # API 失败 -> UNKNOWN，严禁混淆
        assert r["repo_active_within_90d"] is None


# 2. 混合漏斗：Go 样本在 Filter 层被 UNSUPPORTED_LANGUAGE 精准阻断，Python 晋级
def test_pipeline_funnel_with_go_and_python_mix():
    class FakeAlgora:
        name = "algora"

        def fetch_bounties(self, org):
            if org == "python-org":
                return {"success": True, "source_health": "SOURCE_OK", "items": [
                    {"id": "p1", "title": "Fix null pointer in parser", "body": "",
                     "repo": {"name": "acme/py-repo"},
                     "issue": {"number": 101, "url": "https://github.com/acme/py-repo/issues/101"},
                     "reward": {"amount": 100.0, "currency": "USD"},
                     "claimers": [1], "pull_requests": [], "created_at": "2026-09-01T00:00:00Z",
                     "last_activity_at": "2026-10-02T00:00:00Z", "language": "", "labels": []}]}
            return {"success": True, "source_health": "SOURCE_OK", "items": [
                {"id": "g1", "title": "Fix null pointer in parser", "body": "",
                 "repo": {"name": "acme/go-repo"},
                 "issue": {"number": 102, "url": "https://github.com/acme/go-repo/issues/102"},
                 "reward": {"amount": 100.0, "currency": "USD"},
                 "claimers": [1], "pull_requests": [], "created_at": "2026-09-01T00:00:00Z",
                 "last_activity_at": "2026-10-02T00:00:00Z", "language": "", "labels": []}]}

        def normalize(self, raw):
            return _observation(raw["repo"]["name"], issue_id=str(raw["issue"]["number"]))

    enricher = GitHubEnricher(token=None, transport=FakeTransport({
        "https://api.github.com/repos/acme/py-repo": (
            200, {"language": "Python", "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main"}),
        "https://api.github.com/repos/acme/go-repo": (
            200, {"language": "Go", "pushed_at": "2026-10-01T00:00:00Z", "default_branch": "main"}),
    }))

    pipeline = TargetedPipeline(
        org_pool=[{"org": "python-org", "ecosystem": "python"},
                  {"org": "go-org", "ecosystem": "go"}],
        algora_source=FakeAlgora(), enricher=enricher, current_time_iso="2026-10-04T00:00:00Z")
    report = pipeline.run()

    assert report["total_observations"] == 2
    # Python 晋级 -> CANDIDATE（唯一候选）
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["observation"]["language_status"] == "SUPPORTED"
    assert report["candidates"][0]["observation"]["repo"] == "acme/py-repo"
    # Go 样本在 Filter 层被 UNSUPPORTED_LANGUAGE 精准阻断
    assert report["funnel"]["rule_qualified_count"] == 1
    assert report["funnel"]["economically_plausible_count"] == 1
    assert report["funnel"]["reward_verified_count"] == 2


# 3. 只读纪律：静态扫描 + 运行时断言，管线严禁 POST/PUT/PATCH/DELETE/claim
def test_strict_read_only_discipline():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    banned = ["requests.post", "requests.put", "requests.patch", "requests.delete",
              ".post(", ".put(", ".patch(", ".delete("]
    for fname in ("tools/targeted_pipeline.py", "tools/github_enricher.py",
                  "tools/opportunity_sources/algora_source.py"):
        with open(os.path.join(base, fname), "r", encoding="utf-8") as f:
            src = f.read()
        for b in banned:
            assert b not in src, f"{fname} contains banned write call: {b}"

    # 运行时断言：富集器只发 GET
    transport = FakeTransport({})
    enricher = GitHubEnricher(token=None, transport=transport)
    enricher.enrich(_observation("acme/repo"), now_iso="2026-10-04T00:00:00Z")
    assert len(transport.calls) == 1
    assert transport.calls[0].startswith("https://api.github.com/repos/")  # 只读 repos 查询


# 4. 全量回归（Gate 1 -> 4.5-B0.1）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py", "test_human_approval_barrier.py",
              "test_targeted_opportunity_evaluator.py", "test_source_truth_calibration.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
