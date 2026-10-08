# tests/test_conflict_guard.py
# Gate 4.5-B4-3 冲突守卫 FSM 与四路径夹具测试（纯 Mock / 零网络零写）
import pytest

from tools.conflict_guard import ConflictGuard

M2 = "m2" * 20
C2S = "c2" * 20
M3 = "m3" * 20
C3 = "c3" * 20
SNAPSHOT = "snap_unmerged"

GOOD_CODE = '''
def add(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("numeric required")
    return a + b

def multiply(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("multiply requires numeric arguments")
    result = a * b
    if abs(result) > 1_000_000:
        raise ValueError("result magnitude too large")
    return result

def divide(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("divide requires numeric arguments")
    if b == 0:
        raise ValueError("divisor must not be zero")
    return a / b
'''

OURS_ONLY_CODE = '''
def add(a, b):
    return a + b

def multiply(a, b):
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("multiply requires integer arguments")
    return a * b

def divide(a, b):
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("divide requires integer arguments")
    if b == 0:
        raise ValueError("divisor must not be zero")
    return a / b
'''


class FakeSandbox:
    """
    冲突现场桩：两阶段状态——
    探测阶段：merge 失败 + UU 索引 + 冲突标记齐备（真值三要素）；
    消解阶段：测试在 resolve 前把 markers/unmerged 清空（模拟消解动作完成）。
    """

    def __init__(self, merge_exit=1, porcelain="UU calculator.py\n",
                 markers=None, unmerged=True, code=GOOD_CODE,
                 resolution_files=None, c3_parents=None, pytest_exit=0):
        self.merge_exit = merge_exit
        self.merge_output = "CONFLICT (content): Merge conflict in calculator.py"
        self.porcelain = porcelain
        self.markers = markers if markers is not None else ["calculator.py"]
        self.unmerged_flag = unmerged
        self.code = code
        self.resolution_files = resolution_files if resolution_files is not None \
            else ["calculator.py"]
        self.c3_parents = c3_parents if c3_parents is not None else [C2S, M3]
        self.pytest_exit = pytest_exit

    def merge(self, ref):
        return self.merge_exit, self.merge_output

    def status(self):
        return self.porcelain

    def scan_conflict_markers(self):
        return self.markers

    def status_unmerged(self):
        return self.unmerged_flag

    def capture_snapshot(self):
        return SNAPSHOT

    def diff(self, a, b):
        return {"files": ["calculator.py"], "patch": f"diff-{a[:6]}-{b[:6]}"}

    def read_code(self):
        return self.code


def _guard(sb, scope=("calculator.py", "test_calculator.py")):
    return ConflictGuard(sb, M2, C2S, M3, list(scope))


# ------------------------------------------------------------------ #
# 路径 1：正常冲突注入 -> Index 真值识别 -> CONFLICT_DETECTED
# ------------------------------------------------------------------ #
def test_conflict_injection_detects_index_truth():
    sb = FakeSandbox()
    g = _guard(sb)
    assert g.run_invalidation() == "DRIFT_ANALYZING"
    assert g.probe_conflict() == "CONFLICT_DETECTED"

    ev = g.evidence
    # 三重物理事实组合缺一不可
    assert ev["merge_exit_code"] != 0
    assert ev["unmerged_entries"] == ["calculator.py"]  # UU 索引
    assert ev["marker_files"] == ["calculator.py"]      # 冲突文本标记


# ------------------------------------------------------------------ #
# 路径 2：暴力覆盖（--ours/--theirs）-> 语义契约失败 -> UNRESOLVABLE_HALT
# ------------------------------------------------------------------ #
def test_bruteforce_checkout_triggers_halt():
    sb = FakeSandbox(code=OURS_ONLY_CODE)  # --ours 覆盖：丢弃上游放宽契约
    g = _guard(sb)
    g.run_invalidation()
    g.probe_conflict()
    g.capture_unmerged_snapshot()
    sb.markers = []
    sb.unmerged_flag = False  # 消解动作已执行（--ours 覆盖），残留清除

    state = g.resolve(sb.resolution_files, C3, sb.c3_parents, sb.pytest_exit)
    assert state == "UNRESOLVABLE_HALT"
    assert g.evidence["halt_reason"] == "SEMANTIC_CONTRACT_FAIL"
    assert g.evidence["upstream_contract"]["pass"] is False
    # 吸收态约束：禁止流转、禁止审批卡、禁止远端写
    assert g.transition("GEN_3_WAITING_APPROVAL") is False
    assert g.card_allowed is False
    assert g.remote_write_allowed is False


