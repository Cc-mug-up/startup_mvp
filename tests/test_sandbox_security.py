# tests/test_sandbox_security.py
import pytest
import os
from sandbox_manager import build_sandbox_image, start_sandbox, stop_sandbox
from tools.internal_tools import InternalTools

@pytest.fixture(scope="module", autouse=True)
def setup_sandbox():
    build_sandbox_image()
    start_sandbox()
    yield
    stop_sandbox()

# 1. 验证无法越界读取宿主机 state.json (路径遍历拦截)
def test_path_traversal_prevention():
    res = InternalTools.read_file("../../state.json")
    assert not res["success"]
    assert "Path traversal attempt detected" in res["output"]

# 2. 验证容器内无法通过 Shell 访问宿主机文件
def test_shell_cannot_escape_workspace():
    # 容器根目录下不包含宿主机的 controller.py 或 state.json
    res = InternalTools.shell("cat /state.json || cat ../state.json")
    assert not res["success"]
    assert "No such file or directory" in res["output"]

# 3. 验证宿主机的敏感环境变量未泄漏至沙箱
def test_env_credentials_not_leaked():
    res = InternalTools.shell("env")
    assert res["success"]
    # 确认常见的敏感 Key 均不在环境变量中
    for secret in ["STRIPE", "OPENAI", "GEMINI", "GITHUB_TOKEN", "RESEND"]:
        assert secret not in res["output"].upper()

# 4. 验证超时保护 (防死循环脚本卡死控制器)
def test_shell_timeout_kill():
    res = InternalTools.shell("sleep 35")
    assert not res["success"]
    assert "timed out" in res["output"]

# 5. 验证进程数限制 (防 Fork 炸弹拖垮宿主机)
def test_fork_bomb_mitigation():
    # 执行轻量级耗尽进程测试
    res = InternalTools.shell("for i in $(seq 1 100); do sleep 10 & done")
    # 容器配置了 --pids-limit=64，后续进程必然被内核 EAGAIN 拒绝
    assert "Resource temporarily unavailable" in res["output"] or not res["success"]
