# tools/sandbox_delivery.py
"""
Gate 4.2-A 本地确定性交付流水线（Mock 仓库 / 零外网依赖）。

物理隔离：所有 git / pytest 操作一律通过 docker exec 在 startup_sandbox_img 容器内
以非特权用户 sandbox_user (UID 1000) 执行；宿主机仅负责将 patch 写入挂载的
/workspace 目录并发送执行指令。

内置 4 项硬拦截：
1. 变更范围硬拦截（Diff Line Cap，>300 行拒绝）
2. 测试结果客观提取（仅 exit_code == 0 才允许 commit）
3. 工件抽取（git rev-parse HEAD 提取真实 commit_sha）
4. 物理隔离（见上）
"""
import os
import re
from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR
from tools.internal_tools import InternalTools

MAX_DIFF_LINES = 300
_REPO_DIR_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _parse_numstat(text: str) -> int:
    """解析 git diff --numstat：累加每文件的增行数 + 删行数。"""
    total = 0
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for p in parts[:2]:
                p = p.strip()
                if p.isdigit():
                    total += int(p)
    return total


def _fail(opportunity_id: str, error: str, changed_lines: int = 0, raw_log: str = "") -> dict:
    return {
        "success": False,
        "opportunity_id": opportunity_id,
        "error": error,
        "tests_passed": False,
        "commit_sha": None,
        "changed_lines": changed_lines,
        "raw_log": raw_log,
        "hard_cost": 0.0,
        "artifact": None
    }


class SandboxDeliveryPipeline:
    @staticmethod
    def execute_task(opportunity_id: str, repo_dir: str, patch_content: str, test_cmd: str = "pytest") -> dict:
        # 0. repo_dir 形态硬校验（防命令注入）
        if not _REPO_DIR_RE.match(repo_dir or ""):
            return _fail(opportunity_id, f"Invalid repo_dir: {repo_dir!r}")

        # 1. 宿主机仅写 patch 到挂载工作区（路径穿越由 InternalTools 统一拦截）
        patch_rel = f"{repo_dir}/.agent_patch.diff"
        wr = InternalTools.write_file(patch_rel, patch_content)
        if not wr.get("success"):
            return _fail(opportunity_id, f"Patch write failed: {wr.get('output')}")
        try:
            os.chown(os.path.join(WORKSPACE_DIR, patch_rel), 1000, 1000)
        except OSError:
            pass

        # 2. 容器内应用补丁（sandbox_user）
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && git apply .agent_patch.diff"
        )
        if code != 0:
            return _fail(opportunity_id, f"Patch apply failed: {(err or out).strip()[:500]}")

        # 3. 变更范围硬拦截：>300 行直接中断
        # 注意：git diff --numstat 不统计未跟踪的新文件，改对补丁文件本身计数
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && git apply --numstat .agent_patch.diff"
        )
        total_lines = _parse_numstat(out) if code == 0 else 0
        if total_lines > MAX_DIFF_LINES:
            return {
                "success": False,
                "error": f"Diff exceeds max line cap ({MAX_DIFF_LINES} lines)",
                "artifact": None
            }

        # 4. 真实测试结果客观提取：只信 exit_code 与终端输出
        code, out, err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && {test_cmd} -q"
        )
        raw_log = "\n".join(x for x in (out, err) if x).strip()
        if code != 0:
            return _fail(opportunity_id, f"Tests failed (exit_code={code})", total_lines, raw_log)

        # 5. 测试通过后才允许 commit，并提取真实 commit_sha
        c_code, c_out, c_err = exec_in_sandbox(
            f"cd /workspace/{repo_dir} && git add -A "
            f"&& git commit -qm 'Agent fix for {opportunity_id}' && git rev-parse HEAD"
        )
        if c_code != 0:
            return _fail(opportunity_id, f"Commit failed: {(c_err or c_out).strip()[:500]}", total_lines, raw_log)
        commit_sha = c_out.strip().splitlines()[-1] if c_out.strip() else ""

        return {
            "success": True,
            "opportunity_id": opportunity_id,
            "tests_passed": True,
            "commit_sha": commit_sha,
            "changed_lines": total_lines,
            "raw_log": raw_log,
            "hard_cost": 0.0
        }
