# tools/github_pr_issuer.py
"""
Gate 4.2-B 宿主机 PR 发布适配器 + Gate 4.5-A3 审批门禁外部写。

凭证隔离铁律：github_token 仅存在于宿主机当前进程内存（self.token），
严禁写入 /workspace 挂载目录，严禁以任何形式传入沙箱容器。

4.5-A3 变更：新增审批门禁外部写方法（push_branch_gated / create_pr_gated），
任何外部副作用必须先通过 ApprovalGate.guard_write 的 5 大安全锁。
4.2-B 遗留契约 create_pr（无门禁）仅为冻结测试与内部 mock 流程保留。
"""
import subprocess

import requests

from tools.approval_gate import ApprovalGate, UnauthorizedExternalWriteError
from tools.kill_switch import GlobalKillSwitch, KillSwitchActivatedError


class SecurityException(RuntimeError):
    """安全硬锁：物理阻断对受保护分支的写入。"""


PROTECTED_BRANCHES = ("main", "master")


class GitHubPRIssuer:
    def __init__(self, github_token: str = None, api_client=None, approval_gate: ApprovalGate = None,
                 kill_switch: GlobalKillSwitch = None):
        # Token 仅存在于宿主机当前对象内存，严禁传入沙箱容器或写入挂载目录
        self.token = github_token
        self._api = api_client  # 测试注入 mock；生产阶段接真实 GitHub API
        self._issued = {}       # opportunity_id -> external_reference 幂等登记
        self.gate = approval_gate or ApprovalGate()
        self.kill_switch = kill_switch or GlobalKillSwitch()

    def _assert_not_blocked(self) -> None:
        """物理防线第一重：断路器激活时一切远程写立即阻断。"""
        if self.kill_switch.is_blocked():
            raise KillSwitchActivatedError("Global kill switch engaged: all remote writes blocked")

    @staticmethod
    def _assert_branch_safe(branch: str) -> None:
        """分支安全硬锁：目标分支为 main / master 时物理阻断。"""
        if (branch or "").strip().lower() in PROTECTED_BRANCHES:
            raise SecurityException(f"Refusing write to protected branch '{branch}'")

    # ------------------------------------------------------------------ #
    # 4.2-B 遗留契约（无门禁；仅供冻结测试与内部 mock 流程）
    # ------------------------------------------------------------------ #
    def create_pr(self, opportunity_id: str, repo: str, branch: str,
                  title: str, body: str, commit_sha: str) -> dict:
        if not self._api:
            return {
                "success": False,
                "error": "GitHub API client not configured",
                "external_reference": None,
                "hard_cost": 0.0
            }
        if opportunity_id in self._issued:
            return {
                "success": False,
                "error": "Duplicate PR submission rejected",
                "external_reference": self._issued[opportunity_id],
                "duplicate": True,
                "hard_cost": 0.0
            }
        try:
            resp = self._api.create_pull_request(
                repo=repo, branch=branch, title=title, body=body, commit_sha=commit_sha
            )
            pr_number = resp.get("number")
            pr_url = resp.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
            external_reference = f"pr_{repo}_{pr_number}"
            self._issued[opportunity_id] = external_reference
            return {
                "success": True,
                "external_reference": external_reference,
                "pr_url": pr_url,
                "commit_sha": commit_sha,
                "hard_cost": 0.0
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"PR creation failed: {e}",
                "external_reference": None,
                "hard_cost": 0.0
            }

    # ------------------------------------------------------------------ #
    # 4.5-A3 审批门禁外部写（5 大安全锁）
    # ------------------------------------------------------------------ #
    def push_branch_gated(self, artifact: dict, approval_token_id: str,
                          actual_repo: str, actual_branch: str, actual_base_sha: str,
                          current_diff_content: str, push_api=None) -> dict:
        """git push：断路器 + 门禁（无令牌/重放/目标失配/工件篡改 均硬阻断）。"""
        self._assert_not_blocked()
        self._assert_branch_safe(actual_branch)
        self.gate.guard_write(approval_token_id, "push", artifact,
                              actual_repo, actual_branch, actual_base_sha, current_diff_content)
        if push_api is None:
            return {"success": False, "error": "No push transport configured"}
        return push_api.push(repo=actual_repo, branch=actual_branch,
                             commit_sha=artifact["commit_sha"])

    def create_pr_gated(self, artifact: dict, approval_token_id: str,
                        actual_repo: str, actual_branch: str, actual_base_sha: str,
                        current_diff_content: str, title: str, body: str) -> dict:
        """create PR：断路器 + 门禁；PR 正文强制绑定工件哈希（内容与审批卡 100% 吻合）。"""
        self._assert_not_blocked()
        self._assert_branch_safe(actual_branch)
        self.gate.guard_write(approval_token_id, "pr", artifact,
                              actual_repo, actual_branch, actual_base_sha, current_diff_content)
        if self._api is None:
            raise UnauthorizedExternalWriteError("GitHub API client not configured for gated write")
        resp = self._api.create_pull_request(
            repo=actual_repo, branch=actual_branch, title=title,
            body=f"{body}\n\nartifact_hash: {artifact['artifact_hash']}",
            commit_sha=artifact["commit_sha"]
        )
        pr_number = resp.get("number")
        pr_url = resp.get("html_url") or f"https://github.com/{actual_repo}/pull/{pr_number}"
        external_reference = f"pr_{actual_repo}_{pr_number}"
        self._issued[artifact["task_id"]] = external_reference
        return {
            "success": True,
            "external_reference": external_reference,
            "pr_url": pr_url,
            "commit_sha": artifact["commit_sha"],
            "artifact_hash": artifact["artifact_hash"],
            "hard_cost": 0.0
        }

    # ------------------------------------------------------------------ #
    # B2 真实网络写入（仅自有/明确授权仓库；断路器 + 审批双重防线）
    # ------------------------------------------------------------------ #
    def push_branch_remote(self, artifact: dict, approval_token_id: str,
                           actual_repo: str, actual_branch: str, actual_base_sha: str,
                           current_diff_content: str, local_repo_dir: str) -> dict:
        """真实 git push（https + token 内联远端 URL，token 不进沙箱不进磁盘）。"""
        self._assert_not_blocked()
        self._assert_branch_safe(actual_branch)
        self.gate.guard_write(approval_token_id, "push", artifact,
                              actual_repo, actual_branch, actual_base_sha, current_diff_content)
        if not self.token:
            raise UnauthorizedExternalWriteError("GITHUB_TOKEN not configured for remote push")
        remote_url = f"https://x-access-token:{self.token}@github.com/{actual_repo}.git"
        # safe.directory：允许宿主进程推送属主为 UID 1000 的沙箱克隆仓库
        cmd = ["git", "-c", f"safe.directory={local_repo_dir}",
               "-C", local_repo_dir, "push", remote_url,
               f"{artifact['commit_sha']}:refs/heads/{actual_branch}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as e:
            return {"success": False, "error": f"git push timed out: {e}"}
        if proc.returncode != 0:
            return {"success": False, "error": f"git push failed: {proc.stderr[:300]}"}
        return {"success": True, "ref": f"refs/heads/{actual_branch}"}

    def create_pr_remote(self, artifact: dict, approval_token_id: str,
                         actual_repo: str, actual_branch: str, actual_base_sha: str,
                         current_diff_content: str, title: str, body: str,
                         base_ref: str = "main") -> dict:
        """真实 POST /repos/{owner}/{repo}/pulls（断路器 + 审批双重防线）。"""
        self._assert_not_blocked()
        self._assert_branch_safe(actual_branch)
        self.gate.guard_write(approval_token_id, "pr", artifact,
                              actual_repo, actual_branch, actual_base_sha, current_diff_content)
        if not self.token:
            raise UnauthorizedExternalWriteError("GITHUB_TOKEN not configured for remote PR")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Startup-MVP-Scanner",
            "Accept": "application/vnd.github+json"
        }
        payload = {
            "title": title,
            "head": actual_branch,
            "base": base_ref,
            "body": f"{body}\n\nartifact_hash: {artifact['artifact_hash']}"
        }
        try:
            resp = requests.post(
                f"https://api.github.com/repos/{actual_repo}/pulls",
                headers=headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": f"Network error: {e}"}
        if resp.status_code not in (200, 201):
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        pr_number = data.get("number")
        external_reference = f"pr_{actual_repo}_{pr_number}"
        self._issued[artifact["task_id"]] = external_reference
        return {
            "success": True,
            "external_reference": external_reference,
            "pr_url": data.get("html_url"),
            "commit_sha": artifact["commit_sha"],
            "artifact_hash": artifact["artifact_hash"],
            "hard_cost": 0.0
        }
