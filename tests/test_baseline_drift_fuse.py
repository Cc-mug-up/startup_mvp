# tests/test_baseline_drift_fuse.py
# Gate 4.5-B4-1 基线漂移发现与过期授权硬熔断验证（T0~T8 / 纯 Mock / 零网络零写入）
import json
import os
from datetime import datetime, timezone

import pytest

from tools.drift_guard import DriftGuard

M0 = "1111111111111111111111111111111111111111"
M1 = "2222222222222222222222222222222222222222"
M2 = "3333333333333333333333333333333333333333"
M3 = "4444444444444444444444444444444444444444"

EVIDENCE = []


def _ev(tag, verdict, **fields):
    EVIDENCE.append({"t": tag, "ts": datetime.now(timezone.utc).isoformat(),
                     "verdict": verdict, **fields})


class FakeUpstream:
    """现场上游探测器：head 可被测试动态拨动（模拟维护者提交）。"""

    def __init__(self, head=M0):
        self.head = head

    def __call__(self):
        return self.head


@pytest.fixture
def upstream():
    return FakeUpstream(M0)


def _fresh_guard(upstream):
    return DriftGuard(M0, probe=upstream)


# ------------------------------------------------------------------ #
# T0：环境与夹具准备（approved_base_sha=M0 不可变；BASE_HEALTH == HEALTHY）
# ------------------------------------------------------------------ #
def test_t0_env_preparation(upstream):
    g = _fresh_guard(upstream)
    assert g.approved_base_sha == M0
    assert g.base_health() == "HEALTHY"
    assert g.state == "AWAITING_REVIEW"
    _ev("T0", "PASS", approved_base_sha=M0, base_health=g.base_health())


# ------------------------------------------------------------------ #
# T1：基线无变化，连续 3 次探测不产生误报
# ------------------------------------------------------------------ #
def test_t1_no_false_positive_on_stable_base(upstream):
    g = _fresh_guard(upstream)
    for i in range(3):
        r = g.check()
        assert r["drift_detected"] is False
        assert r["state"] == "AWAITING_REVIEW"
    assert g.false_positive_drift is False
    assert g.first_drift_sha is None
    _ev("T1", "PASS", probes=3, false_positive_drift=False)


# ------------------------------------------------------------------ #
# T2/T3：真实漂移发现 + 状态机强制失效（不可逆）
# ------------------------------------------------------------------ #
def test_t2_t3_real_drift_detection_and_invalidation(upstream):
    g = _fresh_guard(upstream)
    upstream.head = M1  # 维护者在 main 提交无关变更，主干前进至 M1
    r = g.check()

    # T2：DRIFT_DETECTED == True + 结构化漂移事件日志
    assert r["drift_detected"] is True
    assert len(g.drift_events) == 1
    ev = g.drift_events[0]
    assert ev["event_type"] == "DRIFT_DETECTED"
    assert ev["approved_base_sha"] == M0 and ev["first_drift_sha"] == M1
    _ev("T2", "PASS", drift_detected=True, event=ev)

    # T3：FSM 跃迁 GEN_1_INVALIDATED，阻断任何向后流转
    assert g.state == "GEN_1_INVALIDATED"
    assert g.external_write_allowed is False
    assert g.try_transition("AWAITING_REVIEW") is False  # 向后流转被拒绝
    assert g.state == "GEN_1_INVALIDATED"
    _ev("T3", "PASS", state="GEN_1_INVALIDATED", backward_transition_blocked=True)


# ------------------------------------------------------------------ #
# T4：未消费旧 Token + 新基线硬拦截（REJECTED_STALE_APPROVAL_BASE，计数 +0）
# ------------------------------------------------------------------ #
def test_t4_stale_token_rejected_on_new_base(upstream):
    # 先漂移至 M1
    g = _fresh_guard(upstream)
    upstream.head = M1
    g.check()

    token = {"id": "tok_m0_unconsumed", "binding": {"base_sha": M0}}
    r = g.guard_write(token)
    assert r["result"] == "REJECTED_STALE_APPROVAL_BASE"
    assert r["upstream_push_counter"] == 0
    assert r["fork_push_counter"] == 0
    assert g.upstream_push_counter == 0 and g.fork_push_counter == 0

    # 正向对照：漂移前（独立守卫生存于 M0 场景）同令牌可通过
    upstream2 = FakeUpstream(M0)
    g2 = DriftGuard(M0, probe=upstream2)
    r2 = g2.guard_write(token)
    assert r2["result"] == "ALLOWED"
    _ev("T4", "PASS", stale_token="REJECTED_STALE_APPROVAL_BASE",
        positive_control="ALLOWED", push_counters=(0, 0))


