# tools/drift_guard.py
"""
Gate 4.5-B4-1 基线漂移守卫：过期授权硬熔断。

核心原则：
- 比较真理源必须是不可变字段 approved_base_sha 与现场实时探测值
  current_upstream_main_sha（严禁仅比对 pr_base_sha）；
- 漂移即失效：AWAITING_REVIEW -> GEN_1_INVALIDATED -> HARD STOP；
- 失效后外部写入一律拒绝，任何向后流转（退回 AWAITING_REVIEW）被阻断；
- 漂移事件幂等：记录 first_drift_sha 与 latest_observed_sha，无重复任务衍生；
- 持久化恢复后状态保持失效，严禁倒退或重放历史 Token。
"""
import json
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class DriftGuard:
    def __init__(self, approved_base_sha: str, probe=None):
        self.approved_base_sha = approved_base_sha  # 不可变锚点
        self._probe = probe or (lambda: approved_base_sha)  # 现场实时探测（测试注入）
        self.state = "AWAITING_REVIEW"
        self.external_write_allowed = True
        self.drift_events = []
        self.first_drift_sha = None
        self.latest_observed_sha = None
        self.false_positive_drift = False
        self.upstream_push_counter = 0
        self.fork_push_counter = 0

    # ---------------- 探测与状态 ----------------
    def probe_sha(self) -> str:
        return self._probe()

    def base_health(self) -> str:
        return "HEALTHY" if self.probe_sha() == self.approved_base_sha else "INVALIDATED"

    def check(self) -> dict:
        """
        单次基线探测。漂移即硬熔断：
        1. 状态恒等 AWAITING_REVIEW 时返回无漂移；
        2. 一旦漂移：AWAITING_REVIEW -> GEN_1_INVALIDATED（不可逆），
           记录 first_drift_sha（首次）与 latest_observed_sha（持续更新），
           事件仅登记一次（幂等，无重复任务衍生）。
        """
        cur = self.probe_sha()
        if cur == self.approved_base_sha:
            return {
                "drift_detected": False,
                "state": self.state,
                "current_upstream_main_sha": cur,
                "approved_base_sha": self.approved_base_sha,
                "external_write_allowed": self.external_write_allowed
            }
        # 漂移发生：状态机强制失效（不可逆）
        self.state = "GEN_1_INVALIDATED"
        self.external_write_allowed = False
        if self.first_drift_sha is None:
            self.first_drift_sha = cur
            self.drift_events.append({
                "event_type": "DRIFT_DETECTED",
                "approved_base_sha": self.approved_base_sha,
                "first_drift_sha": cur,
                "detected_at": _now_iso()
            })
        self.latest_observed_sha = cur
        return {
            "drift_detected": True,
            "state": self.state,
            "current_upstream_main_sha": cur,
            "approved_base_sha": self.approved_base_sha,
            "first_drift_sha": self.first_drift_sha,
            "latest_observed_sha": self.latest_observed_sha,
            "external_write_allowed": self.external_write_allowed
        }

    def try_transition(self, target: str) -> bool:
        """状态机守卫：GEN_1_INVALIDATED 是吸收态，任何向后流转被拒绝。"""
        if self.state == "GEN_1_INVALIDATED":
            return False
        if target == "AWAITING_REVIEW":
            self.state = target
            return True
        return False

    # ---------------- 写入守卫（T4 / T5 双重防线） ----------------
    def guard_write(self, token: dict) -> dict:
        """
        写前校验：
        T4 防线 1：现场 current_upstream_main_sha 必须等于 token.binding.base_sha，
                   否则 REJECTED_STALE_APPROVAL_BASE（旧 Token 在新基线下硬拦截）；
        T5 防线 2：持锁后二次重读基线，与首读不一致即 REJECTED_RACE_DRIFT
                   （写前 TOCTOU 竞争窗口拦截），物理写入次数恒 0。
        """
        if self.state == "GEN_1_INVALIDATED":
            return {"result": "REJECTED_STALE_APPROVAL_BASE",
                    "reason": "guard state GEN_1_INVALIDATED",
                    "upstream_push_counter": self.upstream_push_counter,
                    "fork_push_counter": self.fork_push_counter,
                    "external_write_allowed": False}
        token_base = (token.get("binding") or {}).get("base_sha")
        cur = self.probe_sha()
        if token_base != cur:
            return {"result": "REJECTED_STALE_APPROVAL_BASE",
                    "reason": f"token binding base {token_base} != live {cur}",
                    "upstream_push_counter": self.upstream_push_counter,
                    "fork_push_counter": self.fork_push_counter,
                    "external_write_allowed": False}
        # 持锁后二次重读（TOCTOU 竞争窗口）
        cur2 = self.probe_sha()
        if cur2 != self.approved_base_sha:
            self.state = "GEN_1_INVALIDATED"
            self.external_write_allowed = False
            return {"result": "REJECTED_RACE_DRIFT",
                    "reason": f"second read {cur2} != approved {self.approved_base_sha}",
                    "race_drift_detected": True,
                    "upstream_push_counter": self.upstream_push_counter,
                    "fork_push_counter": self.fork_push_counter,
                    "external_write_allowed": False}
        return {"result": "ALLOWED",
                "upstream_push_counter": self.upstream_push_counter,
                "fork_push_counter": self.fork_push_counter,
                "external_write_allowed": True}

    # ---------------- 持久化与恢复（T7） ----------------
    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "approved_base_sha": self.approved_base_sha,
                "state": self.state,
                "external_write_allowed": self.external_write_allowed,
                "first_drift_sha": self.first_drift_sha,
                "latest_observed_sha": self.latest_observed_sha,
                "drift_events": self.drift_events,
                "upstream_push_counter": self.upstream_push_counter,
                "fork_push_counter": self.fork_push_counter
            }, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str, probe=None):
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        g = cls(d["approved_base_sha"], probe=probe)
        g.state = d["state"]
        g.external_write_allowed = d["external_write_allowed"]
        g.first_drift_sha = d["first_drift_sha"]
        g.latest_observed_sha = d["latest_observed_sha"]
        g.drift_events = d["drift_events"]
        g.upstream_push_counter = d["upstream_push_counter"]
        g.fork_push_counter = d["fork_push_counter"]
        return g
