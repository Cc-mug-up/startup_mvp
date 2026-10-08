# tools/b4_nontrivial_drift.py
"""
Gate 4.5-B4-2 非平凡拓扑完整演练（裁决后重做）。

阶段：
  gen1_build   从 M1 签出 agent/b4-real-drift -> divide 实现+单测 -> C1* -> 锚定 M1
  watch        只读探针捕获外部注入的 M2 -> GEN_1_INVALIDATED -> 停机
  merge_verify no-ff 真实合并 -> C2*（parents [C1*, M2]）-> 非平凡 Dual-Delta -> 审批卡停机

严禁：跳卡直推、upstream 写、改写历史。
"""
import json
import os
import sys
import time

import requests

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.approval_gate import canonical, sha256_hex
from tools.github_enricher import load_github_token
from tools.internal_tools import InternalTools

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
FORK_REPO = os.environ.get("AIE_FORK_REPO", "")
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner"}
WORK = "aie_b4n_work"
BRANCH = "agent/b4-real-drift"
STATE = "b4n_state.json"

PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -8,3 +8,11 @@\n"
    "     if not isinstance(a, int) or not isinstance(b, int):\n"
    "         raise TypeError(\"multiply requires integer arguments\")\n"
    "     return a * b\n"
    "+\n"
    "+\n"
    "+def divide(a: int, b: int) -> float:\n"
    "+    if not isinstance(a, int) or not isinstance(b, int):\n"
    "+        raise TypeError(\"divide requires integer arguments\")\n"
    "+    if b == 0:\n"
    "+        raise ValueError(\"divisor must not be zero\")\n"
    "+    return a / b\n"
    "--- a/test_calculator.py\n"
    "+++ b/test_calculator.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-from calculator import add, multiply\n"
    "+from calculator import add, multiply, divide\n"
    " \n"
    " import pytest\n"
    "@@ -15,4 +15,18 @@\n"
    " def test_multiply_negative():\n"
    "     assert multiply(-2, 3) == -6\n"
    " \n"
    " \n"
    "+\n"
    "+\n"
    "+def test_divide():\n"
    "+    assert divide(6, 3) == 2\n"
    "+\n"
    "+\n"
    "+def test_divide_by_zero():\n"
    "+    with pytest.raises(ValueError):\n"
    "+        divide(1, 0)\n"
    "+\n"
    "+\n"
    "+def test_divide_rejects_non_numeric():\n"
    "+    with pytest.raises(TypeError):\n"
    "+        divide(\"6\", 3)\n"
)


def log(msg):
    print(f"[B4N] {msg}", flush=True)


def _sh(cmd, timeout=180):
    return exec_in_sandbox(cmd, timeout=timeout)


def _norm_patch(text):
    lines = []
    for ln in canonical(text).split("\n"):
        if ln.startswith("@@"):
            lines.append("@@")
        elif ln.startswith(("diff --git", "index ")):
            continue
        else:
            lines.append(ln)
    return "\n".join(lines)


def phase_gen1_build():
    stop_sandbox()
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    # 1. 从 M1 签出全新分支
    for attempt in range(8):
        code, out, err = _sh(
            f"cd /workspace && rm -rf {WORK} && git clone -q --depth 5 https://github.com/{FORK_REPO} {WORK} "
            f"&& cd {WORK} && git remote add upstream https://github.com/{UPSTREAM_REPO} "
            f"&& git remote set-url --push upstream no_push://upstream_disabled "
            f"&& git fetch -q upstream main && git rev-parse upstream/main", timeout=180)
        if code == 0:
            break
        log(f"clone attempt {attempt + 1}/8 failed: {(err or out)[:120]}")
        time.sleep(15)
    assert code == 0, "clone failed"
    m1 = out.strip().splitlines()[-1]
    # 2. approved_base_sha = M1（新锚点，动态）
    rec = json.load(open("task_lifecycle_record.json", encoding="utf-8"))
    rec["approved_base_sha"] = m1
    rec["fsm"] = "AWAITING_REVIEW"
    json.dump(rec, open("task_lifecycle_record.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    log(f"approved_base_sha anchored to M1={m1}")

    wr = InternalTools.write_file(f"{WORK}/.agent_patch.diff", PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, WORK, ".agent_patch.diff"), 1000, 1000)
    code, out, err = _sh(
        f"cd /workspace/{WORK} && git config user.email 'agent@aie.local' "
        f"&& git config user.name 'AIE Agent' "
        f"&& git checkout -q -b {BRANCH} upstream/main "
        f"&& git apply .agent_patch.diff && rm .agent_patch.diff && rm -rf __pycache__ "
        f"&& printf '__pycache__/\\n*.pyc\\n' > .gitignore "
        f"&& PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Add divide (aie-b4-2 nontrivial)' && git rev-parse HEAD",
        timeout=180)
    assert code == 0, f"gen1 build failed: {err or out[:400]}"
    c1star = out.strip().splitlines()[-1]
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m1}..{c1star}")
    biz_files = sorted({l for l in out.splitlines() if l.strip()})
    log(f"C1*={c1star} business_files={biz_files}")
    # 5. 非空且恰好两个文件
    assert biz_files == ["calculator.py", "test_calculator.py"], f"business files {biz_files}"
    json.dump({"m1": m1, "c1star": c1star, "branch": BRANCH,
               "business_files": biz_files, "fsm": "AWAITING_REVIEW"},
              open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log("C1* committed; awaiting external M2 injection.")


def phase_watch():
    st = json.load(open(STATE, encoding="utf-8"))
    m1 = st["m1"]
    for i in range(60):
        resp = requests.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
                            headers=H, timeout=20)
        cur = resp.json()["sha"]
        if cur != m1:
            st["m2"] = cur
            st["fsm"] = "GEN_1_INVALIDATED"
            json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            log(f"M2 CAPTURED={cur} fsm=GEN_1_INVALIDATED")
            print("[B4N] PHYSICAL STOP. Run 'merge_verify'.")
            return
        if i % 4 == 0:
            log(f"watch poll {i + 1}/60: no drift")
        time.sleep(15)
    log("watch window exhausted; re-run watch.")


