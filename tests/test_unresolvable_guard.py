# tests/test_unresolvable_guard.py
# Gate 4.5-B4-4 Phase 1 单元测试（纯本地 / 零网络 / 零写入）
import json
import os

from tools.unresolvable_guard import UnresolvableResolver

M2 = "m2" * 20
C2S = "c2" * 20
M3 = "m3" * 20
C3 = "c3" * 20

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

# 语义互斥代码：divide 除零不抛错（Agent 侧契约永久失败）
MUTUALLY_EXCLUSIVE_CODE = GOOD_CODE.replace(
    'raise ValueError("divisor must not be zero")', "return 0")


class FakeSandbox:
    def __init__(self, code=GOOD_CODE, markers=None, unmerged=False, diff_lines=10,
                 failed_log=""):
        self.code = code
        self.markers = markers if markers is not None else []
        self.unmerged = unmerged
        self._diff_lines = diff_lines
        self._failed_log = failed_log

    def status(self):
        return "UU calculator.py\n" if self.unmerged else ""

    def scan_conflict_markers(self):
        return self.markers

    def status_unmerged(self):
        return self.unmerged

    def resolution_diff_lines(self):
        return self._diff_lines

    def failed_tests_log(self):
        return self._failed_log

    def read_code(self):
        return self.code


def _resolver(sb, bundle_dir, code=None):
    return UnresolvableResolver(sb, M2, C2S, M3,
                                allowed_scope=["calculator.py", "test_calculator.py"],
                                bundle_dir=bundle_dir)


# ------------------------------------------------------------------ #
# 用例 A：语义互斥 -> 尝试耗尽 -> UNRESOLVABLE_HALT (ATTEMPTS_EXCEEDED)
# ------------------------------------------------------------------ #
def test_attempts_exhausted_on_semantic_mutex(tmp_path):
    sb = FakeSandbox(code=MUTUALLY_EXCLUSIVE_CODE)
    bundle = str(tmp_path / "failure_bundle")
    r = _resolver(sb, bundle)

    r1 = r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    r2 = r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    assert r1 == "CONFLICT_SANDBOX_RESOLVING"  # 可重试
    assert r2 == "CONFLICT_SANDBOX_RESOLVING"

    r3 = r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    assert r3 == "UNRESOLVABLE_HALT"
    assert r.halt_reason == "ATTEMPTS_EXCEEDED"
    assert r.attempts == 3

    # 黑匣子五件套
    for fname in ("metadata.json", "git_status.txt", "failed_tests.log",
                  "constraints.json", "exploration_history.json"):
        assert os.path.exists(os.path.join(bundle, fname)), fname
    meta = json.load(open(os.path.join(bundle, "metadata.json"), encoding="utf-8"))
    assert meta["halt_reason"] == "ATTEMPTS_EXCEEDED"
    assert meta["counters"]["attempts"] == 3
    history = json.load(open(os.path.join(bundle, "exploration_history.json"),
                             encoding="utf-8"))
    assert len(history) == 3
    assert all(h["verdict"] == "SEMANTIC_CONTRACT_FAIL" for h in history)


# ------------------------------------------------------------------ #
# 用例 B：越权修改无关文件 -> SCOPE_EXCEEDED -> UNRESOLVABLE_HALT
# ------------------------------------------------------------------ #
def test_scope_exceeded_halt(tmp_path):
    sb = FakeSandbox()
    bundle = str(tmp_path / "failure_bundle_b")
    r = _resolver(sb, bundle)
    state = r.attempt_resolution(["calculator.py", "test_calculator.py", "README.md"],
                                 C3, [C2S, M3], pytest_exit=0)
    assert state == "UNRESOLVABLE_HALT"
    assert r.halt_reason == "SCOPE_EXCEEDED"
    meta = json.load(open(os.path.join(bundle, "metadata.json"), encoding="utf-8"))
    assert meta["halt_reason"] == "SCOPE_EXCEEDED"


# ------------------------------------------------------------------ #
# 用例 C：改动量超出预算 -> DIFF_BUDGET_EXCEEDED -> UNRESOLVABLE_HALT
# ------------------------------------------------------------------ #
def test_diff_budget_exceeded_halt(tmp_path):
    sb = FakeSandbox(diff_lines=120)  # > MAX_DIFF_LINES=50
    bundle = str(tmp_path / "failure_bundle_c")
    r = _resolver(sb, bundle)
    state = r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    assert state == "UNRESOLVABLE_HALT"
    assert r.halt_reason == "DIFF_BUDGET_EXCEEDED"
    cons = json.load(open(os.path.join(bundle, "constraints.json"), encoding="utf-8"))
    assert cons["max_diff_lines"] == 50 and cons["max_attempts"] == 3 and cons["max_files"] == 2


# ------------------------------------------------------------------ #
# 用例 D：终态吸收——审批卡硬阻断 + 网络写计数恒 0
# ------------------------------------------------------------------ #
def test_halt_blocks_card_and_writes(tmp_path):
    sb = FakeSandbox(diff_lines=120)
    bundle = str(tmp_path / "failure_bundle_d")
    r = _resolver(sb, bundle)
    r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    assert r.state == "UNRESOLVABLE_HALT"

    # 审批卡生成硬阻断
    assert r.generate_approval_card() is None
    assert r.card_allowed is False
    # 后续一切尝试无效（终态吸收）
    assert r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0) == "UNRESOLVABLE_HALT"
    assert r.attempts == 1  # 终态后不再计数
    # 网络写计数器恒 0
    assert r.network_write_counters == {"upstream": 0, "fork": 0}
    meta = json.load(open(os.path.join(bundle, "metadata.json"), encoding="utf-8"))
    assert meta["network_write_counters"] == {"upstream": 0, "fork": 0}
    assert meta["card_allowed"] is False


# ------------------------------------------------------------------ #
# 附加：正确消解路径绿灯（预算内/范围合规/契约双向 PASS）
# ------------------------------------------------------------------ #
def test_budgeted_correct_resolution_passes(tmp_path):
    sb = FakeSandbox(code=GOOD_CODE, diff_lines=10)
    bundle = str(tmp_path / "failure_bundle_e")
    r = _resolver(sb, bundle)
    state = r.attempt_resolution(["calculator.py"], C3, [C2S, M3], pytest_exit=0)
    assert state == "GEN_3_WAITING_APPROVAL"
    assert r.card_allowed is True
    assert r.generate_approval_card() is not None
    assert not os.path.exists(bundle)  # 未触发停机则不产生黑匣子
