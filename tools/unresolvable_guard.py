# tools/unresolvable_guard.py
"""
Gate 4.5-B4-4 控制面熔断骨架：不可解冲突识别、预算限制与事故黑匣子（纯本地，零网络）。

预算防线：MAX_ATTEMPTS=3 / MAX_FILES=2 / MAX_DIFF_LINES=50。
状态机：CONFLICT_SANDBOX_RESOLVING -> GEN_3_WAITING_APPROVAL | UNRESOLVABLE_HALT（终态）。
事故黑匣子（failure_bundle/）：metadata.json / git_status.txt / failed_tests.log /
constraints.json / exploration_history.json。
"""
import json
import os
from datetime import datetime, timezone

from tools.conflict_guard import agent_side_contract, upstream_side_contract


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class UnresolvableResolver:
    MAX_ATTEMPTS = 3
    MAX_FILES = 2
    MAX_DIFF_LINES = 50

    def __init__(self, sandbox, m2, c2star, m3, allowed_scope, bundle_dir="failure_bundle",
                 task_id="b4-4-task"):
        self.sandbox = sandbox
        self.m2, self.c2star, self.m3 = m2, c2star, m3
        self.allowed_scope = set(allowed_scope)
        self.bundle_dir = bundle_dir
        self.task_id = task_id
        self.state = "CONFLICT_SANDBOX_RESOLVING"
        self.attempts = 0
        self.exploration_history = []
        self.card_allowed = False
        self.network_write_counters = {"upstream": 0, "fork": 0}
        self.halt_reason = None

    # ------------------------------------------------------------------ #
    def attempt_resolution(self, resolution_files, c3, c3_parents, pytest_exit,
                           diff_lines=None) -> str:
        """单次消解尝试（可重试路径与即时熔断路径分离）。"""
        if self.state == "UNRESOLVABLE_HALT":
            return self.state  # 终态：一切后续尝试无效
        self.attempts += 1
        attempt = {"attempt": self.attempts, "ts": _now_iso(),
                   "resolution_files": sorted(resolution_files),
                   "pytest_exit": pytest_exit, "verdict": None}

        # 用例 B：文件预算 + 范围锁（即时熔断）
        if len(resolution_files) > self.MAX_FILES:
            return self._halt("SCOPE_EXCEEDED", attempt,
                              files=len(resolution_files), limit=self.MAX_FILES)
        if not set(resolution_files) <= self.allowed_scope:
            return self._halt("SCOPE_EXCEEDED", attempt,
                              files=sorted(set(resolution_files) - self.allowed_scope))

        # 用例 C：改动量预算（即时熔断）
        lines = diff_lines if diff_lines is not None else self.sandbox.resolution_diff_lines()
        if lines > self.MAX_DIFF_LINES:
            return self._halt("DIFF_BUDGET_EXCEEDED", attempt,
                              diff_lines=lines, limit=self.MAX_DIFF_LINES)

        # 清洁性与 DAG
        if self.sandbox.scan_conflict_markers() or self.sandbox.status_unmerged():
            return self._halt("UNCLEAN_RESOLUTION", attempt)
        if list(c3_parents) != [self.c2star, self.m3]:
            return self._halt("DAG_VIOLATION", attempt)

        # 双向语义契约
        code = self.sandbox.read_code()
        agent_ok = agent_side_contract(code)
        upstream_ok = upstream_side_contract(code)
        attempt["contracts"] = {"agent": agent_ok["pass"], "upstream": upstream_ok["pass"],
                                "failures": agent_ok["failures"] + upstream_ok["failures"]}
        if not (agent_ok["pass"] and upstream_ok["pass"]):
            # 用例 A：语义互斥——可重试，尝试耗尽后熔断
            attempt["verdict"] = "SEMANTIC_CONTRACT_FAIL"
            self.exploration_history.append(attempt)
            if self.attempts >= self.MAX_ATTEMPTS:
                return self._halt("ATTEMPTS_EXCEEDED", attempt=None)
            return self.state
        if pytest_exit != 0:
            attempt["verdict"] = "PYTEST_FAIL"
            self.exploration_history.append(attempt)
            if self.attempts >= self.MAX_ATTEMPTS:
                return self._halt("ATTEMPTS_EXCEEDED", attempt=None)
            return self.state

        self.card_allowed = True
        self.state = "GEN_3_WAITING_APPROVAL"
        attempt["verdict"] = "PASS"
        self.exploration_history.append(attempt)
        return self.state

    # ------------------------------------------------------------------ #
    def _halt(self, reason, attempt=None, **extra):
        if attempt is not None:
            attempt["verdict"] = reason
            attempt.update(extra)
            self.exploration_history.append(attempt)
        self.state = "UNRESOLVABLE_HALT"
        self.card_allowed = False
        self.halt_reason = reason
        self._assemble_failure_bundle(reason)
        return self.state

    def _assemble_failure_bundle(self, reason):
        """事故黑匣子：触发停机时自动组装五件套诊断包。"""
        os.makedirs(self.bundle_dir, exist_ok=True)

        metadata = {
            "task_id": self.task_id,
            "state": self.state,
            "halt_reason": reason,
            "halted_at": _now_iso(),
            "counters": {"attempts": self.attempts},
            "shas": {"m2": self.m2, "c2star": self.c2star, "m3": self.m3},
            "card_allowed": self.card_allowed,
            "network_write_counters": dict(self.network_write_counters)
        }
        with open(os.path.join(self.bundle_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        with open(os.path.join(self.bundle_dir, "git_status.txt"), "w", encoding="utf-8") as f:
            f.write(self.sandbox.status())

        with open(os.path.join(self.bundle_dir, "failed_tests.log"), "w", encoding="utf-8") as f:
            f.write(self.sandbox.failed_tests_log())

        constraints = {
            "max_attempts": self.MAX_ATTEMPTS,
            "max_files": self.MAX_FILES,
            "max_diff_lines": self.MAX_DIFF_LINES,
            "allowed_scope": sorted(self.allowed_scope),
            "dag_parents_required": [self.c2star, self.m3]
        }
        with open(os.path.join(self.bundle_dir, "constraints.json"), "w", encoding="utf-8") as f:
            json.dump(constraints, f, indent=2, ensure_ascii=False)

        with open(os.path.join(self.bundle_dir, "exploration_history.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.exploration_history, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    def generate_approval_card(self) -> dict:
        """审批卡生成硬阻断：UNRESOLVABLE_HALT 下严禁产出任何审批卡。"""
        if self.state == "UNRESOLVABLE_HALT":
            return None
        if not self.card_allowed:
            return None
        return {"task_id": self.task_id, "state": self.state, "card": "PENDING"}
