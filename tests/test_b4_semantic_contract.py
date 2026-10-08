# tests/test_b4_semantic_contract.py
# Gate 4.5-B4-3 双向语义契约独立测试模块（纯本地 / 零网络）
from tools.conflict_guard import agent_side_contract, upstream_side_contract

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

# --ours 暴力覆盖产物：丢弃上游类型放宽与幅值守卫
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


# ---------- Agent 侧契约 ----------
def test_agent_contract_passes_good_code():
    r = agent_side_contract(GOOD_CODE)
    assert r["pass"] is True, r["failures"]


def test_agent_contract_detects_missing_divide():
    r = agent_side_contract(OURS_ONLY_CODE.replace("def divide", "def noop").replace("divide(", "noop("))
    assert r["pass"] is False
    assert any("divide missing" in f for f in r["failures"])


def test_agent_contract_detects_zero_division_not_raising():
    bad = GOOD_CODE.replace('raise ValueError("divisor must not be zero")', "return 0")
    r = agent_side_contract(bad)
    assert r["pass"] is False
    assert any("did not raise ValueError" in f for f in r["failures"])


def test_agent_contract_detects_negative_sign_break():
    bad = GOOD_CODE.replace("result = a * b", "result = abs(a * b)")
    r = agent_side_contract(bad)
    assert r["pass"] is False
    assert any("negative multiply sign broken" in f for f in r["failures"])


# ---------- Upstream 侧契约 ----------
def test_upstream_contract_passes_good_code():
    r = upstream_side_contract(GOOD_CODE)
    assert r["pass"] is True, r["failures"]


def test_upstream_contract_rejects_int_only_bruteforce():
    r = upstream_side_contract(OURS_ONLY_CODE)
    assert r["pass"] is False
    assert any("float/int relaxation broken" in f for f in r["failures"])


def test_upstream_contract_detects_huge_result_allowed():
    bad = GOOD_CODE.replace(
        'if abs(result) > 1_000_000:\n        raise ValueError("result magnitude too large")', "")
    r = upstream_side_contract(bad)
    assert r["pass"] is False
    assert any("huge result did not raise ValueError" in f for f in r["failures"])


def test_upstream_contract_detects_non_numeric_allowed():
    bad = GOOD_CODE.replace(
        'raise TypeError("multiply requires numeric arguments")', "pass")
    r = upstream_side_contract(bad)
    assert r["pass"] is False
    assert any("non-numeric" in f for f in r["failures"])  # 守卫缺失或守卫外 TypeError 均判失败
