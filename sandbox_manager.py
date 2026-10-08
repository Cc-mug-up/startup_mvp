# sandbox_manager.py
# 运行环境：外部 Linux 云服务器（原生 docker，非本地 Windows/WSL）
import subprocess
import os

CONTAINER_NAME = "startup_sandbox"
IMAGE_NAME = "startup_sandbox_img"
WORKSPACE_DIR = os.path.abspath("./workspace")

def ensure_workspace():
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

def build_sandbox_image():
    ensure_workspace()
    cmd = ["docker", "build", "-t", IMAGE_NAME, "-f", "Dockerfile.sandbox", "."]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"Docker build failed: {res.stderr[-2000:]}")

def start_sandbox():
    ensure_workspace()
    # 检查容器是否已经在运行
    check_cmd = ["docker", "ps", "-q", "-f", f"name={CONTAINER_NAME}"]
    running = subprocess.run(check_cmd, capture_output=True, text=True).stdout.strip()
    if running:
        return

    # 清理已停止的同名容器
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)

    # 启动受限常驻容器
    # 限制：512M内存，1个CPU核心，最多64进程，禁止获取额外权限，无宿主机环境变量透传
    cmd = [
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "--memory=512m",
        "--memory-swap=512m",  # 与 memory 相等 = 禁止使用宿主机 swap，硬内存顶
        "--cpus=1.0",
        "--pids-limit=64",
        "--security-opt=no-new-privileges:true",
        "-v", f"{WORKSPACE_DIR}:/workspace:rw",
        "-w", "/workspace",
        IMAGE_NAME,
        "tail", "-f", "/dev/null"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start sandbox container: {res.stderr[-2000:]}")

def stop_sandbox():
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)

def exec_in_sandbox(cmd_str: str, timeout: int = 30) -> tuple:
    """
    在沙箱内执行命令，实施严格超时与输出截断
    """
    start_sandbox()
    exec_cmd = [
        "docker", "exec",
        "-u", "sandbox_user",
        "-w", "/workspace",
        CONTAINER_NAME,
        "bash", "-c", cmd_str
    ]
    try:
        proc = subprocess.run(
            exec_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        # 防止输出过大撑爆上下文，硬截断至 4000 字符
        stdout = proc.stdout[:4000]
        stderr = proc.stderr[:4000]
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds."
    except Exception as e:
        return -1, "", str(e)
