# tools/preflight_checker.py
"""
Gate 4.5-A1 环境预检器：在唤醒 LLM 之前执行，环境坏掉不得白烧 Token。

预检序列（全部在沙箱内以 sandbox_user 执行）：
1. 仓库存在性与 git 健全检查 -> 失败归因 DEPENDENCY_ERROR
2. 依赖安装校验（requirements.txt 存在时执行）-> 失败归因 DEPENDENCY_ERROR
3. 基线测试运行（存在 test_baseline.py 则跑烟测，否则全量 pytest）
   -> 失败归因 BASELINE_TEST_FAILED
"""
import os
import re
from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR

_REPO_DIR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SMOKE_FILE = "test_baseline.py"


class PreflightChecker:
    @staticmethod
    def run(repo_dir: str) -> dict:
        """返回 {"preflight_passed": bool, "reason": None|"BASELINE_TEST_FAILED"|"DEPENDENCY_ERROR", "output": str}"""
        if not _REPO_DIR_RE.match(repo_dir or ""):
            return {"preflight_passed": False, "reason": "DEPENDENCY_ERROR",
                    "output": f"Invalid repo_dir: {repo_dir!r}"}

        host_repo = os.path.join(WORKSPACE_DIR, repo_dir)
        if not os.path.isdir(host_repo):
            return {"preflight_passed": False, "reason": "DEPENDENCY_ERROR",
                    "output": f"Repo dir missing: {repo_dir}"}

        # 1. git 健全检查
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && git rev-parse --is-inside-work-tree")
        if code != 0:
            return {"preflight_passed": False, "reason": "DEPENDENCY_ERROR",
                    "output": f"Git check failed: {(err or out).strip()[:500]}"}

        # 2. 依赖安装校验（无 requirements.txt 则跳过）
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && "
            f"[ ! -f requirements.txt ] || pip install -q -r requirements.txt --timeout 8 --retries 1")
        if code != 0:
            return {"preflight_passed": False, "reason": "DEPENDENCY_ERROR",
                    "output": f"Dependency install failed: {(err or out).strip()[:500]}"}

        # 3. 基线测试运行
        baseline_cmd = (f"python3 -m pytest -q {SMOKE_FILE}"
                        if os.path.exists(os.path.join(host_repo, SMOKE_FILE))
                        else "python3 -m pytest -q")
        code, out, err = exec_in_sandbox(f"cd /workspace/{repo_dir} && {baseline_cmd}")
        if code != 0:
            return {"preflight_passed": False, "reason": "BASELINE_TEST_FAILED",
                    "output": f"Baseline pytest failed (exit {code}): {(err or out).strip()[:500]}"}

        return {"preflight_passed": True, "reason": None, "output": (out or err).strip()[:500]}
