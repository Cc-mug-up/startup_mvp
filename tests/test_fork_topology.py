# tests/test_fork_topology.py
# Gate 4.5-B3-2 跨仓库 Fork 拓扑确定性单测（纯 Mock / 零网络 / 零真实推送）
import os
from types import SimpleNamespace

import pytest

from tools.approval_gate import BaseShaDriftException, DestinationMismatchError
from tools.b3_fork_driver import (ForkFlow, ForkPusher, FSM_TRANSITIONS,
                                  UPSTREAM_REPO, FORK_REPO)

UP = "org-lab/aie-b3-upstream"
FORK = "cc-user/aie-b3-upstream"
BASE = "aaaabbbbccccddddeeeeffff0000111122223333"
COMMIT = "9999888877776666555544443333222211110000"
BRANCH = "agent/fix-issue-3"
DIFF = ("--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 2\n")


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._p = payload

    def json(self):
        return self._p


class FakeAPI:
    def __init__(self, get_map=None):
        self.get_map = get_map or {}
        self.posts = []
        self.post_result = {}

    def get(self, path, timeout=20):
        return FakeResp(200, self.get_map.get(path, {}))

    def post(self, path, payload, timeout=20):
        self.posts.append((path, payload))
        return FakeResp(201, self.post_result.get(path, {"number": 7, "html_url": "https://x/pr/7"}))


def _fake_sandbox(commit=COMMIT):
    def exec_fn(cmd, timeout=60):
        if "git rev-parse upstream/main" in cmd:
            return 0, f"ok\n{BASE}", ""
        if "git rev-parse HEAD" in cmd:
            return 0, f"ok\n{commit}", ""
        return 0, "ok", ""
    return exec_fn


def _fake_git_run():
    def run(cmd, capture_output=True, text=True, timeout=60):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def _flow(api, pusher=None):
    return ForkFlow(UP, FORK, "tok", api=api,
                    pusher=pusher or ForkPusher(FORK, UP, "tok"),
                    sandbox_exec=_fake_sandbox())


# 1. 拓扑分离：upstream / fork 独立顶级结构 + fork 审计元数据
def test_topology_separation_structs():
    api = FakeAPI({f"/repos/{FORK}": {"full_name": FORK},
                   f"/repos/{UP}/branches/main": {"commit": {"sha": BASE}}})
    flow = _flow(api)
    flow.ensure_fork()

    st = flow.state
    assert set(st["upstream"]) == {"repo", "default_branch", "approved_base_sha",
                                   "pr_number", "readonly_url"}
    assert set(st["fork"]) == {"repo", "created_at", "source_sha", "branch", "push_url"}
    assert st["upstream"]["repo"] == UP and st["fork"]["repo"] == FORK
    assert st["fork"]["source_sha"] == BASE  # fork 审计锚
    assert st["fork"]["created_at"] is not None


# 2. 上游推送物理拦截：upstream_push_counter 恒 0，违者抛 DestinationMismatchError
def test_upstream_push_blocked_and_counter():
    pusher = ForkPusher(FORK, UP, "tok")
    r = pusher.push(FORK, BRANCH, COMMIT, "/tmp/work", git_run=_fake_git_run())
    assert r["success"] is True
    assert pusher.successful_pushes == 1
    assert pusher.upstream_push_counter == 0

    with pytest.raises(DestinationMismatchError):
        pusher.push(UP, "main", COMMIT, "/tmp/work", git_run=_fake_git_run())
    assert pusher.upstream_push_counter == 1  # 违规尝试被计数（作为证据）
    assert pusher.successful_pushes == 1  # 但成功写入仍只有 fork 一次


# 3. 推送上行物理禁用：sync 阶段必须配置 no_push 上游远端
def test_upstream_remote_pushurl_disabled():
    api = FakeAPI({f"/repos/{FORK}": {}, f"/repos/{UP}/branches/main": {"commit": {"sha": BASE}}})
    flow = _flow(api)
    flow.ensure_fork()
    seen = {}

    def exec_fn(cmd, timeout=60):
        seen["cmd"] = cmd
        return 0, f"ok\n{BASE}", ""
    flow.sandbox_exec = exec_fn
    flow.sync()
    assert "git remote set-url --push upstream no_push://upstream_disabled" in seen["cmd"]
    assert "git remote add upstream https://github.com/" + UP in seen["cmd"]


# 4. 四重 SHA：三 SHA 全等 PASS + LOCK-3 base 锁定
def test_quad_sha_full_pass():
    get_map = {
        f"/repos/{UP}/branches/main": {"commit": {"sha": BASE}},
        f"/repos/{FORK}/branches/{BRANCH}": {"commit": {"sha": COMMIT}},
        f"/repos/{UP}/pulls/7": {"number": 7, "html_url": "https://x/pr/7",
                                 "head": {"sha": COMMIT}, "base": {"sha": BASE}},
    }
    api = FakeAPI(get_map)
    flow = _flow(api)
    flow.state["upstream"]["approved_base_sha"] = BASE
    flow.state["fork"]["branch"] = BRANCH
    flow.state["commit_sha"] = COMMIT
    v = flow.verify_quad_sha(7)
    assert v["triple_sha"] is True
    assert v["main_head"] == BASE and v["fork_tip"] == COMMIT and v["pr_head"] == COMMIT
    assert v["pr_base"] == BASE  # LOCK-3


