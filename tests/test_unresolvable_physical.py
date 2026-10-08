# tests/test_unresolvable_physical.py
# Gate 4.5-B4-4 Phase 2 本地半物理验证（沙箱真实 Git 仓库 / 零 GitHub API / 零网络写）
import json
import os
import time

import pytest

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.internal_tools import InternalTools
from tools.unresolvable_guard import UnresolvableResolver

REPO = "b4p4_repo"
BUNDLE_TS = time.strftime("%Y%m%d_%H%M%S")
BUNDLE_HOST = os.path.join(WORKSPACE_DIR, f"failure_bundle_{BUNDLE_TS}")  # 宿主机路径（resolver 落盘）
BUNDLE_SBX = f"/workspace/failure_bundle_{BUNDLE_TS}"                     # 沙箱挂载路径（cp 用）

CALC_BASE = "def add(a, b):\n    return a + b\n"
TEST_BASE = "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

# branch_agent：无守卫 multiply（与上游守卫语义在同一区域冲突）
CALC_AGENT = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"
TEST_AGENT = "from calculator import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"

# branch_upstream：数值守卫 multiply + 与守卫互斥的测试约束（要求字符串拼接）
CALC_UPSTREAM = (
    "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n"
    "    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):\n"
    "        raise TypeError(\"multiply requires numeric arguments\")\n"
    "    return a * b\n")
TEST_UPSTREAM = (
    "from calculator import multiply\n\n\ndef test_upstream_requires_string_concat():\n"
    "    assert multiply(\"2\", 3) == \"222\"\n")

# 消解尝试（每次相同）：通过双向契约、但必然失败于上游互斥测试
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


class PhysicalSandbox:
    """UnresolvableResolver 的沙箱适配器：真实 git/pytest 现场。"""

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


@pytest.fixture(scope="module")
def physical_repo():
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    import shutil
    shutil.rmtree(os.path.join(WORKSPACE_DIR, REPO), ignore_errors=True)  # 宿主机侧清场
    # 沙箱侧以 uid 1000 建目录（host root 建的目录 uid 1000 无法清空/写入）
    code, out, err = exec_in_sandbox(
        f"cd /workspace && rm -rf {REPO} && mkdir {REPO} && cd {REPO} "
        f"&& git init -q && git config user.email a@b.c && git config user.name s",
        timeout=120)
    assert code == 0, f"repo init failed: {err or out}"
    for f, c in (("calculator.py", CALC_BASE), ("test_base.py", TEST_BASE)):
        wr = InternalTools.write_file(f"{REPO}/{f}", c)
        assert wr["success"]
    for f in ("calculator.py", "test_base.py"):
        os.chown(os.path.join(WORKSPACE_DIR, REPO, f), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{REPO} && git add -A && git commit -qm base && git rev-parse HEAD",
        timeout=120)
    assert code == 0, err or out
    base_sha = out.strip().splitlines()[-1]
    yield {"repo": REPO, "base_sha": base_sha}
    stop_sandbox()


