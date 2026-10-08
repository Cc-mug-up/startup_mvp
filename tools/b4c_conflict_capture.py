# tools/b4c_conflict_capture.py
"""
Gate 4.5-B4-3 真实网络物理冲突捕获与 Index 真值核验。

严禁：自动消解、git add/commit、checkout --ours/--theirs、任何网络推送、
git merge --abort、修改代码。冲突现场必须完整保留。
"""
import json
import os
import sys

import requests

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox
from tools.github_enricher import load_github_token

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner"}
WORK = "aie_b4n_work"
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def log(msg):
    print(f"[B4C] {msg}", flush=True)


def main():
    st = json.load(open("b4n_state.json", encoding="utf-8"))
    m2 = st["m2"]
    c2star = st["c2star"]

    # 一、捕获真实 M3
    m3 = requests.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
                      headers=H, timeout=20).json()["sha"]
    assert m3 != m2, f"M3 == M2: {m3}"
    st["m3"] = m3
    st["fsm"] = "GEN_2_INVALIDATED"
    json.dump(st, open("b4n_state.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"M3 captured={m3} (fsm=GEN_2_INVALIDATED, old token voided)")

    # 二、沙箱真实拉取与合并（基于 C2* 工作树）
    start_sandbox()
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git fetch -q upstream main && git merge --no-ff FETCH_HEAD",
        timeout=180)
    merge_exit = code
    merge_output = (out + err).strip()
    log(f"merge_exit_code={merge_exit}")
    log(f"merge_output={merge_output[:200]}")
    assert merge_exit != 0, "merge unexpectedly succeeded"

    # 三、物理 Index 真值判定（Index-First Ground Truth）
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git status --porcelain", timeout=60)
    porcelain = out
    log(f"git status --porcelain:\n{porcelain}")
    assert "UU calculator.py" in porcelain, "unmerged index marker UU missing"

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && grep -n -E '<<<<<<<|=======|>>>>>>>' calculator.py | head -8",
        timeout=60)
    marker_lines = out
    log(f"conflict markers:\n{marker_lines}")
    assert all(m in marker_lines for m in CONFLICT_MARKERS), "conflict text markers missing"

    # 快照基准：冲突现场保持完整（禁止 abort/修改）
    st["fsm"] = "CONFLICT_DETECTED"
    st["merge_exit_code"] = merge_exit
    st["snapshot_kept"] = True
    json.dump(st, open("b4n_state.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print()
    print("B4-3_PHYSICAL_CONFLICT_EVIDENCE")
    print(f"M2={m2}")
    print(f"M3={m3}")
    print(f"C2*={c2star}")
    print(f"merge_exit_code={merge_exit}")
    print(f"unmerged_index=UU calculator.py")
    print(f"conflict_markers_present={all(m in marker_lines for m in CONFLICT_MARKERS)}")
    print(f"marker_lines={marker_lines.strip()!r}")
    print(f"fsm={st['fsm']}")
    print("snapshot_unmerged=KEPT (no abort, no modification)")
    print("[B4C] PHYSICAL STOP. Awaiting resolution directive.")


if __name__ == "__main__":
    main()
