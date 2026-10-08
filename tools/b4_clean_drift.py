# tools/b4_clean_drift.py
"""
Gate 4.5-B4-2 Clean Drift Recovery（无冲突基线漂移恢复）。

严禁：跳过审批卡直接推送、向 upstream 任何网络写、改写历史（--no-ff 强制）。
所有 SHA 动态对账读取，严禁硬编码。
"""
import json
import os
import sys

import requests

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.approval_gate import canonical, sha256_hex
from tools.github_enricher import load_github_token

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
FORK_REPO = os.environ.get("AIE_FORK_REPO", "")
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner"}
WORK = "aie_g3_work"
SCOPE_ALLOWED = {"calculator.py", "test_calculator.py"}


def log(msg):
    print(f"[B4-2] {msg}", flush=True)


def _sh(cmd, timeout=180):
    return exec_in_sandbox(cmd, timeout=timeout)


def _norm_patch(text: str) -> str:
    """剥离行号元数据后的规范化 Patch 内容（Dual-Delta 语义等价比对）。"""
    lines = []
    for ln in canonical(text).split("\n"):
        if ln.startswith("@@"):
            lines.append("@@")  # 行号元数据剥离
        elif ln.startswith(("diff --git", "index ")):
            continue  # 索引元数据剥离
        else:
            lines.append(ln)
    return "\n".join(lines)


