# tools/b4c_resolution.py
"""
Gate 4.5-B4-3 沙箱语义冲突消解与 Gen-3 审批卡构建。

严禁：checkout --ours/--theirs、网络推送、修改 calculator.py/test_calculator.py 以外文件。
"""
import difflib
import json
import os
import sys

from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR
from tools.approval_gate import canonical, sha256_hex
from tools.conflict_guard import agent_side_contract, upstream_side_contract
from tools.internal_tools import InternalTools

WORK = "aie_b4n_work"
STATE = "b4n_state.json"

RESOLVED_CALC = '''def add(a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("add requires integer arguments")
    return a + b


def multiply(a: int | float, b: int | float) -> int | float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("multiply requires numeric arguments")
    result = a * b
    if abs(result) > 1_000_000:
        raise ValueError("multiplication overflow limit exceeded")
    return result


def divide(a: int, b: int) -> float:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("divide requires integer arguments")
    if b == 0:
        raise ValueError("divisor must not be zero")
    return a / b
'''

RESOLVED_TEST = '''from calculator import add, multiply, divide

import pytest


def test_add_rejects_non_numeric():
    with pytest.raises(TypeError):
        add("2", 3)


def test_multiply():
    assert multiply(2, 3) == 6


def test_multiply_negative():
    assert multiply(-2, 3) == -6




def test_divide():
    assert divide(6, 3) == 2


def test_divide_by_zero():
    with pytest.raises(ValueError):
        divide(1, 0)


def test_divide_rejects_non_numeric():
    with pytest.raises(TypeError):
        divide("6", 3)


def test_multiply_accepts_float():
    assert multiply(2, 3.5) == 7.0


def test_multiply_non_numeric_rejected():
    with pytest.raises(TypeError):
        multiply("2", 3)


def test_multiply_overflow_rejected():
    with pytest.raises(ValueError):
        multiply(2000, 2000)
'''


def log(msg):
    print(f"[B4C-R] {msg}", flush=True)


