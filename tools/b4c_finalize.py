# tools/b4c_finalize.py
"""
Gate 4.5-B4-3 Gen-3 放行闭环：Anti-TOCTOU -> 无二次漂移 -> Fork 推送 -> B4_3_CLOSED。
"""
import difflib
import json
import time

import requests

from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR
from tools.approval_gate import canonical, sha256_hex
from tools.b3_fork_driver import ForkPusher
from tools.github_enricher import load_github_token

UPSTREAM_REPO = "cc-mug-up-lab/aie-b3-upstream"
FORK_REPO = "Cc-mug-up/aie-b3-upstream"
BRANCH = "agent/b4-real-drift"
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner"}
STATE = "b4n_state.json"

# 逐字冲突现场（此前 cat 取证原样，28 行）
CONFLICTED = '''def add(a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("add requires integer arguments")
    return a + b


<<<<<<< HEAD
def multiply(a: int, b: int) -> int:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("multiply requires integer arguments")
    return a * b


def divide(a: int, b: int) -> float:
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError("divide requires integer arguments")
    if b == 0:
        raise ValueError("divisor must not be zero")
    return a / b
=======
def multiply(a: int | float, b: int | float) -> int | float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise TypeError("multiply requires numeric arguments")
    result = a * b
    if abs(result) > 1_000_000:
        raise ValueError("multiplication overflow limit exceeded")
    return result
>>>>>>> 14a3c7771f43b5cffc09c1361681ce86e092d6cc
'''


def main():
    st = json.load(open(STATE, encoding="utf-8"))
    signed = st["artifact_hash_gen3"]
    dt = st["delta_texts"]  # 持久化真理源（可复现重算）

    # 1. 现场重算 Anti-TOCTOU（11 要素，基于持久化 delta 文本）
    up_hash = sha256_hex(canonical(dt["upstream_delta"]))
    biz_hash = sha256_hex(canonical(dt["business_delta"]))
    resolution_delta = "\n".join(difflib.unified_diff(
        dt["snapshot_unmerged"].splitlines(), dt["resolved_calculator"].splitlines(),
        fromfile="snapshot_unmerged/calculator.py", tofile="c3/calculator.py", lineterm=""))
    res_hash = sha256_hex(canonical(resolution_delta))
    recalc = sha256_hex(canonical(
        f"gen3|{st['m2']}|{st['m3']}|{st['c2star']}|{st['c3']}|{st['c2star']}|{st['m3']}"
        f"|{st['tree_sha']}|{up_hash}|{biz_hash}|{res_hash}"))
    assert recalc == signed, f"ANTI-TOCTOU FAIL: {recalc} != {signed}"
    # 双路印证：与 Git 现场重读一致
    code, out, err = exec_in_sandbox(
        f"cd /workspace/aie_b4n_work && git diff {st['m2']}..{st['m3']}", timeout=60)
    assert sha256_hex(canonical(out)) == up_hash, "upstream delta drifted from persisted text"
    print(f"[B4C-F] Anti-TOCTOU recompute: MATCH ({signed[:16]}...)")

    # 2. 无二次漂移
    cur = requests.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
                       headers=H, timeout=20).json()["sha"]
    assert cur == st["m3"], f"SECOND DRIFT: {cur} != {st['m3']}"
    print(f"[B4C-F] upstream main == M3 ({cur[:12]}...): NO SECOND DRIFT")

    # 3. Fork 推送（唯一写入通道；网络波次抖动下重试，推送幂等）
    pusher = ForkPusher(FORK_REPO, UPSTREAM_REPO, TOKEN)
    res = None
    t0 = time.perf_counter()
    for attempt in range(3):
        try:
            res = pusher.push(FORK_REPO, BRANCH, st["c3"], WORKSPACE_DIR + "/aie_b4n_work")
        except Exception as e:
            print(f"[B4C-F] push attempt {attempt + 1} failed: {str(e)[:120]}")
            time.sleep(10)
            continue
        if res.get("success"):
            break
        time.sleep(10)
    push_ms = (time.perf_counter() - t0) * 1000
    assert res and res.get("success"), res
    print(f"[B4C-F] pushed C3 to fork ({push_ms:.0f}ms)")

    # 4. 物理断言
    assert pusher.upstream_push_counter == 0
    st["fsm"] = "B4_3_CLOSED"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print()
    print("B4-3_FINAL_CLOSURE")
    print(f"exit_status=PASS")
    print(f"signature_verified=True")
    print(f"anti_toctou_recompute=MATCH")
    print(f"upstream_main={cur}")
    print(f"approved_base_sha={st['m3']}")
    print(f"second_drift=False")
    print(f"fork_push_ok=True (branch={BRANCH}, commit={st['c3']})")
    print(f"upstream_push_counter={pusher.upstream_push_counter}")
    print(f"fsm={st['fsm']}")
    print(f"c2star={st['c2star']}")
    print(f"c3={st['c3']}")
    print(f"tree_sha={st['tree_sha']}")
    print(f"artifact_hash_gen3={st['artifact_hash_gen3']}")
    print(f"push_ms={push_ms:.0f}")


if __name__ == "__main__":
    main()
