# tools/b3_fork_driver.py
"""
Gate 4.5-B3-2 跨仓库 Fork 协作拓扑驱动（Upstream is Read-Only, Fork is Write-Only）。

纯参数化解耦（严禁硬编码）：
  - AIE_UPSTREAM_REPO（如 <ORG>/aie-b3-upstream）
  - AIE_FORK_REPO（如 Cc-mug-up/aie-b3-upstream）
  - GITHUB_TOKEN（.env）

物理隔离铁律：
  - git remote set-url --push upstream no_push://upstream_disabled（推送上行物理禁用）
  - 所有写入仅以 origin (Fork) 为目标；upstream_push_counter 必须恒为 0，
    违者抛 DestinationMismatchError；
  - 跨库 PR head 参数严格为 f"{fork_owner}:{branch}"，严禁拼接 repo 名。

四重 SHA 锁：
  LOCK-1 执行前 remote upstream main HEAD == approved_base_sha（漂移即 BaseShaDriftException）
  LOCK-2 执行前现场工件哈希 == 签名（Anti-TOCTOU）
  三 SHA 全等 SHA-A(approved) == SHA-B(fork tip) == SHA-C(PR head)
  LOCK-3 PR_OPEN 阶段 PR.base.sha == approved_base_sha
"""
import base64
import json
import os
import subprocess
import sys
import time

import requests

from tools.approval_gate import (ApprovalGate, canonical, sha256_hex,
                                 ArtifactTamperedError, BaseShaDriftException,
                                 DestinationMismatchError)
from tools.github_enricher import load_github_token
from tools.kill_switch import GlobalKillSwitch
from tools.review_normalizer import ReviewNormalizer

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
FORK_REPO = os.environ.get("AIE_FORK_REPO", "")
TOKEN = load_github_token()
STATE_FILE = "b3fork_driver_state.json"

# FSM 合法流转表
FSM_TRANSITIONS = {
    "NEW": ["FORK_ENSURED"],
    "FORK_ENSURED": ["UPSTREAM_SYNCED"],
    "UPSTREAM_SYNCED": ["SANDBOX_VERIFIED"],
    "SANDBOX_VERIFIED": ["PENDING_APPROVAL"],
    "PENDING_APPROVAL": ["PUSHED_TO_FORK"],
    "PUSHED_TO_FORK": ["PR_OPEN"],
    "PR_OPEN": ["DELIVERED_PR_MERGED", "DEAD_PR_REJECTED"],
}


class GitHubAPI:
    """只读 + 受控写 API 客户端（测试可注入 Fake）。"""

    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport or requests

    def get(self, path, timeout=20):
        resp = self.transport.get(f"https://api.github.com{path}", timeout=timeout,
                                  headers={"Authorization": f"Bearer {self.token}",
                                           "User-Agent": "Startup-MVP-Scanner",
                                           "Accept": "application/vnd.github+json"})
        return resp

    def post(self, path, payload, timeout=20):
        resp = self.transport.post(f"https://api.github.com{path}", timeout=timeout,
                                   headers={"Authorization": f"Bearer {self.token}",
                                            "User-Agent": "Startup-MVP-Scanner",
                                            "Accept": "application/vnd.github+json"},
                                   json=payload)
        return resp

    def put(self, path, payload, timeout=20):
        resp = self.transport.put(f"https://api.github.com{path}", timeout=timeout,
                                  headers={"Authorization": f"Bearer {self.token}",
                                           "User-Agent": "Startup-MVP-Scanner",
                                           "Accept": "application/vnd.github+json"},
                                  json=payload)
        return resp