def main():
    st = json.load(open(STATE, encoding="utf-8"))
    m2, m3, c2star = st["m2"], st["m3"], st["c2star"]
    st["fsm"] = "CONFLICT_SANDBOX_RESOLVING"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 快照先行：未解决现场物理留档（仓库目录之外）
    code, out, err = exec_in_sandbox(
        f"cp /workspace/{WORK}/calculator.py /workspace/snapshot_unmerged.txt && wc -l "
        f"/workspace/snapshot_unmerged.txt", timeout=60)
    assert code == 0, err or out
    log(f"snapshot_unmerged captured ({out.strip()})")

    # 手工语义融合（仅两文件）
    wr = InternalTools.write_file(f"{WORK}/calculator.py", RESOLVED_CALC)
    assert wr["success"], wr
    wr = InternalTools.write_file(f"{WORK}/test_calculator.py", RESOLVED_TEST)
    assert wr["success"], wr
    for fname in ("calculator.py", "test_calculator.py"):
        os.chown(os.path.join(WORKSPACE_DIR, WORK, fname), 1000, 1000)

    # 结构完整性：标记清零
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && grep -rn -E '<<<<<<<|=======|>>>>>>>' . ; echo GREP_EXIT=$?",
        timeout=60)
    assert "GREP_EXIT=1" in out, f"conflict markers remain: {out[:200]}"
    log("conflict markers fully cleared")

    # 提交 Merge Commit C3
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git add calculator.py test_calculator.py && "
        f"git commit -m 'Resolve conflict: merge M3 numeric semantics with C2* divide' && "
        f"git rev-parse HEAD && git rev-parse HEAD^1 && git rev-parse HEAD^2 && "
        f"git rev-parse HEAD^{{tree}}", timeout=120)
    assert code == 0, f"commit failed: {err or out[:300]}"
    c3, p0, p1, tree_sha = [l for l in out.splitlines() if l.strip()][-4:]
    assert p0 == c2star, f"C3.parents[0]={p0} != C2*"
    assert p1 == m3, f"C3.parents[1]={p1} != M3"
    log(f"C3={c3} parents=[C2*, M3] tree={tree_sha}")

    code, out, err = exec_in_sandbox(f"cd /workspace/{WORK} && git status --porcelain", timeout=60)
    assert not any(l.split() and l.split()[0] in {"UU", "AA", "AU", "UA", "DD", "UD", "DU"}
                   for l in out.splitlines()), f"unmerged entries remain: {out[:200]}"
    log("unmerged index cleared")

    # 三维差分解耦
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git diff {m2}..{m3}", timeout=60)
    upstream_delta = canonical(out)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git diff {m2}..{c2star}", timeout=60)
    business_delta = canonical(out)
    with open(os.path.join(WORKSPACE_DIR, "snapshot_unmerged.txt"), encoding="utf-8") as f:
        snapshot_text = f.read()
    code, out, err = exec_in_sandbox(f"cd /workspace/{WORK} && cat calculator.py", timeout=60)
    resolution_delta = "\n".join(difflib.unified_diff(
        snapshot_text.splitlines(), out.splitlines(),
        fromfile="snapshot_unmerged/calculator.py", tofile="c3/calculator.py", lineterm=""))
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git diff --name-only {m3}..{c3}", timeout=60)
    changed_vs_m3 = {l for l in out.splitlines() if l.strip()}
    assert changed_vs_m3 <= {"calculator.py", "test_calculator.py"}, \
        f"SCOPE VIOLATED: {sorted(changed_vs_m3)}"
    log(f"three-way deltas computed; scope vs M3: {sorted(changed_vs_m3)}")

    # 双向语义契约 + 全量回归
    st["fsm"] = "GEN_3_VERIFYING"
    agent_ok = agent_side_contract(RESOLVED_CALC)
    upstream_ok = upstream_side_contract(RESOLVED_CALC)
    assert agent_ok["pass"] and upstream_ok["pass"], (agent_ok, upstream_ok)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && rm -rf __pycache__ && "
        f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q", timeout=180)
    assert code == 0, f"pytest failed: {err or out[:300]}"
    log(f"contracts PASS + pytest exit 0 ({out.strip().splitlines()[-1]})")

    # Gen-3 工件哈希（11 要素）
    up_hash = sha256_hex(upstream_delta)
    biz_hash = sha256_hex(business_delta)
    res_hash = sha256_hex(canonical(resolution_delta))
    artifact_hash = sha256_hex(canonical(
        f"gen3|{m2}|{m3}|{c2star}|{c3}|{p0}|{p1}|{tree_sha}|{up_hash}|{biz_hash}|{res_hash}"))
    st.update({"c3": c3, "tree_sha": tree_sha, "artifact_hash_gen3": artifact_hash,
               "upstream_delta_hash": up_hash, "business_delta_hash": biz_hash,
               "resolution_delta_hash": res_hash, "fsm": "GEN_3_WAITING_APPROVAL"})
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print()
    print("==================== [ HUMAN APPROVAL CARD (B4-3 Gen-3) ] ====================")
    print(f"generation:            3")
    print(f"ancestor_base_sha:     {m2}")
    print(f"new_approved_base_sha: {m3}")
    print(f"parent_1 (Agent C2*):  {c2star}")
    print(f"parent_2 (Upstream M3): {m3}")
    print(f"sync_merge_commit:     {c3}")
    print(f"final_tree_sha:        {tree_sha}")
    print(f"resolution_scope:      ['calculator.py', 'test_calculator.py']")
    print(f"upstream_delta_summary:    [{up_hash[:16]}...]")
    print(f"original_business_delta_summary: [{biz_hash[:16]}...]")
    print(f"resolution_delta_summary: [{res_hash[:16]}...]")
    print(f"bidirectional_contract: PASS")
    print(f"test_result:           EXIT 0")
    print(f"ARTIFACT HASH (Gen-3): {artifact_hash}")
    print("================================================================================")
    print()
    print("[B4C-R] PHYSICAL STOP. Zero push. Awaiting human review.")
    print(f"[B4C-R] artifact_hash_gen3={artifact_hash}")


if __name__ == "__main__":
    main()
