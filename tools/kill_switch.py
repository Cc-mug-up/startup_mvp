# tools/kill_switch.py
"""
Gate 4.5-B2 宿主机物理断路器（Kill Switch）。

激活方式（任一即可）：
- 环境变量 AIE_KILL_SWITCH=1
- 磁盘锁文件 /tmp/aie_kill_switch.lock

激活后，底层一切远程写操作（push_branch / create_pr）直接抛出
KillSwitchActivatedError，立即物理阻断，不产生任何网络写调用。
"""
import os

KILL_SWITCH_ENV = "AIE_KILL_SWITCH"
KILL_SWITCH_LOCK = "/tmp/aie_kill_switch.lock"


class KillSwitchActivatedError(RuntimeError):
    """物理断路器激活：一切远程写操作被立即阻断。"""


class GlobalKillSwitch:
    @staticmethod
    def is_blocked() -> bool:
        if os.environ.get(KILL_SWITCH_ENV) == "1":
            return True
        return os.path.exists(KILL_SWITCH_LOCK)

    @staticmethod
    def engage() -> None:
        with open(KILL_SWITCH_LOCK, "w") as f:
            f.write("engaged")

    @staticmethod
    def disengage() -> None:
        if os.path.exists(KILL_SWITCH_LOCK):
            os.remove(KILL_SWITCH_LOCK)
