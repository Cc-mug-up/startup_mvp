# tools/conflict_guard.py
"""
Gate 4.5-B4-3 语义冲突控制骨架（本地隔离，严禁任何真实 GitHub 网络写）。

状态机拓扑：
  AWAITING_REVIEW -> GEN_2_INVALIDATED -> DRIFT_ANALYZING -> CONFLICT_DETECTED
  -> CONFLICT_SANDBOX_RESOLVING -> CONFLICT_VERIFYING -> GEN_3_VERIFYING
  -> GEN_3_WAITING_APPROVAL
  任何校验失败硬熔断至 UNRESOLVABLE_HALT（吸收态：禁止审批卡、禁止远端写、零进程常驻）。

冲突真值判定（Index-First Ground Truth）唯一物理事实组合：
  1. git merge exit_code != 0；
  2. git status --porcelain 出现 unmerged 索引（UU/AA/AU/UA/DD/UD/DU）；
  3. 文件中存在冲突文本标记（<<<<<<< / ======= / >>>>>>>）。
"""
import re

UNMERGED_CODES = {"UU", "AA", "AU", "UA", "DD", "UD", "DU"}
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def agent_side_contract(code_text: str) -> dict:
    """
    Agent 侧语义契约：
    - divide 函数存在且除零抛 ValueError；
    - 负数乘法符号逻辑保持（multiply(-2, 3) == -6）。
    """
    ns = {}
    try:
        exec(code_text, ns)
    except Exception as e:
        return {"pass": False, "failures": [f"exec error: {e}"]}
    failures = []
    divide, multiply = ns.get("divide"), ns.get("multiply")
    if not callable(divide):
        failures.append("divide missing")
    else:
        try:
            divide(1, 0)
            failures.append("divide(1,0) did not raise ValueError")
        except ValueError:
            pass
        except Exception as e:
            failures.append(f"divide(1,0) raised wrong exception: {e}")
    if not callable(multiply):
        failures.append("multiply missing")
    else:
        try:
            if multiply(-2, 3) != -6:
                failures.append("negative multiply sign broken")
        except Exception as e:
            failures.append(f"negative multiply raised: {e}")
    return {"pass": not failures, "failures": failures}


def upstream_side_contract(code_text: str) -> dict:
    """
    Upstream 侧语义契约：
    - 数字类型放宽至 float/int（multiply(2, 3.0) == 6.0）；
    - 非数值抛 TypeError；
    - 结果绝对值 > 1_000_000 抛 ValueError。
    """
    ns = {}
    try:
        exec(code_text, ns)
    except Exception as e:
        return {"pass": False, "failures": [f"exec error: {e}"]}
    failures = []
    multiply = ns.get("multiply")
    if not callable(multiply):
        failures.append("multiply missing")
    else:
        try:
            if multiply(2, 3.0) != 6.0:
                failures.append("float/int relaxation broken")
        except Exception as e:
            failures.append(f"float/int relaxation broken: {e}")
        try:
            multiply("2", 3)
            failures.append("non-numeric did not raise TypeError")
        except TypeError as e:
            # TypeError 必须来自守卫语义（消息含 numeric），而非下游 abs() 等副产物
            if "numeric" not in str(e):
                failures.append(f"non-numeric TypeError not from guard: {e}")
        except Exception as e:
            failures.append(f"non-numeric wrong exception: {e}")
        try:
            multiply(2000, 2000)
            failures.append("huge result did not raise ValueError")
        except ValueError:
            pass
        except Exception as e:
            failures.append(f"huge result wrong exception: {e}")
    return {"pass": not failures, "failures": failures}


