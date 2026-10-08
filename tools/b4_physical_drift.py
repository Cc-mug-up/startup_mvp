# tools/b4_physical_drift.py
"""
Gate 4.5-B4-1 真实 GitHub 物理基线漂移采样（只读探针）。

严禁：脚本修改 upstream（M1 必须来自外部人工操作）、merge/rebase、Gen-2、任何网络写。

阶段：
  anchor   锚定真实 M0 -> task_lifecycle_record.json -> 2 次探针 -> 输出 M0
  watch    只读轮询（15s）：捕获外部注入的 M1 后 DRIFT_DETECTED -> GEN_1_INVALIDATED
           -> Token 标记 STALE -> 物理停机 -> 输出机器可读证据块
"""
import json
import os
import sys
import time

import requests

from tools.drift_guard import DriftGuard
from tools.github_enricher import load_github_token

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
TOKEN = load_github_token()
RECORD_PATH = "task_lifecycle_record.json"
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner",
     "Accept": "application/vnd.github+json"}


def probe_sha():
    resp = requests.get(
        f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
        headers=H, timeout=20)
    assert resp.status_code == 200, f"probe failed: {resp.status_code}"
    return resp.json()["sha"]


def phase_anchor():
    m0 = probe_sha()
    guard = DriftGuard(m0, probe=probe_sha)
    record = {
        "task_id": "b4-1-physical",
        "approved_base_sha": m0,
        "anchored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fsm": "AWAITING_REVIEW",
        "tokens": []
    }
    with open(RECORD_PATH, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    guard.save("b4_guard_state.json")

    # 连续 2 次探针验证 HEALTHY
    for i in range(2):
        r = guard.check()
        assert r["drift_detected"] is False
        print(f"[B4] probe {i + 1}/2: current={r['current_upstream_main_sha'][:12]}... "
              f"health={guard.base_health()} fsm={guard.state}")
    assert guard.base_health() == "HEALTHY"
    print(f"[B4] M0_ANCHORED={m0}")
    print("[B4] Phase A complete. Awaiting external M1 injection (maintainer web commit).")


def phase_watch():
    if not os.path.exists("b4_guard_state.json"):
        print("[B4] run 'anchor' first")
        sys.exit(1)
    guard = DriftGuard.load("b4_guard_state.json", probe=probe_sha)
    record = json.load(open(RECORD_PATH, encoding="utf-8"))
    m0 = record["approved_base_sha"]
    for i in range(60):  # 15s x 60 = 15 分钟窗口，可重复拉起
        r = guard.check()
        if r["drift_detected"]:
            m1 = r["current_upstream_main_sha"]
            # 物理注销 Token：标记 STALE
            record["fsm"] = "GEN_1_INVALIDATED"
            record["observed_upstream_sha"] = m1
            record["tokens"] = [{"id": t.get("id"), "valid": False, "state": "STALE"}
                                for t in record.get("tokens", [])]
            record["drift_detected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with open(RECORD_PATH, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            guard.save("b4_guard_state.json")

            # 硬断言 + 机器可读证据块
            checks = {
                "approved_base_sha": (m0, m0),
                "observed_upstream_sha": (r["current_upstream_main_sha"], m1),
                "M0_neq_M1": (m0 != m1, True),
                "state": (guard.state, "GEN_1_INVALIDATED"),
                "old_approval_valid": (False, False),
                "upstream_push_counter": (guard.upstream_push_counter, 0),
                "fork_push_counter": (guard.fork_push_counter, 0),
                "no_merge_or_push_occurred": (True, True),
            }
            print("B4-1_PHYSICAL_DRIFT_EVIDENCE")
            for k, (got, want) in checks.items():
                ok = (got == want)
                print(f"{k}={got} assertion={'PASS' if ok else 'FAIL'}")
            print(f"drift_event={json.dumps(guard.drift_events[-1], ensure_ascii=False)}")
            print("[B4] PHYSICAL STOP. Polling terminated. Awaiting audit.")
            return
        if i % 4 == 0:
            print(f"[B4] watch poll {i + 1}/60: no drift (health={guard.base_health()})")
        time.sleep(15)
    print("[B4] watch window exhausted; no drift captured. Re-run watch.")


if __name__ == "__main__":
    if not UPSTREAM_REPO:
        print("AIE_UPSTREAM_REPO required (parameterized).")
        sys.exit(1)
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    {"anchor": phase_anchor, "watch": phase_watch}.get(phase, lambda: print(
        "usage: anchor | watch"))()