class ForkPusher:
    """写入物理隔离执行器：唯一推送入口，upstream_push_counter 恒 0。"""

    def __init__(self, fork_repo, upstream_repo, token):
        self.fork_repo = fork_repo
        self.upstream_repo = upstream_repo
        self.token = token
        self.upstream_push_counter = 0
        self.successful_pushes = 0

    def push(self, target_repo, branch, commit_sha, local_repo_dir, git_run=None):
        if target_repo != self.fork_repo:
            if target_repo == self.upstream_repo:
                self.upstream_push_counter += 1
            raise DestinationMismatchError(
                f"push target {target_repo} != fork {self.fork_repo} (upstream is read-only)")
        remote_url = f"https://x-access-token:{self.token}@github.com/{self.fork_repo}.git"
        run = git_run or subprocess.run
        proc = run(["git", "-c", f"safe.directory={local_repo_dir}",
                    "-C", local_repo_dir, "push", remote_url,
                    f"{commit_sha}:refs/heads/{branch}"],
                   capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {"success": False, "error": f"git push failed: {proc.stderr[:300]}"}
        self.successful_pushes += 1
        return {"success": True, "ref": f"refs/heads/{branch}"}


class ForkFlow:
    """B3-2 拓扑流水线（纯逻辑，API/推送/沙箱均可注入 mock）。"""

    def __init__(self, upstream_repo, fork_repo, token, api=None, pusher=None,
                 state=None, sandbox_exec=None):
        self.upstream_repo = upstream_repo
        self.fork_repo = fork_repo
        self.fork_owner = fork_repo.split("/")[0]
        self.api = api or GitHubAPI(token)
        self.pusher = pusher or ForkPusher(fork_repo, upstream_repo, token)
        self.sandbox_exec = sandbox_exec
        self.state = state if state is not None else {
            "upstream": {"repo": upstream_repo, "default_branch": "main",
                         "approved_base_sha": None, "pr_number": None,
                         "readonly_url": f"https://github.com/{upstream_repo}"},
            "fork": {"repo": fork_repo, "created_at": None, "source_sha": None,
                     "branch": None, "push_url": f"https://x-access-token:<TOKEN>@github.com/{fork_repo}"},
            "fsm": "NEW",
            "artifact_hash": None,
            "commit_sha": None
        }

    # ---------------- FSM ----------------
    def _transition(self, to):
        cur = self.state["fsm"]
        if to not in FSM_TRANSITIONS.get(cur, []):
            raise RuntimeError(f"Illegal FSM transition: {cur} -> {to}")
        self.state["fsm"] = to
        return to

    # ---------------- 阶段 ----------------
    def ensure_fork(self):
        """FORK_ENSURED：fork 存在性检查（不存在则 POST /forks 创建）。"""
        resp = self.api.get(f"/repos/{self.fork_repo}")
        if resp.status_code == 200:
            created = False
        else:
            resp = self.api.post(f"/repos/{self.upstream_repo}/forks", {})
            assert resp.status_code in (200, 202), f"fork create failed: {resp.status_code}"
            created = True
        src = self.api.get(f"/repos/{self.upstream_repo}/branches/main")
        self.state["fork"]["source_sha"] = src.json()["commit"]["sha"]
        self.state["fork"]["created_at"] = self.state["fork"].get("created_at") or \
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.state["upstream"]["approved_base_sha"] = self.state["fork"]["source_sha"]
        return self._transition("FORK_ENSURED")

    def sync(self):
        """UPSTREAM_SYNCED：沙箱内配置只读 upstream 远端 + 推送上行物理禁用 + LOCK-1。"""
        exec_fn = self.sandbox_exec
        assert exec_fn is not None, "sandbox_exec required"
        setup_cmd = (
            f"git clone -q https://github.com/{self.fork_repo} work && cd work && "
            f"git remote add upstream https://github.com/{self.upstream_repo} && "
            f"git remote set-url --push upstream no_push://upstream_disabled && "
            f"git fetch -q upstream main && git rev-parse upstream/main")
        code, out, err = exec_fn(setup_cmd)
        assert code == 0, f"sync failed: {err or out}"
        upstream_head = out.strip().splitlines()[-1]
        # LOCK-1：上游 main 必须等于 approved base（漂移即失效，严禁自动 rebase）
        if upstream_head != self.state["upstream"]["approved_base_sha"]:
            raise BaseShaDriftException(
                f"upstream main {upstream_head} != approved base "
                f"{self.state['upstream']['approved_base_sha']}")
        return self._transition("UPSTREAM_SYNCED")

    def sandbox_verify(self, branch, patch, exec_fn=None):
        """SANDBOX_VERIFIED：基于 upstream/main 的受控修补 + pytest exit 0。"""
        exec_fn = exec_fn or self.sandbox_exec
        assert exec_fn is not None, "sandbox_exec required"
        import os as _os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
            f.write(patch)
            patch_path = f.name
        base = self.state["upstream"]["approved_base_sha"]
        chain = (
            f"cd work && git config user.email 'agent@aie.local' && "
            f"git config user.name 'AIE Agent' && "
            f"git checkout -q -b {branch} upstream/main && "
            f"git apply {patch_path} && rm {patch_path} && rm -rf __pycache__ && "
            f"printf '__pycache__/\\n*.pyc\\n' > .gitignore && "
            f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q && "
            f"git add -A && git commit -qm 'Fork fix (aie-b3-2)' && git rev-parse HEAD")
        code, out, err = exec_fn(chain)
        _os.unlink(patch_path) if _os.path.exists(patch_path) else None
        assert code == 0, f"sandbox verify failed: {err or out[:400]}"
        self.state["commit_sha"] = out.strip().splitlines()[-1]
        self.state["fork"]["branch"] = branch
        return self._transition("SANDBOX_VERIFIED")

    def freeze_card(self, task_id, source_issue, diff_content, model_id="b3-fork-scripted"):
        """PENDING_APPROVAL：拓扑绑定的不可变工件 + 审批卡（物理停机由 CLI 执行）。"""
        gate = ApprovalGate()
        diff_hash = sha256_hex(canonical(diff_content))
        commit = self.state["commit_sha"]
        base = self.state["upstream"]["approved_base_sha"]
        branch = self.state["fork"]["branch"]
        # 工件哈希绑定拓扑：task + upstream.repo + approved_base + fork.repo + branch + commit + diff
        artifact_hash = sha256_hex(canonical(
            f"{task_id}{self.upstream_repo}{base}{self.fork_repo}{branch}{commit}{diff_hash}"))
        artifact = {
            "task_id": task_id, "source_issue": source_issue,
            "upstream_repo": self.upstream_repo, "fork_repo": self.fork_repo,
            "approved_base_sha": base, "branch": branch, "commit_sha": commit,
            "diff_content": diff_content, "diff_hash": diff_hash,
            "artifact_hash": artifact_hash, "model_id": model_id,
            "token_cost": 0.0, "test_exit_code": 0,
            "changed_files": gate._derive_changed_files(diff_content),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "topology": {"role": "fork_push", "upstream": self.upstream_repo,
                         "fork": self.fork_repo}
        }
        self.state["artifact_hash"] = artifact_hash
        self.state["task_id"] = task_id  # 供 execute_push 现场重算使用
        return self._transition("PENDING_APPROVAL"), artifact

    def execute_push(self, signed_hash, diff_content, local_repo_dir, git_run=None):
        """PUSHED_TO_FORK：LOCK-1 复验 + LOCK-2（Anti-TOCTOU）→ 只向 fork 推送。"""
        # LOCK-1 复验
        main_head = self.api.get(
            f"/repos/{self.upstream_repo}/branches/main").json()["commit"]["sha"]
        if main_head != self.state["upstream"]["approved_base_sha"]:
            raise BaseShaDriftException(
                f"upstream main drifted: {main_head} != {self.state['upstream']['approved_base_sha']}")
        # LOCK-2 现场工件重算
        commit = self.state["commit_sha"]
        base = self.state["upstream"]["approved_base_sha"]
        branch = self.state["fork"]["branch"]
        cur_hash = sha256_hex(canonical(
            f"{self.state.get('task_id', '')}{self.upstream_repo}{base}{self.fork_repo}"
            f"{branch}{commit}{sha256_hex(canonical(diff_content))}"))
        if cur_hash != self.state["artifact_hash"] or signed_hash != self.state["artifact_hash"]:
            raise ArtifactTamperedError("ARTIFACT HASH MISMATCH -> FUSE BLOWN")
        res = self.pusher.push(self.fork_repo, branch, commit, local_repo_dir, git_run=git_run)
        assert res["success"], res
        assert self.pusher.upstream_push_counter == 0, "upstream push attempted!"
        return self._transition("PUSHED_TO_FORK")

    def open_pr(self, title, body, task_id):
        """PR_OPEN：跨库 PR（head=fork_owner:branch）+ 四重 SHA 对账。"""
        self.state["task_id"] = task_id
        branch = self.state["fork"]["branch"]
        payload = {"title": title, "head": f"{self.fork_owner}:{branch}", "base": "main",
                   "body": body}
        # head 严格 owner:branch：owner 侧严禁拼接 repo 名（含 "/" 即非法）
        assert payload["head"].count(":") == 1, "head must be owner:branch"
        assert "/" not in payload["head"].split(":")[0], "head owner must not contain repo name"
        resp = self.api.post(f"/repos/{self.upstream_repo}/pulls", payload)
        assert resp.status_code in (200, 201), f"PR create failed: {resp.status_code} {resp.text[:200]}"
        pr = resp.json()
        pr_number = pr["number"]
        self.state["upstream"]["pr_number"] = pr_number
        # 四重 SHA
        v = self.verify_quad_sha(pr_number)
        assert v["triple_sha"] is True, v
        return self._transition("PR_OPEN"), pr

    def verify_quad_sha(self, pr_number):
        """四重 SHA 锁：LOCK-1 base 不漂移 + 三 SHA 全等 + LOCK-3 PR.base.sha。"""
        base = self.state["upstream"]["approved_base_sha"]
        commit = self.state["commit_sha"]
        branch = self.state["fork"]["branch"]
        # LOCK-1
        main_head = self.api.get(
            f"/repos/{self.upstream_repo}/branches/main").json()["commit"]["sha"]
        if main_head != base:
            raise BaseShaDriftException(f"upstream main {main_head} != approved base {base}")
        pr = self.api.get(f"/repos/{self.upstream_repo}/pulls/{pr_number}").json()
        # LOCK-3
        pr_base = pr["base"]["sha"]
        if pr_base != base:
            raise BaseShaDriftException(f"PR.base.sha {pr_base} != approved base {base}")
        tip = self.api.get(
            f"/repos/{self.fork_repo}/branches/{branch}").json()["commit"]["sha"]
        pr_head = pr["head"]["sha"]
        triple = (commit == tip == pr_head)
        return {"triple_sha": triple, "main_head": main_head, "fork_tip": tip,
                "pr_head": pr_head, "pr_base": pr_base, "approved_base": base,
                "pr_number": pr_number, "pr_url": pr.get("html_url")}

    def reconcile(self, pr_number):
        """终态对账：merged -> DELIVERED_PR_MERGED；closed 未合并 -> DEAD_PR_REJECTED。"""
        pr = self.api.get(f"/repos/{self.upstream_repo}/pulls/{pr_number}").json()
        if pr.get("merged"):
            return self._transition("DELIVERED_PR_MERGED")
        if pr.get("state") == "closed":
            return self._transition("DEAD_PR_REJECTED")
        return None  # 仍 open


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    if not UPSTREAM_REPO or not FORK_REPO:
        print("AIE_UPSTREAM_REPO / AIE_FORK_REPO env vars required (no hardcoding).")
        sys.exit(1)
    print(f"topology: upstream={UPSTREAM_REPO} fork={FORK_REPO} (parameterized)")


if __name__ == "__main__":
    main()