def phase_merge_verify():
    st = json.load(open(STATE, encoding="utf-8"))
    m1, c1star, m2 = st["m1"], st["c1star"], st["m2"]
    # 幂等合并：HEAD 双亲已是 [C1*, M2] 则采用，否则执行 no-ff 合并
    code, out, err = _sh(f"cd /workspace/{WORK} && git fetch -q upstream main && "
                         f"git rev-parse HEAD^1 2>/dev/null; git rev-parse HEAD^2 2>/dev/null; "
                         f"git rev-parse HEAD")
    pre = [l for l in out.splitlines() if l.strip()]
    if len(pre) == 3 and pre[0] == c1star and pre[1] == m2:
        log("merge already present (idempotent)")
    else:
        code, out, err = _sh(f"cd /workspace/{WORK} && git merge --no-ff upstream/main")
        assert code == 0, f"merge failed: {err or out[:300]}"
    code, out, err = _sh(f"cd /workspace/{WORK} && git rev-parse HEAD && git rev-parse HEAD^1 "
                         f"&& git rev-parse HEAD^2 && git rev-parse HEAD^{{tree}}")
    c2star, p0, p1, tree_sha = [l for l in out.splitlines() if l.strip()][:4]
    # DAG 拓扑物理断言
    assert p0 == c1star, f"parents[0]={p0} != C1*"
    assert p1 == m2, f"parents[1]={p1} != M2"
    log(f"C2*={c2star} parents=[C1*, M2] tree={tree_sha}")

    # 非平凡 Dual-Delta 硬断言
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m1}..{c1star}")
    biz_files = sorted({l for l in out.splitlines() if l.strip()})
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m2}..{c2star}")
    aligned_files = sorted({l for l in out.splitlines() if l.strip()})
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m1}..{m2}")
    external_files = sorted({l for l in out.splitlines() if l.strip()})
    assert biz_files == ["calculator.py", "test_calculator.py"], "biz files wrong"  # 严禁为空
    assert aligned_files == biz_files, f"aligned {aligned_files} != biz {biz_files}"
    assert not (set(biz_files) & set(external_files)), "external leak into business scope"
    biz_diff = _sh(f"cd /workspace/{WORK} && git diff {m1}..{c1star}")[1]
    aligned_diff = _sh(f"cd /workspace/{WORK} && git diff {m2}..{c2star}")[1]
    assert _norm_patch(aligned_diff) == _norm_patch(biz_diff), "semantic mismatch"
    log(f"dual-delta NONTRIVIAL VERIFIED: biz={biz_files} external={external_files}")

    code, out, err = _sh(f"cd /workspace/{WORK} && rm -rf __pycache__ && "
                         f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q")
    assert code == 0, f"pytest failed: {err or out[:300]}"
    log("pytest exit 0 on C2* tree")

    business_patch_hash = sha256_hex(_norm_patch(biz_diff))
    upstream_sync_diff_hash = sha256_hex(_norm_patch(
        _sh(f"cd /workspace/{WORK} && git diff {m1}..{m2}")[1]))
    artifact_hash = sha256_hex(canonical(
        f"gen2|{m1}|{m2}|{c1star}|{c2star}|{p0}|{p1}|{tree_sha}"
        f"|{business_patch_hash}|{upstream_sync_diff_hash}"))
    st.update({"c2star": c2star, "tree_sha": tree_sha, "m2": m2,
               "artifact_hash_gen2": artifact_hash,
               "business_patch_hash": business_patch_hash,
               "upstream_sync_diff_hash": upstream_sync_diff_hash,
               "external_files": external_files,
               "fsm": "GEN_2_WAITING_APPROVAL"})
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print()
    print("==================== [ HUMAN APPROVAL CARD (B4-2 Gen-2 NONTRIVIAL) ] ====================")
    print(f"generation:            2")
    print(f"old_approved_base_sha: {m1}")
    print(f"new_approved_base_sha: {m2}")
    print(f"parent_1 (Agent C1*):  {c1star}")
    print(f"parent_2 (Upstream M2): {m2}")
    print(f"sync_merge_commit:     {c2star}")
    print(f"final_tree_sha:        {tree_sha}")
    print(f"business_changed_files: {biz_files}")
    print(f"external_synced_files:  {external_files}")
    print(f"dual_delta_status:      VERIFIED_CLEAN (NONTRIVIAL)")
    print(f"test_result:            EXIT 0")
    print(f"BUSINESS PATCH HASH:    {business_patch_hash}")
    print(f"UPSTREAM SYNC HASH:     {upstream_sync_diff_hash}")
    print(f"ARTIFACT HASH (Gen-2):  {artifact_hash}")
    print("=======================================================================================")
    print()
    print("[B4N] PHYSICAL STOP. No git push. Awaiting human review.")
    print(f"[B4N] artifact_hash_gen2={artifact_hash}")


if __name__ == "__main__":
    if not UPSTREAM_REPO or not FORK_REPO:
        print("AIE_UPSTREAM_REPO / AIE_FORK_REPO required (parameterized).")
        sys.exit(1)
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    {"gen1_build": phase_gen1_build, "watch": phase_watch,
     "merge_verify": phase_merge_verify}.get(phase, lambda: print(
        "usage: gen1_build | watch | merge_verify"))()