# ------------------------------------------------------------------ #
# 路径 3：越权扩散（修改 README.md）-> Scope Lock 失败 -> UNRESOLVABLE_HALT
# ------------------------------------------------------------------ #
def test_scope_violation_triggers_halt():
    sb = FakeSandbox(resolution_files=["calculator.py", "README.md"])
    g = _guard(sb)
    g.run_invalidation()
    g.probe_conflict()
    g.capture_unmerged_snapshot()
    sb.markers = []
    sb.unmerged_flag = False  # 消解动作已执行（越权扩散：README.md 被改）

    state = g.resolve(sb.resolution_files, C3, sb.c3_parents, sb.pytest_exit)
    assert state == "UNRESOLVABLE_HALT"
    assert g.evidence["halt_reason"].startswith("SCOPE_VIOLATION")
    assert g.card_allowed is False and g.remote_write_allowed is False


# ------------------------------------------------------------------ #
# 路径 4：正确消解 -> 全链路绿灯 -> GEN_3_WAITING_APPROVAL
# ------------------------------------------------------------------ #
def test_correct_resolution_full_green():
    sb = FakeSandbox()  # 干净消解：无标记/无未合并/双亲合规/契约双向 PASS/pytest 0
    g = _guard(sb)
    g.run_invalidation()
    assert g.probe_conflict() == "CONFLICT_DETECTED"
    g.capture_unmerged_snapshot()
    sb.markers = []
    sb.unmerged_flag = False  # 消解完成：冲突标记与未合并索引清除

    state = g.resolve(sb.resolution_files, C3, sb.c3_parents, sb.pytest_exit)
    assert state == "GEN_3_WAITING_APPROVAL"
    assert g.card_allowed is True
    # 三维差分解耦齐备
    assert g.evidence["upstream_delta"]["files"] == ["calculator.py"]
    assert g.evidence["original_business_delta"]["files"] == ["calculator.py"]
    assert g.evidence["resolution_delta"]["files"] == ["calculator.py"]
    # 双向契约 PASS
    assert g.evidence["agent_contract"]["pass"] is True
    assert g.evidence["upstream_contract"]["pass"] is True
    # 状态路径完整
    assert g.state == "GEN_3_WAITING_APPROVAL"


# ------------------------------------------------------------------ #
# 附加：DAG 违规与未清洁消解路径
# ------------------------------------------------------------------ #
def test_dag_violation_halt():
    sb = FakeSandbox(c3_parents=[C2S, "wrong" * 20])
    g = _guard(sb)
    g.run_invalidation()
    g.probe_conflict()
    g.capture_unmerged_snapshot()
    sb.markers = []
    sb.unmerged_flag = False  # 消解已执行，DAG 双亲错误
    assert g.resolve(sb.resolution_files, C3, sb.c3_parents, 0) == "UNRESOLVABLE_HALT"
    assert g.evidence["halt_reason"] == "DAG_VIOLATION"


def test_unclean_resolution_halt():
    sb = FakeSandbox(markers=["calculator.py"], unmerged=True)  # 消解后仍残留
    g = _guard(sb)
    g.run_invalidation()
    g.probe_conflict()
    g.capture_unmerged_snapshot()
    assert g.resolve(sb.resolution_files, C3, sb.c3_parents, 0) == "UNRESOLVABLE_HALT"
    assert g.evidence["halt_reason"] == "UNCLEAN_RESOLUTION"
