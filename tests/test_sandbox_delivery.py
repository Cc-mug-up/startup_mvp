# tests/test_sandbox_delivery.py
# Gate 4.2-A 确定性单测：Mock 本地 git 仓库 / 零外网依赖
import os
import shutil
import pytest
from sandbox_manager import start_sandbox, exec_in_sandbox, WORKSPACE_DIR
from tools.sandbox_delivery import SandboxDeliveryPipeline

REPO = "mock_calculator"

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
TEST_CALC = "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

CORRECT_PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)


@pytest.fixture
def mock_repo():
    """每个测试前在 /workspace 内重建极简 git 仓库（含会失败的单测），属主 UID 1000。"""
    start_sandbox()
    host_repo = os.path.join(WORKSPACE_DIR, REPO)
    shutil.rmtree(host_repo, ignore_errors=True)
    os.makedirs(host_repo, exist_ok=True)
    with open(os.path.join(host_repo, "calculator.py"), "w", encoding="utf-8") as f:
        f.write(CALC_BUGGY)
    with open(os.path.join(host_repo, "test_calculator.py"), "w", encoding="utf-8") as f:
        f.write(TEST_CALC)
    os.chown(host_repo, 1000, 1000)  # 仓库根目录本身也必须归沙箱用户
    for root, dirs, files in os.walk(host_repo):
        for d in dirs:
            os.chown(os.path.join(root, d), 1000, 1000)
        for fn in files:
            os.chown(os.path.join(root, fn), 1000, 1000)

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{REPO} && git init -q && git config user.email 'agent@test.local' "
        f"&& git config user.name 'sandbox_agent' && git add -A && git commit -qm initial "
        f"&& git rev-parse HEAD"
    )
    assert code == 0, f"Repo init failed: stdout={out} stderr={err}"
    initial_sha = out.strip().splitlines()[-1]
    yield {"repo": REPO, "initial_sha": initial_sha}


# 1. 正确补丁：容器内 pytest 通过（exit 0），返回有效 commit_sha
def test_successful_patch_and_test_pass(mock_repo):
    res = SandboxDeliveryPipeline.execute_task("opp_gh_test_repo_1", mock_repo["repo"], CORRECT_PATCH)
    assert res["success"] is True
    assert res["tests_passed"] is True
    assert res["changed_lines"] == 2  # 1 增 1 删
    assert len(res["commit_sha"]) == 40
    assert res["hard_cost"] == 0.0


# 2. 错误补丁：容器内 pytest 失败，success=False，且不生成 commit
def test_failed_patch_blocks_commit(mock_repo):
    bad_patch = CORRECT_PATCH.replace("return a + b", "return a * b")
    res = SandboxDeliveryPipeline.execute_task("opp_gh_test_repo_2", mock_repo["repo"], bad_patch)
    assert res["success"] is False
    assert "Tests failed" in res["error"]

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{mock_repo['repo']} && git log --oneline | wc -l && git rev-parse HEAD"
    )
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    assert lines[0] == "1"  # 只有初始 commit
    assert lines[-1] == mock_repo["initial_sha"]  # HEAD 未移动


# 3. 超过 300 行垃圾变更：触发超限硬拦截
def test_diff_cap_rejection(mock_repo):
    body = "\n".join(f"+v{i} = {i}" for i in range(350))
    patch = f"--- /dev/null\n+++ b/junk.py\n@@ -0,0 +1,350 @@\n{body}\n"
    res = SandboxDeliveryPipeline.execute_task("opp_gh_test_repo_3", mock_repo["repo"], patch)
    assert res["success"] is False
    assert res["error"] == "Diff exceeds max line cap (300 lines)"
    assert res["artifact"] is None


# 4. git 操作以非 root（UID 1000）执行，全程无 Permission denied
def test_sandbox_user_permission(mock_repo):
    code, out, err = exec_in_sandbox("id -u")
    assert code == 0 and out.strip() == "1000"

    res = SandboxDeliveryPipeline.execute_task("opp_gh_test_repo_4", mock_repo["repo"], CORRECT_PATCH)
    assert res["success"] is True
    assert "Permission denied" not in res["raw_log"]
