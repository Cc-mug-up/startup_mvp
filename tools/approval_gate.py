# tools/approval_gate.py
"""
Gate 4.5-A3 人工审批门禁：不可变工件 + 审批卡 + 一次性审批令牌。

核心公理：审批不授权意图，仅授权单一不可变、哈希绑定的工件（Anti-TOCTOU）。
任何外部副作用（git push / create PR / 写评论）发生前必须持有
与当前工件哈希完全吻合、且未被消费的审批令牌。
"""
import hashlib
import re
import uuid
from datetime import datetime, timezone


class UnauthorizedExternalWriteError(RuntimeError):
    """外部写锁：无合法审批令牌时调用写方法。"""


class ArtifactTamperedError(RuntimeError):
    """工件锁（Anti-TOCTOU）：审批后工件被篡改，哈希失配，强行断电。"""


class DestinationMismatchError(RuntimeError):
    """目标锁：实际目标仓库/分支/基准 SHA 与令牌锁定值不一致。"""


class ApprovalReplayError(RuntimeError):
    """重放防护：同一令牌的同一外部写操作重复消费。"""


class BaseShaDriftException(RuntimeError):
    """上游一致性锁：远端主干 HEAD 与审批 base_sha 不一致（时序漂移），废弃审批卡。"""


def canonical(text: str) -> str:
    """规范化：统一换行、去行尾空白，保证同内容同哈希。"""
    return "\n".join(line.rstrip() for line in str(text).replace("\r\n", "\n").split("\n"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalGate:
    def __init__(self):
        self.artifacts = {}  # task_id -> artifact
        self.tokens = {}     # token_id -> token

    # ------------------------------------------------------------------ #
    # 1. 工件哈希绑定（Immutable Artifact Snapshot）
    # ------------------------------------------------------------------ #
    @staticmethod
    def current_artifact_hash(task_id: str, base_sha: str, commit_sha: str,
                              diff_content: str, target_repo: str, target_branch: str) -> str:
        diff_hash = sha256_hex(canonical(diff_content))
        return sha256_hex(canonical(
            f"{task_id}{base_sha}{commit_sha}{diff_hash}{target_repo}{target_branch}"))

    @staticmethod
    def _derive_changed_files(diff_content: str) -> list:
        """
        真理源归正（DEBT-004 整改）：changed_files 严禁手动入参，
        一律从 diff_content（Git 真实输出）解析提取。
        """
        files = []
        for block in str(diff_content).split("diff --git ")[1:]:
            lines = block.splitlines()
            if not lines:
                continue
            header = lines[0]
            m = re.search(r'"a/(.*?)" "b/(.*?)"|a/(\S+) b/(\S+)', header)
            if not m:
                continue
            if m.group(1) is not None:
                path = m.group(2) if m.group(2) != "/dev/null" else m.group(1)
            else:
                path = m.group(4) if m.group(4) != "/dev/null" else m.group(3)
            adds = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
            dels = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
            files.append({"path": path, "added": adds, "deleted": dels})
        return files

    def freeze_artifact(self, task_id: str, source_issue: str, target_repo: str,
                        target_branch: str, base_sha: str, commit_sha: str,
                        diff_content: str, model_id: str, token_cost: float,
                        test_exit_code: int, created_at: str = None,
                        tokens: int = 0, test_summary: str = "") -> dict:
        diff_hash = sha256_hex(canonical(diff_content))
        artifact_hash = sha256_hex(canonical(
            f"{task_id}{base_sha}{commit_sha}{diff_hash}{target_repo}{target_branch}"))
        artifact = {
            "task_id": str(task_id),
            "source_issue": str(source_issue),
            "target_repo": str(target_repo),
            "target_branch": str(target_branch),
            "base_sha": str(base_sha),
            "commit_sha": str(commit_sha),
            "diff_content": str(diff_content),
            "diff_hash": diff_hash,
            "artifact_hash": artifact_hash,
            "model_id": str(model_id),
            "token_cost": float(token_cost),
            "test_exit_code": int(test_exit_code),
            "created_at": created_at or _now_iso(),
            "changed_files": self._derive_changed_files(diff_content),  # Git 物理事实唯一真理源
            "tokens": int(tokens or 0),
            "test_summary": str(test_summary or "")
        }
        self.artifacts[task_id] = artifact
        return artifact

    # ------------------------------------------------------------------ #
    # 2. 标准化审批摘要卡（Approval Card）
    # ------------------------------------------------------------------ #
    @staticmethod
    def check_scope(changed_files, max_files: int = 3, max_lines: int = 50) -> dict:
        total_lines = sum(int(f.get("added", 0)) + int(f.get("deleted", 0))
                          for f in (changed_files or []))
        if len(changed_files or []) > max_files or total_lines > max_lines:
            return {"ok": False, "reason": "SCOPE_EXCEEDED"}
        return {"ok": True, "reason": None}

    @staticmethod
    def risk_class(changed_files) -> str:
        total_lines = sum(int(f.get("added", 0)) + int(f.get("deleted", 0))
                          for f in (changed_files or []))
        if len(changed_files or []) <= 3 and total_lines < 10:
            return f"LOW (BugFix, Lines Changed {total_lines} < 10)"
        return "HIGH (Broad Change)"

    def render_approval_card(self, artifact: dict) -> str:
        files_detail = ", ".join(
            f"{f.get('path', '?')}: +{f.get('added', 0)}, -{f.get('deleted', 0)}"
            for f in artifact.get("changed_files", []))
        n = len(artifact.get("changed_files", []))
        return (
            "==================== [ HUMAN APPROVAL CARD ] ====================\n"
            f"TASK ID:       {artifact['task_id']}\n"
            f"SOURCE:        {artifact['source_issue']}\n"
            f"TARGET REPO:   {artifact['target_repo']}\n"
            f"BASE SHA:      {artifact['base_sha'][:12]}...\n"
            f"TARGET BRANCH: {artifact['target_branch']}\n"
            f"CHANGED FILES: {n} files ({files_detail})\n"
            f"DIFF HASH:     {artifact['diff_hash']}\n"
            f"ARTIFACT HASH: {artifact['artifact_hash']}\n"
            f"TEST RESULT:   EXIT {artifact['test_exit_code']} ({artifact.get('test_summary') or 'n/a'})\n"
            f"COST:          ${artifact['token_cost']:.4f} (Tokens: {artifact.get('tokens', 0)})\n"
            "EXTERNAL ACTIONS REQUESTED:\n"
            f"  [X] GIT PUSH to origin/{artifact['target_branch']}\n"
            f"  [X] CREATE PR against {artifact['target_repo']}:main\n"
            f"RISK CLASS:    {self.risk_class(artifact.get('changed_files', []))}\n"
            "================================================================="
        )

    # ------------------------------------------------------------------ #
    # 3. 一次性审批令牌（Approval Token）
    # ------------------------------------------------------------------ #
    def issue_approval_token(self, artifact: dict) -> dict:
        token = {
            "token_id": str(uuid.uuid4()),
            "artifact_hash": artifact["artifact_hash"],
            "target_repo": artifact["target_repo"],
            "target_branch": artifact["target_branch"],
            "base_sha": artifact["base_sha"],
            "authorized_at": _now_iso(),
            "consumed_ops": {"push": False, "pr": False},
            "revoked": False
        }
        self.tokens[token["token_id"]] = token
        return token

    def revoke_token(self, token_id: str) -> None:
        token = self.tokens.get(token_id)
        if token is not None:
            token["revoked"] = True

    def guard_write(self, token_id: str, op: str, artifact: dict,
                    actual_repo: str, actual_branch: str, actual_base_sha: str,
                    current_diff_content: str) -> dict:
        """
        5 大安全锁硬编码：
        1. External Write Lock（无令牌 -> UnauthorizedExternalWriteError）
        2. Replay Protection（同操作已消费 -> ApprovalReplayError）
        3. Destination Lock（目标不一致 -> DestinationMismatchError）
        4. Artifact Lock（Anti-TOCTOU：现场哈希失配 -> ArtifactTamperedError + 令牌作废）
        5. 通过后原子消费该操作的令牌额度
        """
        token = self.tokens.get(token_id)
        if token is None or token.get("revoked"):
            raise UnauthorizedExternalWriteError("No valid approval token for external write")
        if token["consumed_ops"].get(op):
            raise ApprovalReplayError(f"Token already consumed for op '{op}'")
        if (actual_repo, actual_branch, actual_base_sha) != \
                (token["target_repo"], token["target_branch"], token["base_sha"]):
            raise DestinationMismatchError(
                f"Destination mismatch: approved "
                f"{token['target_repo']}/{token['target_branch']}@{token['base_sha'][:12]}, "
                f"got {actual_repo}/{actual_branch}@{actual_base_sha[:12]}")
        current_hash = self.current_artifact_hash(
            artifact["task_id"], actual_base_sha, artifact["commit_sha"],
            current_diff_content, actual_repo, actual_branch)
        if current_hash != token["artifact_hash"]:
            self.revoke_token(token_id)
            raise ArtifactTamperedError("Artifact hash mismatch: tamper detected (Anti-TOCTOU)")
        token["consumed_ops"][op] = True
        return token