def main():
    start_sandbox()
    # ---------- 一、不可变上下文动态读取 ----------
    rec = json.load(open("task_lifecycle_record.json", encoding="utf-8"))
    m0 = rec["approved_base_sha"]
    b3 = json.load(open("b3g3_state.json", encoding="utf-8"))
    c1 = b3["commit2_sha"]
    m1 = requests.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
                      headers=H, timeout=20).json()["sha"]
    log(f"M0={m0}")
    log(f"M1={m1}")
    log(f"C1={c1}")
    assert m0 != m1

    # ---------- 二、预检分析（DRIFT_ANALYZING） ----------
    code, out, err = _sh(f"cd /workspace/{WORK} && git fetch -q upstream main && "
                         f"git diff --name-only {m0}..{m1}")
    upstream_files = {l for l in out.splitlines() if l.strip()}
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m0}..{c1}")
    business_files = {l for l in out.splitlines() if l.strip()}
    inter = upstream_files & business_files
    log(f"UpstreamDelta files={sorted(upstream_files)}")
    log(f"BusinessDelta files={sorted(business_files)}")
    log(f"intersection={sorted(inter)}")
    assert not inter, "CONFLICT_DETECTED: clean drift only"
    rec["drift_analysis"] = {"status": "CLEAN", "upstream_files": sorted(upstream_files),
                             "business_files": sorted(business_files)}

    # ---------- 三、沙箱安全合并（SYNC_AUTO_MERGING） ----------
    # 幂等：若前次运行已完成 no-ff 合并（HEAD 双亲 == [C1, M1]），直接采用
    code, out, err = _sh(f"cd /workspace/{WORK} && git rev-parse HEAD^1 2>/dev/null; "
                         f"git rev-parse HEAD^2 2>/dev/null; git rev-parse HEAD")
    pre_lines = [l for l in out.splitlines() if l.strip()]
    if len(pre_lines) == 3 and pre_lines[0] == c1 and pre_lines[1] == m1:
        log("merge already present (idempotent path): HEAD parents == [C1, M1]")
    else:
        code, out, err = _sh(f"cd /workspace/{WORK} && git merge --no-ff upstream/main")
        assert code == 0, f"merge failed: {err or out}"
        assert "Merge made" in out or "merge" in out.lower()
    code, out, err = _sh(f"cd /workspace/{WORK} && git status --porcelain")
    status = out.strip()
    assert not status, f"dirty tree after merge: {status[:200]}"
    code, out, err = _sh(f"cd /workspace/{WORK} && git ls-files -u")
    assert not out.strip(), f"unresolved conflict markers present: {out[:200]}"
    code, out, err = _sh(f"cd /workspace/{WORK} && git rev-parse HEAD && git rev-parse HEAD^1 "
                         f"&& git rev-parse HEAD^2 && git rev-parse HEAD^{{tree}}")
    lines = [l for l in out.splitlines() if l.strip()]
    c2, p0, p1, tree_sha = lines[0], lines[1], lines[2], lines[3]
    # DAG 双亲拓扑硬断言
    assert p0 == c1, f"C2.parents[0]={p0} != C1={c1}"
    assert p1 == m1, f"C2.parents[1]={p1} != M1={m1}"
    log(f"C2={c2} parents=[C1, M1] tree={tree_sha}")

    # ---------- 四、Dual-Delta 提纯 ----------
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff {m1}..{c2}")
    aligned_diff = out
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff --name-only {m1}..{c2}")
    aligned_names = {l for l in out.splitlines() if l.strip()}
    code, out, err = _sh(f"cd /workspace/{WORK} && git diff {m0}..{c1}")
    business_diff = out
    # 断言 A：文件集合恒等
    assert aligned_names == business_files, f"A FAIL: {aligned_names} != {business_files}"
    # 断言 B：业务范围受限
    assert business_files <= SCOPE_ALLOWED, f"B FAIL: {sorted(business_files)}"
    # 断言 C：语义等价（剥离行号元数据后规范化内容一致）
    assert _norm_patch(aligned_diff) == _norm_patch(business_diff), "C FAIL: semantic drift"
    # 断言 D：外部隔离——UpstreamDelta 文件仅入 external_synced_files，
    # 绝不计入业务 Scope（业务 ∩ 上游 == ∅ 已在前置交集检查断言）
    external_synced = sorted(upstream_files)
    assert not (set(external_synced) & business_files), "D FAIL: external files leaked into business scope"
    business_patch_hash = sha256_hex(_norm_patch(business_diff))
    upstream_sync_diff_hash = sha256_hex(_norm_patch(
        _sh(f"cd /workspace/{WORK} && git diff {m0}..{m1}")[1]))
    log(f"dual-delta: A/B/C/D VERIFIED_CLEAN; business_patch_hash={business_patch_hash}")

    # ---------- 五、沙箱全量回归（GEN_2_VERIFYING） ----------
    code, out, err = _sh(f"cd /workspace/{WORK} && rm -rf __pycache__ && "
                         f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q")
    assert code == 0, f"pytest failed: {err or out[:300]}"
    log("pytest exit 0 on C2 tree")

    # ---------- 六、审批卡构建与物理停机（GEN_2_WAITING_APPROVAL） ----------
    artifact_hash = sha256_hex(canonical(
        f"gen2|{m0}|{m1}|{c1}|{c2}|{p0}|{p1}|{tree_sha}|{business_patch_hash}"
        f"|{upstream_sync_diff_hash}"))
    rec.update({
        "fsm": "GEN_2_WAITING_APPROVAL",
        "new_approved_base_sha": m1,
        "sync_merge_commit": c2,
        "final_tree_sha": tree_sha,
        "business_patch_hash": business_patch_hash,
        "upstream_sync_diff_hash": upstream_sync_diff_hash,
        "artifact_hash_gen2": artifact_hash,
        "gen1_token_state": "STALE"
    })
    json.dump(rec, open("task_lifecycle_record.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print()
    print("==================== [ HUMAN APPROVAL CARD (B4-2 Gen-2) ] ====================")
    print(f"generation:            2")
    print(f"old_approved_base_sha: {m0}")
    print(f"new_approved_base_sha: {m1}")
    print(f"parent_1 (Agent Gen-1): {c1}")
    print(f"parent_2 (Upstream M1): {m1}")
    print(f"sync_merge_commit:     {c2}")
    print(f"final_tree_sha:        {tree_sha}")
    print(f"business_changed_files: {sorted(business_files)}")
    print(f"external_synced_files:  {sorted(upstream_files)}")
    print(f"dual_delta_status:      VERIFIED_CLEAN")
    print(f"test_result:            EXIT 0")
    print(f"BUSINESS PATCH HASH:    {business_patch_hash}")
    print(f"UPSTREAM SYNC HASH:     {upstream_sync_diff_hash}")
    print(f"ARTIFACT HASH (Gen-2):  {artifact_hash}")
    print("================================================================================")
    print()
    print("[B4-2] PHYSICAL STOP. Gen-1 token marked STALE. No git push. Awaiting human review.")
    print(f"[B4-2] artifact_hash_gen2={artifact_hash}")


if __name__ == "__main__":
    main()