class ConflictGuard:
    """冲突守卫：FSM + 真值探针 + 三维差分 + 范围锁 + 双向语义契约。"""

    def __init__(self, sandbox, m2, c2star, m3, original_business_scope):
        self.sandbox = sandbox  # 注入桩：merge/status/diff/read_code
        self.m2, self.c2star, self.m3 = m2, c2star, m3
        self.business_scope = set(original_business_scope)
        self.state = "AWAITING_REVIEW"
        self.evidence = {}
        self.card_allowed = False
        self.remote_write_allowed = False  # 本阶段恒 False（本地隔离）
        self.c3 = None

    # ---------------- FSM ----------------
    def transition(self, to: str) -> bool:
        if self.state == "UNRESOLVABLE_HALT":
            return False  # 吸收态：任何流转被拒绝
        self.state = to
        return True

    def _halt(self, reason: str) -> str:
        self.state = "UNRESOLVABLE_HALT"
        self.evidence["halt_reason"] = reason
        return self.state

    # ---------------- 阶段 ----------------
    def run_invalidation(self) -> str:
        """外部漂移注入 -> GEN_2_INVALIDATED -> DRIFT_ANALYZING。"""
        self.transition("GEN_2_INVALIDATED")
        self.transition("DRIFT_ANALYZING")
        return self.state

    def probe_conflict(self) -> str:
        """Index-First Ground Truth：三重物理事实组合判定冲突。"""
        exit_code, output = self.sandbox.merge(self.m3)
        porcelain = self.sandbox.status()
        unmerged = [ln.split()[1] for ln in porcelain.splitlines()
                    if ln.split() and ln.split()[0] in UNMERGED_CODES]
        marker_files = self.sandbox.scan_conflict_markers()
        self.evidence.update({
            "merge_exit_code": exit_code,
            "merge_output": output,
            "unmerged_entries": unmerged,
            "marker_files": marker_files
        })
        # 冲突真值：三项物理事实缺一不可
        if not (exit_code != 0 and unmerged and marker_files):
            return self._halt("CONFLICT_TRUTH_PROBE_FAILED")
        self.transition("CONFLICT_DETECTED")
        return self.state

    def capture_unmerged_snapshot(self) -> str:
        """git merge 报错后未解决现场的物理快照引用。"""
        ref = self.sandbox.capture_snapshot()
        self.evidence["snapshot_ref"] = ref
        return ref

    def resolve(self, resolution_files, c3, c3_parents, pytest_exit: int) -> str:
        """消解路径：范围锁 -> 清洁性 -> DAG -> 三维差分 -> 双向契约 -> pytest。"""
        if self.state != "CONFLICT_DETECTED":
            return self._halt("ILLEGAL_RESOLVE_ORDER")
        self.transition("CONFLICT_SANDBOX_RESOLVING")

        # 1. Allowed Resolution Scope：变更文件 ⊆ UnmergedEntries ∪ OriginalBusinessScope
        unmerged = set(self.evidence["unmerged_entries"])
        changed = set(resolution_files)
        if not changed <= (unmerged | self.business_scope):
            return self._halt(f"SCOPE_VIOLATION: {sorted(changed - (unmerged | self.business_scope))}")

        # 2. 清洁性：无冲突标记、无未合并索引
        if self.sandbox.scan_conflict_markers() or self.sandbox.status_unmerged():
            return self._halt("UNCLEAN_RESOLUTION")

        # 3. DAG 物理断言：C3.parents == [C2*, M3]
        if list(c3_parents) != [self.c2star, self.m3]:
            return self._halt("DAG_VIOLATION")

        self.transition("CONFLICT_VERIFYING")
        self.c3 = c3

        # 4. 三重 Delta 解耦
        up_delta = self.sandbox.diff(self.m2, self.m3)                    # UpstreamDelta
        biz_delta = self.sandbox.diff(self.m2, self.c2star)               # OriginalBusinessDelta
        res_delta = self.sandbox.diff(self.evidence["snapshot_ref"], c3)  # ResolutionDelta
        self.evidence.update({
            "upstream_delta": up_delta,
            "original_business_delta": biz_delta,
            "resolution_delta": res_delta
        })
        self.transition("GEN_3_VERIFYING")

        # 5. 双向语义契约
        code = self.sandbox.read_code()
        agent_ok = agent_side_contract(code)
        upstream_ok = upstream_side_contract(code)
        self.evidence.update({"agent_contract": agent_ok, "upstream_contract": upstream_ok})
        if not (agent_ok["pass"] and upstream_ok["pass"]):
            return self._halt("SEMANTIC_CONTRACT_FAIL")

        # 6. 全量回归
        if pytest_exit != 0:
            return self._halt(f"PYTEST_FAIL exit={pytest_exit}")

        self.card_allowed = True
        self.transition("GEN_3_WAITING_APPROVAL")
        return self.state