# 5. BaseShaDrift 熔断：上游漂移 -> 抛异常停机（严禁自动 rebase）
def test_base_sha_drift_fuse():
    get_map = {f"/repos/{UP}/branches/main": {"commit": {"sha": "drifted0000000000"}},
               f"/repos/{FORK}/branches/{BRANCH}": {"commit": {"sha": COMMIT}},
               f"/repos/{UP}/pulls/7": {"head": {"sha": COMMIT},
                                        "base": {"sha": BASE}}}
    api = FakeAPI(get_map)
    flow = _flow(api)
    flow.state["upstream"]["approved_base_sha"] = BASE
    flow.state["fork"]["branch"] = BRANCH
    flow.state["commit_sha"] = COMMIT
    with pytest.raises(BaseShaDriftException):
        flow.verify_quad_sha(7)
    # PR.base.sha 漂移同样熔断
    get_map[f"/repos/{UP}/branches/main"] = {"commit": {"sha": BASE}}
    get_map[f"/repos/{UP}/pulls/7"] = {"head": {"sha": COMMIT},
                                       "base": {"sha": "drifted0000000000"}}
    with pytest.raises(BaseShaDriftException):
        flow.verify_quad_sha(7)


# 6. 跨库 PR head 参数格式：严格 owner:branch，严禁含 repo 名
def test_cross_repo_pr_head_format():
    get_map = {
        f"/repos/{UP}/branches/main": {"commit": {"sha": BASE}},
        f"/repos/{FORK}/branches/{BRANCH}": {"commit": {"sha": COMMIT}},
        f"/repos/{UP}/pulls/7": {"number": 7, "html_url": "https://x/pr/7",
                                 "head": {"sha": COMMIT}, "base": {"sha": BASE}},
    }
    api = FakeAPI(get_map)
    flow = _flow(api)
    flow.state["upstream"]["approved_base_sha"] = BASE
    flow.state["fork"]["branch"] = BRANCH
    flow.state["commit_sha"] = COMMIT
    flow.state["task_id"] = "issue_3"
    flow.state["fsm"] = "PUSHED_TO_FORK"  # 前置状态（本测试聚焦 head 参数格式）
    _, pr = flow.open_pr("Fix", "body", "issue_3")
    path, payload = api.posts[0]
    assert path == f"/repos/{UP}/pulls"
    assert payload["head"] == f"cc-user:{BRANCH}"  # owner:branch
    assert payload["head"].count(":") == 1
    assert "/" not in payload["head"].split(":")[0]  # owner 侧严禁拼接 repo 名
    assert pr["number"] == 7


# 7. FSM 六态流转：合法路径推进 + 非法跳转拒绝
def test_fsm_progression_and_illegal_transition():
    api = FakeAPI({
        f"/repos/{FORK}": {}, f"/repos/{UP}/branches/main": {"commit": {"sha": BASE}},
        f"/repos/{FORK}/branches/{BRANCH}": {"commit": {"sha": COMMIT}},
        f"/repos/{UP}/pulls/7": {"number": 7, "html_url": "https://x/pr/7",
                                 "head": {"sha": COMMIT}, "base": {"sha": BASE}},
    })
    flow = _flow(api)
    assert flow.ensure_fork() == "FORK_ENSURED"
    assert flow.sync() == "UPSTREAM_SYNCED"
    assert flow.sandbox_verify(BRANCH, DIFF) == "SANDBOX_VERIFIED"
    st, artifact = flow.freeze_card("issue_3", "https://x/issues/3", DIFF)
    assert st == "PENDING_APPROVAL"
    assert artifact["topology"]["fork"] == FORK
    assert flow.execute_push(artifact["artifact_hash"], DIFF, "/tmp/work",
                             git_run=_fake_git_run()) == "PUSHED_TO_FORK"
    st, pr = flow.open_pr("Fix", "body", "issue_3")
    assert st == "PR_OPEN" and pr["number"] == 7
    # 非法跳转拒绝
    with pytest.raises(RuntimeError):
        flow._transition("SANDBOX_VERIFIED")  # PR_OPEN 不可回跳
    # 终态对账
    api.get_map[f"/repos/{UP}/pulls/7"] = {"number": 7, "merged": True, "state": "closed"}
    assert flow.reconcile(7) == "DELIVERED_PR_MERGED"


# 8. 参数化解耦断言：driver 无硬编码仓库名
def test_parameterized_topology_no_hardcoding():
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "b3_fork_driver.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "AIE_UPSTREAM_REPO" in src and "AIE_FORK_REPO" in src
    assert "org-lab/aie-b3-upstream" not in src  # 测试值严禁出现在驱动源码
    assert "cc-user/aie-b3-upstream" not in src


# 9. 全量回归（Gate 1 -> 4.5-B3-1）
def test_all_previous_gates_zero_regression():
    base = os.path.dirname(__file__)
    suites = ["test_security_bounds.py", "test_waiting_resolution.py", "test_settlement_flow.py",
              "test_github_scanner.py", "test_opportunity_filter.py", "test_opportunity_replay.py",
              "test_market_observer.py", "test_private_e2e_rehearsal.py",
              "test_fault_recovery_and_resilience.py", "test_human_approval_barrier.py",
              "test_targeted_opportunity_evaluator.py", "test_source_truth_calibration.py",
              "test_targeted_pipeline.py", "test_real_remote_execution.py"]
    for s in suites:
        rc = pytest.main([os.path.join(base, s), "-q"])
        assert rc == 0, f"Regression failed: {s}"