def test_physical_unresolvable_halt_flow(physical_repo):
    repo = physical_repo["repo"]
    base_sha = physical_repo["base_sha"]

    def _write(fname, content):
        wr = InternalTools.write_file(f"{repo}/{fname}", content)
        assert wr["success"], wr
        os.chown(os.path.join(WORKSPACE_DIR, repo, fname), 1000, 1000)

    def _sh(cmd, timeout=120):
        return exec_in_sandbox(f"cd /workspace/{repo} && {cmd}", timeout=timeout)

    # 一、构造真实本地冲突：branch_agent vs branch_upstream
    _write("calculator.py", CALC_AGENT)
    _write("test_agent.py", TEST_AGENT)
    code, out, err = _sh("git checkout -q -b branch_agent && git add -A && "
                         "git commit -qm agent && git rev-parse HEAD")
    assert code == 0, err or out
    agent_sha = out.strip().splitlines()[-1]

    _sh("git checkout -q -b branch_upstream master 2>/dev/null || git checkout -q -b branch_upstream main")
    # 统一回退到 base 构造上游分支（若 master/main 命名差异则退回 base_sha）
    _sh(f"git checkout -q -b branch_upstream {base_sha} 2>/dev/null || "
        f"git checkout -q -B branch_upstream {base_sha}")
    _write("calculator.py", CALC_UPSTREAM)
    _write("test_upstream.py", TEST_UPSTREAM)
    code, out, err = _sh("git add -A && git commit -qm upstream && git rev-parse HEAD")
    assert code == 0, err or out
    upstream_sha = out.strip().splitlines()[-1]

    # 回到 branch_agent 执行真实合并
    _sh("git checkout -q branch_agent")
    code, out, err = _sh("git merge branch_upstream")
    merge_exit = code
    assert merge_exit != 0, "merge unexpectedly clean"
    status = _sh("git status --porcelain")[1]
    assert "UU calculator.py" in status
    print(f"[B4P4] real merge conflict: exit={merge_exit}, status={status.strip()!r}")

    # 未解决冲突快照（冲突标记现场，消解前固化）
    code, out, err = _sh("cp calculator.py /tmp/unmerged_snapshot.patch")
    assert code == 0, err or out

    # 二、有界搜索：3 次消解尝试，每次真实 pytest 均失败
    adapter = PhysicalSandbox(repo)
    resolver = UnresolvableResolver(adapter, m2=base_sha, c2star=agent_sha, m3=upstream_sha,
                                    allowed_scope=["calculator.py"], bundle_dir=BUNDLE_HOST)
    for i in range(3):
        _write("calculator.py", CALC_RESOLUTION)
        # 真实消解动作：git add 清除未合并索引（否则清洁性检查先于尝试计数熔断）
        code, out, err = _sh("git add calculator.py")
        assert code == 0, err or out
        code, out, err = _sh("rm -rf __pycache__ && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q")
        pytest_exit = code
        adapter.last_pytest_log = out + err
        assert pytest_exit != 0, "mutually exclusive test unexpectedly passed"
        state = resolver.attempt_resolution(["calculator.py"], "c3" * 20,
                                            [agent_sha, upstream_sha], pytest_exit=pytest_exit)
        print(f"[B4P4] attempt {i + 1}: pytest_exit={pytest_exit} state={state}")

    assert resolver.state == "UNRESOLVABLE_HALT"
    assert resolver.halt_reason == "ATTEMPTS_EXCEEDED"

    # 三、黑匣子固化：unmerged_snapshot.patch 补写 + 受控 git merge --abort
    os.chown(BUNDLE_HOST, 1000, 1000)  # 宿主建的 bundle 目录交给沙箱用户写入
    code, out, err = _sh(f"cp /tmp/unmerged_snapshot.patch {BUNDLE_SBX}/unmerged_snapshot.patch")
    assert code == 0, err or out
    code, out, err = _sh("git merge --abort")
    assert code == 0, f"merge --abort failed: {err or out}"
    clean_status = _sh("git status --porcelain")[1]
    assert clean_status == "", f"workspace not clean after abort: {clean_status!r}"
    code, out, err = _sh("ls .git/index.lock 2>/dev/null; echo LOCK_EXIT=$?")
    assert "LOCK_EXIT=2" in out, "index.lock residue"
    print(f"[B4P4] git merge --abort OK; workspace clean: {clean_status!r}; no index.lock")

    # 四、终态防御断言
    assert resolver.generate_approval_card() is None
    assert resolver.card_allowed is False
    assert resolver.network_write_counters == {"upstream": 0, "fork": 0}

    # 黑匣子文件清单与非空校验
    host_bundle = BUNDLE_HOST
    files = sorted(os.listdir(host_bundle))
    print(f"[B4P4] failure_bundle files: {files}")
    for fname in ("metadata.json", "git_status.txt", "unmerged_snapshot.patch",
                  "failed_tests.log", "constraints.json", "exploration_history.json"):
        assert fname in files, f"missing {fname}"
        assert os.path.getsize(os.path.join(host_bundle, fname)) > 0, f"empty {fname}"
    meta = json.load(open(os.path.join(host_bundle, "metadata.json"), encoding="utf-8"))
    assert meta["halt_reason"] == "ATTEMPTS_EXCEEDED"
    assert meta["state"] == "UNRESOLVABLE_HALT"
    history = json.load(open(os.path.join(host_bundle, "exploration_history.json"),
                             encoding="utf-8"))
    assert len(history) == 3 and all(h["verdict"] == "PYTEST_FAIL" for h in history)
    print("[B4P4] terminal defenses verified: card=None, counters=0, bundle non-empty")