# ------------------------------------------------------------------ #
# T5：写前 TOCTOU 竞争窗口拦截（持锁二次重读）
# ------------------------------------------------------------------ #
class SequencedProbe:
    """按序列返回现场基线：精确建模"读1之后、读2之前"的竞争切片。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.i = 0

    def __call__(self):
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v


def test_t5_race_window_toctou_intercept():
    # 序列：M0(预检) -> M0(guard 读1) -> M1(guard 读2)：漂移恰在两次读之间
    probe = SequencedProbe([M0, M0, M1])
    g = DriftGuard(M0, probe=probe)
    token = {"id": "tok_m0", "binding": {"base_sha": M0}}

    # Preflight 首读通过（M0 无漂移）
    assert g.probe_sha() == M0
    # guard_write：读1 = M0（令牌绑定通过）；读2 = M1（TOCTOU 竞争窗口命中）
    r = g.guard_write(token)
    assert r["result"] == "REJECTED_RACE_DRIFT"
    assert r.get("race_drift_detected") is True
    assert g.upstream_push_counter == 0 and g.fork_push_counter == 0
    assert g.state == "GEN_1_INVALIDATED"
    _ev("T5", "PASS", race_drift_detected=True, physical_writes=0)


# ------------------------------------------------------------------ #
# T6：多次漂移事件幂等性（M1->M2->M3，无重复任务衍生）
# ------------------------------------------------------------------ #
def test_t6_multi_drift_idempotency(upstream):
    g = _fresh_guard(upstream)
    upstream.head = M1
    g.check()
    upstream.head = M2
    g.check()
    upstream.head = M3
    g.check()

    assert g.state == "GEN_1_INVALIDATED"
    assert g.first_drift_sha == M1          # 首次漂移 SHA 锚定
    assert g.latest_observed_sha == M3      # 最新观察 SHA 持续更新
    assert len(g.drift_events) == 1         # 事件仅登记一次：无重复任务衍生
    _ev("T6", "PASS", first_drift_sha=M1, latest_observed_sha=M3,
        drift_events=len(g.drift_events))


# ------------------------------------------------------------------ #
# T7：进程重启恢复（持久化后仍失效，严禁倒退/重放）
# ------------------------------------------------------------------ #
def test_t7_restart_recovery_stays_invalidated(upstream, tmp_path):
    g = _fresh_guard(upstream)
    upstream.head = M1
    g.check()
    state_path = str(tmp_path / "drift_state.json")
    g.save(state_path)

    # 模拟进程终止后重新拉起（新的探测实例仍指向 M1 现场）
    upstream2 = FakeUpstream(M1)
    g2 = DriftGuard.load(state_path, probe=upstream2)
    assert g2.state == "GEN_1_INVALIDATED"          # 直接判定失效
    assert g2.external_write_allowed is False
    assert g2.try_transition("AWAITING_REVIEW") is False  # 严禁倒退
    # 历史 Token 重放被硬拦截
    token = {"id": "tok_m0", "binding": {"base_sha": M0}}
    r = g2.guard_write(token)
    assert r["result"] == "REJECTED_STALE_APPROVAL_BASE"
    assert g2.upstream_push_counter == 0 and g2.fork_push_counter == 0
    _ev("T7", "PASS", restored_state="GEN_1_INVALIDATED",
        replay_rejected="REJECTED_STALE_APPROVAL_BASE")


# ------------------------------------------------------------------ #
# T8：机器可读验收证据块（T0~T8 因果时间戳）
# ------------------------------------------------------------------ #
def test_t8_evidence_block():
    tags = [e["t"] for e in EVIDENCE]
    for expected in ["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7"]:
        assert expected in tags, f"missing evidence {expected}"
    assert all(e["verdict"] == "PASS" for e in EVIDENCE)
    EVIDENCE.append({"t": "T8", "ts": datetime.now(timezone.utc).isoformat(),
                     "verdict": "PASS", "summary": "B4-1 all assertions passed"})
    print()
    print("B4-1_EVIDENCE_BLOCK")
    for e in EVIDENCE:
        print(json.dumps(e, ensure_ascii=False))
