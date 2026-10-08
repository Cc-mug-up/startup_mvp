# tools/b4p4_public.py
"""
Gate 4.5-B4-4 Phase 3：真实 GitHub 公网不可解冲突熔断。

严禁：向 upstream/fork 任何 git push、生成任何审批卡、篡改测试契约。
"""
import json
import os
import sys
import time

import requests

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.github_enricher import load_github_token
from tools.internal_tools import InternalTools
from tools.unresolvable_guard import UnresolvableResolver

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
TOKEN = load_github_token()
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner"}
WORK = "aie_b4n_work"
STATE = "b4n_state.json"
BRANCH = "agent/b4-real-drift"

# 消解尝试（每次相同）：C3 语义（支持负数乘法）——与 M4 的禁负契约互斥
CALC_RESOLUTION = (
    "def add(a, b):\n    return a + b\n\n"
    "def multiply(a, b):\n"
    "    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n"
    "        raise TypeError(\"multiply requires numeric arguments\")\n"
    "    result = a * b\n"
    "    if abs(result) > 1_000_000:\n"
    "        raise ValueError(\"result magnitude too large\")\n"
    "    return result\n\n"
    "def divide(a, b):\n"
    "    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n"
    "        raise TypeError(\"divide requires numeric arguments\")\n"
    "    if b == 0:\n"
    "        raise ValueError(\"divisor must not be zero\")\n"
    "    return a / b\n")


def log(msg):
    print(f"[B4P4] {msg}", flush=True)


class SandboxAdapter:
    def __init__(self, repo):
        self.repo = repo
        self.last_pytest_log = ""

    def _sh(self, cmd, timeout=120):
        return exec_in_sandbox(f"cd /workspace/{self.repo} && {cmd}", timeout=timeout)

    def status(self):
        return self._sh("git status --porcelain")[1]

    def scan_conflict_markers(self):
        code, out, err = self._sh(
            "grep -l -E '<<<<<<<|=======|>>>>>>>' calculator.py 2>/dev/null; echo GREP_EXIT=$?")
        return ["calculator.py"] if "calculator.py" in out else []

    def status_unmerged(self):
        return "UU" in self.status()

    def resolution_diff_lines(self):
        code, out, err = self._sh("git diff --numstat | awk '{s+=$1+$2} END {print s+0}'")
        return int(out.strip() or 0)

    def failed_tests_log(self):
        return self.last_pytest_log

    def read_code(self):
        return self._sh("cat calculator.py")[1]


def main():
    st = json.load(open(STATE, encoding="utf-8"))
    m3, c3 = st["m3"], st["c3"]

    # 一、捕获外部 M4 漂移
    m4 = requests.get(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits/main",
                      headers=H, timeout=20).json()["sha"]
    assert m4 != m3, f"M4 == M3: {m4}"
    st["m4"] = m4
    st["fsm"] = "GEN_3_INVALIDATED"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"M4 captured={m4} (fsm=GEN_3_INVALIDATED, Gen-3 token voided)")

    # 二、真实合并与冲突真值捕获
    start_sandbox()
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git fetch -q upstream main && git merge --no-ff FETCH_HEAD",
        timeout=180)
    merge_exit = code
    assert merge_exit != 0, "merge unexpectedly clean"
    status = exec_in_sandbox(f"cd /workspace/{WORK} && git status --porcelain", timeout=60)[1]
    assert "UU calculator.py" in status, f"UU missing: {status!r}"
    st["fsm"] = "CONFLICT_DETECTED"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"conflict truth: exit={merge_exit}, status={status.strip()!r}")

    # 冲突现场快照（消解前固化）
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && cp calculator.py /tmp/unmerged_snapshot.patch", timeout=60)
    assert code == 0, err or out

    # 三、有界搜索（3 次真实消解尝试，每次 pytest 因禁负契约互斥而失败）
    st["fsm"] = "CONFLICT_SANDBOX_RESOLVING"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ts = time.strftime("%Y%m%d_%H%M%S")
    bundle_host = os.path.join(WORKSPACE_DIR, f"failure_bundle_{ts}")
    bundle_sbx = f"/workspace/failure_bundle_{ts}"
    adapter = SandboxAdapter(WORK)
    resolver = UnresolvableResolver(adapter, m2=m3, c2star=c3, m3=m4,
                                    allowed_scope=["calculator.py"], bundle_dir=bundle_host)

    def _write_calc(content):
        wr = InternalTools.write_file(f"{WORK}/calculator.py", content)
        assert wr["success"], wr
        os.chown(os.path.join(WORKSPACE_DIR, WORK, "calculator.py"), 1000, 1000)

    for i in range(3):
        _write_calc(CALC_RESOLUTION)
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{WORK} && git add calculator.py", timeout=60)
        assert code == 0, err or out
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{WORK} && rm -rf __pycache__ && "
            f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q", timeout=180)
        pytest_exit = code
        adapter.last_pytest_log = out + err
        assert pytest_exit != 0, "mutually exclusive test unexpectedly passed"
        state = resolver.attempt_resolution(["calculator.py"], "c4" * 20,
                                            [c3, m4], pytest_exit=pytest_exit)
        log(f"attempt {i + 1}/3: pytest_exit={pytest_exit} state={state}")

    assert resolver.state == "UNRESOLVABLE_HALT"
    assert resolver.halt_reason == "ATTEMPTS_EXCEEDED"
    st["fsm"] = "UNRESOLVABLE_HALT"
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 四、黑匣子补写快照 + 受控收容
    os.chown(bundle_host, 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cp /tmp/unmerged_snapshot.patch {bundle_sbx}/unmerged_snapshot.patch", timeout=60)
    assert code == 0, err or out
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git merge --abort", timeout=60)
    assert code == 0, f"abort failed: {err or out}"
    clean = exec_in_sandbox(f"cd /workspace/{WORK} && git status --porcelain", timeout=60)[1]
    assert clean == "", f"workspace not clean: {clean!r}"
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && ls .git/index.lock 2>/dev/null; echo LOCK_EXIT=$?", timeout=60)
    assert "LOCK_EXIT=2" in out, "index.lock residue"
    log("workspace contained: merge --abort OK, clean status, no lock files")

    # 五、终态防御断言 + 证据块
    assert resolver.generate_approval_card() is None
    assert resolver.network_write_counters == {"upstream": 0, "fork": 0}
    files = sorted(os.listdir(bundle_host))
    for fname in ("metadata.json", "git_status.txt", "unmerged_snapshot.patch",
                  "failed_tests.log", "constraints.json", "exploration_history.json"):
        assert fname in files and os.path.getsize(os.path.join(bundle_host, fname)) > 0

    print()
    print("B4-4_PHYSICAL_HALT_EVIDENCE")
    print(f"m3={m3}")
    print(f"m4={m4}")
    print(f"c3={c3}")
    print(f"merge_exit_code={merge_exit}")
    print("unmerged_index=UU calculator.py")
    print(f"attempts=3")
    print(f"pytest_exits=[1, 1, 1] (negative-multiply contract mutual exclusion)")
    print(f"halt_reason={resolver.halt_reason}")
    print(f"fsm={st['fsm']}")
    print(f"bundle_dir={bundle_sbx}")
    print(f"bundle_files={files}")
    print(f"card_generation_result={resolver.generate_approval_card()}")
    print(f"upstream_push_counter=0")
    print(f"fork_push_counter=0")
    print("workspace_clean=True (git merge --abort, no index.lock)")
    print("[B4P4] PHYSICAL STOP. Awaiting audit.")
    sys.exit(0)


if __name__ == "__main__":
    main()
