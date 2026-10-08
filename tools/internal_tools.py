# tools/internal_tools.py
import os
from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR

class InternalTools:
    @staticmethod
    def _sanitize_path(relative_path: str) -> str:
        # 严格防御路径遍历：必须解析在 workspace 内部
        clean_path = os.path.normpath(relative_path).lstrip("/")
        full_path = os.path.abspath(os.path.join(WORKSPACE_DIR, clean_path))
        if not full_path.startswith(WORKSPACE_DIR):
            raise PermissionError("Path traversal attempt detected.")
        return clean_path

    @staticmethod
    def shell(cmd: str) -> dict:
        exit_code, stdout, stderr = exec_in_sandbox(cmd)
        success = (exit_code == 0)
        if success:
            # 成功时合并 stdout+stderr：内核级错误（如 fork EAGAIN）打在 stderr，
            # 必须对控制器可见，否则资源耗尽类信号会丢失
            output = f"{stdout}\n{stderr}".strip()
        else:
            output = f"Exit code {exit_code}\nStderr: {stderr}\nStdout: {stdout}"
        return {
            "success": success,
            "output": output.strip(),
            "hard_cost": 0.0005  # 模拟执行算力/时延折算微成本
        }

    @classmethod
    def write_file(cls, path: str, content: str) -> dict:
        try:
            rel_path = cls._sanitize_path(path)
            full_path = os.path.join(WORKSPACE_DIR, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {
                "success": True,
                "output": f"File written successfully: {rel_path}",
                "hard_cost": 0.0005
            }
        except Exception as e:
            return {
                "success": False,
                "output": f"Write failed: {str(e)}",
                "hard_cost": 0.0005
            }

    @classmethod
    def read_file(cls, path: str) -> dict:
        try:
            rel_path = cls._sanitize_path(path)
            full_path = os.path.join(WORKSPACE_DIR, rel_path)
            if not os.path.exists(full_path):
                return {"success": False, "output": f"File not found: {rel_path}", "hard_cost": 0.0005}
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(4000)  # 读取上限 4000 字符
            return {
                "success": True,
                "output": content,
                "hard_cost": 0.0005
            }
        except Exception as e:
            return {
                "success": False,
                "output": f"Read failed: {str(e)}",
                "hard_cost": 0.0005
            }
