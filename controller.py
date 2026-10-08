# controller.py
import uuid
import json
import os
from datetime import datetime, timedelta
from typing import Tuple, Dict, Any, Optional
from config import CONFIG


def _parse_iso(ts: str) -> datetime:
    # Python 3.10 的 fromisoformat 不认 'Z' 后缀（3.11+ 才支持），统一归一化
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class BudgetExhaustedError(RuntimeError):
    """任务预算硬熔断异常：触发后一切模型调用与工具调用必须直接抛出。"""


# 危险指令文本策略拦截模式（越权 / 逃逸 / 提权）
POLICY_BLOCKED_PATTERNS = [
    "/etc/hosts", "/etc/shadow", "/etc/passwd", "sudo", "su -",
    "chmod 777", "rm -rf /", "shutdown", "reboot", "chroot",
    "/root/", ".ssh/id_rsa"
]


class Controller:
    def __init__(self, state_path: str = "state.json", log_path: str = "logs/audit.jsonl"):
        self.state_path = state_path
        self.log_path = log_path
        self.state = self.load_state()
        self._action_seq = 0  # 确定性动作序号（不使用 uuid，保证仿真可复现）

    def load_state(self) -> Dict[str, Any]:
        with open(self.state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_state(self) -> None:
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def log_audit(self, entry: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # =========================================================================
    # 1. 纯验证逻辑 (零副作用)
    # =========================================================================
    def validate_action(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        opp_id = action.get("opportunity_id")
        action_type = action.get("action_type")
        tool_name = action.get("tool_name")
        guards = self.state["runtime_guards"]
        opp_memory = self.state["opportunity_memory"]

        # 1.1 机会合法性校验
        if tool_name != "discover_opportunities":
            if opp_id not in opp_memory:
                return False, f"Opportunity '{opp_id}' does not exist. Must discover first."
            if opp_memory[opp_id].get("status") == "DEAD":
                return False, f"Opportunity '{opp_id}' is permanently DEAD."

        # 1.2 内部步数锁死
        if guards["consecutive_internal_steps"] >= guards["max_internal_steps"]:
            if action_type == "INTERNAL":
                return False, f"Internal limit ({guards['max_internal_steps']}) reached. Must execute EXTERNAL, ABANDON, or valid WAIT."

        # 1.25 WAITING 物理冻结：外部事件挂起期间仅允许 WAIT 轮询，
        # INTERNAL / EXTERNAL / ABANDON 一律拦截，禁止进一步消耗工具成本
        if guards.get("system_status") == "WAITING":
            if action_type != "WAIT":
                return False, "System is WAITING on pending external event. Only WAIT allowed."

        # 1.3 WAIT 必须有未决事件
        if action_type == "WAIT":
            if not guards.get("pending_external_event"):
                return False, "Cannot enter WAIT: No pending external event registered."

        # 1.4 ABANDON 只能针对已存在机会
        if action_type == "ABANDON":
            if tool_name == "discover_opportunities" or opp_id not in opp_memory:
                return False, f"Cannot ABANDON invalid opportunity '{opp_id}'."

        return True, "VALID"

    # =========================================================================
    # 2. 状态变更逻辑 (唯一状态写入点)
    # =========================================================================
    def apply_action(
        self,
        action_id: str,
        action: Dict[str, Any],
        is_valid: bool,
        validation_error: str,
        tool_result: Optional[Dict[str, Any]],
        llm_decision_cost: float
    ) -> None:
        guards = self.state["runtime_guards"]
        financials = self.state["financials"]
        opp_memory = self.state["opportunity_memory"]
        opp_id = action.get("opportunity_id")
        action_type = action.get("action_type")

        # 扣减真实发生的决策成本
        financials["current_cash"] -= llm_decision_cost
        financials["total_hard_cost"] += llm_decision_cost
        financials["cost_breakdown"]["llm_decision"] += llm_decision_cost

        if not is_valid:
            guards["consecutive_invalid_steps"] += 1
            if guards["consecutive_invalid_steps"] >= guards["max_invalid_steps"]:
                guards["system_status"] = "HALTED_INVALID_LOOP"
            # 工具已先行执行的硬成本照扣：拦截的是状态变更，不是已发生的费用
            tool_exec_cost = tool_result.get("hard_cost", 0.0) if tool_result else 0.0
            financials["current_cash"] -= tool_exec_cost
            financials["total_hard_cost"] += tool_exec_cost
            financials["cost_breakdown"]["tool_execution"] += tool_exec_cost
            self.save_state()
            return

        guards["consecutive_invalid_steps"] = 0

        # 扣减工具执行硬成本
        tool_exec_cost = tool_result.get("hard_cost", 0.0) if tool_result else 0.0
        financials["current_cash"] -= tool_exec_cost
        financials["total_hard_cost"] += tool_exec_cost
        financials["cost_breakdown"]["tool_execution"] += tool_exec_cost

        # 机会登记（仅能由工具产出）
        if tool_result and "discovered_opportunities" in tool_result:
            for new_opp in tool_result["discovered_opportunities"]:
                new_id = new_opp["opportunity_id"]
                if new_id not in opp_memory:
                    opp_memory[new_id] = {
                        "source": new_opp.get("source", "unknown"),
                        "observed_revenue": float(new_opp.get("observed_revenue", 0.0)),
                        "accumulated_cost": 0.0,
                        "internal_steps_spent": 0,
                        "external_interactions": 0,
                        "status": "ACTIVE"
                    }

        if opp_id in opp_memory:
            opp_memory[opp_id]["accumulated_cost"] += (llm_decision_cost + tool_exec_cost)

        # 状态机流转
        if action_type == "INTERNAL":
            guards["consecutive_internal_steps"] += 1
            if opp_id in opp_memory:
                opp_memory[opp_id]["internal_steps_spent"] += 1

        elif action_type == "EXTERNAL":
            guards["consecutive_internal_steps"] = 0
            if opp_id in opp_memory:
                opp_memory[opp_id]["external_interactions"] += 1

            if tool_result and tool_result.get("pending_event"):
                guards["pending_external_event"] = tool_result["pending_event"]
                guards["system_status"] = "WAITING"

        elif action_type == "ABANDON":
            guards["consecutive_internal_steps"] = 0
            if opp_id in opp_memory:
                opp_memory[opp_id]["status"] = "DEAD"

        elif action_type == "WAIT":
            guards["system_status"] = "WAITING"

        if financials["current_cash"] <= 0:
            guards["system_status"] = "BANKRUPT"

        self.save_state()

    # =========================================================================
    # 3. WAITING 轮询与幂等结算
    # =========================================================================
    def poll_waiting_event(self, tool_dispatcher) -> None:
        guards = self.state["runtime_guards"]
        pending = guards.get("pending_external_event")
        if not pending:
            guards["system_status"] = "ACTIVE"
            self.save_state()
            return

        poll_res = tool_dispatcher.check_pending_event(pending)

        if poll_res.get("status") == "RESOLVED":
            ext_ref = pending.get("external_reference")
            ev_type = pending.get("event_type")

            # 校验事件合法性与防重放 (Idempotency Guard)
            if ev_type in CONFIG["valid_event_types"] and ext_ref not in self.state["settled_event_refs"]:
                verified_rev = float(poll_res.get("verified_revenue", 0.0))
                if verified_rev > 0:
                    self.state["financials"]["total_revenue"] += verified_rev
                    self.state["financials"]["current_cash"] += verified_rev
                    self.state["transaction_ledger"].append({
                        "timestamp": guards["current_time"],
                        "opportunity_id": pending["opportunity_id"],
                        "amount": verified_rev,
                        "external_ref": ext_ref
                    })
                    self.state["settled_event_refs"].append(ext_ref)

            # 事件处理完毕，清空等待槽，重置为主状态
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"

        else:
            cur_time = _parse_iso(guards["current_time"])
            timeout_time = _parse_iso(pending["timeout_timestamp"])
            if cur_time >= timeout_time:
                # 超时强制拔除：区分于被拒绝的 DEAD，打标 TIMEOUT 并归因，成本沉没保留
                opp = self.state["opportunity_memory"].get(pending["opportunity_id"])
                if opp is not None:
                    opp["status"] = "TIMEOUT"
                    opp["reason"] = "TIMEOUT"
                self.state.setdefault("event_archive", []).append({
                    "resolved_at": guards["current_time"],
                    "event": pending,
                    "resolution": {"event_type": "TIMEOUT", "reason": "TIMEOUT"}
                })
                guards["pending_external_event"] = None
                guards["system_status"] = "ACTIVE"

        self.save_state()

    # =========================================================================
    # 3.5 事件解挂与状态归一化（交付/拒绝，绝不涉及收入结算）
    # =========================================================================
    def resolve_pending_event(self, parsed_event: dict) -> None:
        """
        根据现实回传事件（PR_MERGED / PR_REJECTED / PR_PENDING）解除 WAITING 冻结
        并归一化机会状态。交付不等于结算：本函数严禁触碰任何财务字段。

        幂等去重：无挂起事件、或事件的 external_reference 与挂起事件不匹配
        （过期/错配的重复回调）时，直接 no-op 忽略，绝不重复触发状态流转。
        """
        guards = self.state["runtime_guards"]
        pending = guards.get("pending_external_event")
        if not pending:
            return  # 重复/过期事件：无挂起事件可解挂，幂等忽略

        ev_ref = parsed_event.get("external_reference")
        if ev_ref is not None and ev_ref != pending.get("external_reference"):
            return  # 引用不匹配：错配事件，幂等忽略

        event_type = parsed_event.get("event_type")
        opp = self.state["opportunity_memory"].get(pending["opportunity_id"])

        if event_type == "PR_MERGED":
            # 完成交付：状态 DELIVERED，挂 PAYMENT_WAITING 下一步标志（等 Gate 4.3 结算）
            if opp is not None:
                opp["status"] = "DELIVERED"
                opp["payment_waiting"] = True
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": pending,
                "resolution": parsed_event
            })
        elif event_type == "PR_REJECTED":
            # 拒绝归因：机会死亡并记录结构化 reason，数据不删除，成本保持沉没
            if opp is not None:
                opp["status"] = "DEAD"
                opp["reason"] = "PR_REJECTED"
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": pending,
                "resolution": parsed_event
            })
        elif event_type == "CLARIFICATION_ANSWER":
            # 人类澄清事件：记录答复，解除冻结恢复执行
            if opp is not None:
                opp["clarification_answer"] = parsed_event.get("answer")
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": pending,
                "resolution": parsed_event
            })
        # PR_PENDING：维持 WAITING 冻结，等待下一轮询

        self.save_state()

    # =========================================================================
    # 3.7 澄清等待：进入 WAITING (reason: NEED_CLARIFICATION) 物理冻结
    # =========================================================================
    def enter_clarification_wait(self, opportunity_id: str, question: str) -> None:
        guards = self.state["runtime_guards"]
        guards["pending_external_event"] = {
            "event_type": "CLARIFICATION",
            "reason": "NEED_CLARIFICATION",
            "opportunity_id": opportunity_id,
            "question": question
        }
        guards["system_status"] = "WAITING"
        self.save_state()

    # =========================================================================
    # 3.8 经济结算：支付凭据 -> SETTLED 与全局唯一记账点
    # =========================================================================
    def settle_opportunity_payment(self, payment_event: dict) -> dict:
        """
        外部支付平台打款凭据结算（纯 Mock 阶段）。硬规则：
        1. 状态前置约束：opportunity 必须存在且处于 DELIVERED 且挂 payment_waiting，
           其余状态（ACTIVE / DEAD / TIMEOUT / SETTLED 等）一律硬拒绝。
        2. 金额严格校验：amount 必须为正数，且严格等于机会登记的 observed_revenue。
        3. 幂等与防重入：payment_ref 已出现过（settled_event_refs 或 ledger）则
           判定为幂等重复，返回成功但绝不二次累加金额。
        4. 唯一步骤执行记账：total_revenue / current_cash / SETTLED / settled_at / ledger。
        """
        financials = self.state["financials"]
        opp_memory = self.state["opportunity_memory"]

        if payment_event.get("event_type") != "PAYMENT_CONFIRMED":
            return {"success": False, "error": "Invalid payment event type"}

        opp_id = payment_event.get("opportunity_id")
        payment_ref = payment_event.get("payment_ref")
        amount_raw = payment_event.get("amount")
        ts = payment_event.get("timestamp")

        # 1. 幂等与防重入（优先级最高：已结算过的 payment_ref 直接 no-op，
        #    不受机会后续状态流转影响——SETTLED 后的重复回调必须仍被幂等识别）
        if payment_ref in self.state["settled_event_refs"]:
            return {"success": True, "duplicate": True,
                    "error": "Payment already settled (idempotent no-op)"}
        for entry in self.state["transaction_ledger"]:
            if entry.get("payment_ref") == payment_ref:
                return {"success": True, "duplicate": True,
                        "error": "Payment already settled (idempotent no-op)"}

        # 2. 状态前置约束
        opp = opp_memory.get(opp_id)
        if opp is None:
            return {"success": False, "error": f"Opportunity '{opp_id}' does not exist"}
        if opp.get("status") != "DELIVERED" or not opp.get("payment_waiting"):
            return {"success": False, "error": f"Opportunity '{opp_id}' is not in DELIVERED/payment_waiting state"}

        # 3. 金额严格校验
        if isinstance(amount_raw, bool) or not isinstance(amount_raw, (int, float)):
            return {"success": False, "error": "Invalid amount type"}
        amount = float(amount_raw)
        if amount <= 0:
            return {"success": False, "error": "Amount must be positive"}
        observed = float(opp.get("observed_revenue") or 0.0)
        if observed <= 0 or amount != observed:
            return {"success": False,
                    "error": f"Amount {amount} does not match observed_revenue {observed}"}

        # 4. 唯一步骤执行记账
        financials["total_revenue"] += amount
        financials["current_cash"] += amount
        opp["status"] = "SETTLED"
        opp["settled_at"] = ts
        self.state["transaction_ledger"].append({
            "type": "REVENUE",
            "amount": amount,
            "opportunity_id": opp_id,
            "payment_ref": payment_ref,
            "timestamp": ts
        })
        self.state["settled_event_refs"].append(payment_ref)
        self.state.setdefault("event_archive", []).append({
            "resolved_at": ts,
            "event": payment_event,
            "resolution": {"event_type": "PAYMENT_CONFIRMED"}
        })
        self.save_state()
        return {"success": True, "amount": amount, "opportunity_id": opp_id, "payment_ref": payment_ref}

    # =========================================================================
    # 3.9.5 人工审批门禁：PENDING_HUMAN_APPROVAL 冻结与审批决议
    # =========================================================================
    def enter_approval_wait(self, task_id: str, artifact: dict) -> None:
        """任何外部副作用发生前，进入 PENDING_HUMAN_APPROVAL 物理冻结。"""
        guards = self.state["runtime_guards"]
        guards["pending_external_event"] = {
            "event_type": "PENDING_HUMAN_APPROVAL",
            "opportunity_id": task_id,
            "artifact_hash": artifact.get("artifact_hash")
        }
        guards["system_status"] = "WAITING"
        self.save_state()

    def apply_human_decision(self, task_id: str, decision: str, approval_token: dict = None) -> dict:
        """人工决议：APPROVE（须携带与挂起工件哈希一致的令牌）或 REJECT。"""
        guards = self.state["runtime_guards"]
        pending = guards.get("pending_external_event")
        if not pending or pending.get("event_type") != "PENDING_HUMAN_APPROVAL":
            return {"success": False, "error": "No pending human approval"}
        if pending.get("opportunity_id") != task_id:
            return {"success": False, "error": "Approval pending for a different task"}
        opp = self.state["opportunity_memory"].get(task_id)

        if decision == "REJECT":
            if opp is not None:
                opp["status"] = "DEAD"
                opp["reason"] = "REJECTED_BY_HUMAN"
                opp["human_rejected_at"] = guards["current_time"]
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": pending,
                "resolution": {"event_type": "HUMAN_REJECTED"}
            })
            self.save_state()
            return {"success": True, "decision": "REJECT"}

        if decision == "APPROVE":
            if approval_token is None or \
                    approval_token.get("artifact_hash") != pending.get("artifact_hash"):
                return {"success": False, "error": "Token mismatch for pending artifact"}
            if opp is not None:
                opp["human_approval_at"] = approval_token.get("authorized_at")
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": pending,
                "resolution": {"event_type": "HUMAN_APPROVED",
                               "artifact_hash": approval_token.get("artifact_hash")}
            })
            self.save_state()
            return {"success": True, "decision": "APPROVE"}

        return {"success": False, "error": f"Unknown decision: {decision}"}

    # =========================================================================
    # 3.9 故障域隔离：硬预算截断、重试上限、策略拦截、对账与持久化恢复
    # =========================================================================
    def register_task(self, task_id: str, budget: float) -> None:
        self.state.setdefault("task_budgets", {})[task_id] = {
            "budget": float(budget), "spent": 0.0, "broken": False
        }
        self.save_state()

    def spend_task_budget(self, task_id: str, cost: float) -> None:
        """真实消耗入账；熔断后一切调用直接抛 BudgetExhaustedError（物理切断）。"""
        tb = self.state.get("task_budgets", {}).get(task_id)
        if tb is None:
            return
        if tb.get("broken"):
            raise BudgetExhaustedError(f"Task '{task_id}' circuit broken (budget exhausted)")
        if cost > 0:
            tb["spent"] += float(cost)
            if tb["spent"] >= tb["budget"] - 1e-12:
                tb["broken"] = True
                self.mark_task_status(task_id, "DEAD", "BUDGET_EXHAUSTED")
        self.save_state()

    def mark_task_status(self, task_id: str, status: str, reason: str = None) -> None:
        opp = self.state["opportunity_memory"].get(task_id)
        if opp is not None:
            opp["status"] = status
            if reason:
                opp["reason"] = reason
            self.save_state()

    def call_model_guarded(self, task_id: str, prompt: str, model_adapter, max_retries: int = 1) -> dict:
        """
        模型调用守卫：预算熔断检查 + 重试硬上限。
        返回 {"success", "reason", "attempts", "result", "cost"}；
        熔断触发时直接抛 BudgetExhaustedError。
        """
        attempts = 0
        while True:
            tb = self.state.get("task_budgets", {}).get(task_id)
            if tb and tb.get("broken"):
                raise BudgetExhaustedError(f"Task '{task_id}' circuit broken (budget exhausted)")
            try:
                result = model_adapter.generate(prompt)
            except (TimeoutError, ConnectionError):
                attempts += 1
                if attempts > max_retries:
                    return {"success": False, "reason": "MODEL_TIMEOUT", "attempts": attempts,
                            "result": None, "cost": 0.0}
                continue
            attempts += 1
            cost = float(result.get("cost") or 0.0)
            if cost > 0:
                self.spend_task_budget(task_id, cost)  # 超限在此抛 BudgetExhaustedError
            return {"success": True, "reason": None, "attempts": attempts, "result": result, "cost": cost}

    @staticmethod
    def check_policy_violation(text) -> str:
        """危险指令文本策略拦截。返回 None（无违例）或 'POLICY'（命中拦截模式）。"""
        if not isinstance(text, str):
            return None
        t = text.lower()
        for pattern in POLICY_BLOCKED_PATTERNS:
            if pattern.lower() in t:
                return "POLICY"
        return None

    def reconcile_with_external_reality(self, opportunity_id: str, external_facts: dict) -> dict:
        """
        外内状态对账：以外部真实事实纠偏本地状态（严禁模型端自主修补）。
        - 外部 PR 已被关闭 -> 本地纠偏 DEAD (reason: PR_REJECTED)
        - 外部 PR 已意外存在 -> 本地纠偏 WAITING 并绑定外部引用
        """
        guards = self.state["runtime_guards"]
        opp = self.state["opportunity_memory"].get(opportunity_id)
        if opp is None:
            return {"success": False, "error": f"Opportunity '{opportunity_id}' does not exist"}

        if external_facts.get("pr_state") == "CLOSED":
            opp["status"] = "DEAD"
            opp["reason"] = "PR_REJECTED"
            prev = guards["pending_external_event"]
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            self.state.setdefault("event_archive", []).append({
                "resolved_at": guards["current_time"],
                "event": prev,
                "resolution": {"event_type": "EXTERNAL_RECONCILE_CLOSED"}
            })
            self.save_state()
            return {"success": True, "correction": "DEAD/PR_REJECTED"}

        if external_facts.get("pr_exists"):
            guards["pending_external_event"] = {
                "event_type": "PR_REVIEW",
                "opportunity_id": opportunity_id,
                "external_reference": external_facts.get("pr_reference"),
                "timeout_timestamp": external_facts.get("timeout_timestamp",
                                                       "2026-10-05T00:00:00+00:00")
            }
            guards["system_status"] = "WAITING"
            self.save_state()
            return {"success": True, "correction": "WAITING/BOUND"}

        return {"success": True, "correction": "NONE"}

    def poll_external_pr_status(self, task_id: str, owner_repo: str, pr_number: int,
                                transport=None, now_iso: str = None) -> dict:
        """
        外部 PR 状态双向对账（只读 GET /repos/{owner}/{repo}/pulls/{number}）：
        - open -> 内部保持 WAITING(PR_OPEN)
        - closed 且未合并 -> 纠偏 DEAD/PR_REJECTED
        - closed 且已合并 -> 流转 DELIVERED/PR_MERGED（交付不等于结算，零收入）
        """
        if transport is None:
            import requests
            transport = requests
        guards = self.state["runtime_guards"]
        try:
            resp = transport.get(
                f"https://api.github.com/repos/{owner_repo}/pulls/{pr_number}",
                timeout=15)
        except Exception as e:
            return {"success": False, "error": f"PR status poll failed: {e}"}
        if resp.status_code != 200:
            return {"success": False, "error": f"HTTP {resp.status_code}"}

        pr = resp.json()
        remote_state = pr.get("state")
        merged = bool(pr.get("merged"))
        opp = self.state["opportunity_memory"].get(task_id)
        prev = guards["pending_external_event"]

        if remote_state == "closed" and not merged:
            if opp is not None:
                opp["status"] = "DEAD"
                opp["reason"] = "PR_REJECTED"
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            correction = "DEAD/PR_REJECTED"
        elif remote_state == "closed" and merged:
            if opp is not None:
                opp["status"] = "DELIVERED"
                opp["payment_waiting"] = True
            guards["pending_external_event"] = None
            guards["system_status"] = "ACTIVE"
            correction = "DELIVERED/PR_MERGED"
        else:
            correction = "WAITING/PR_OPEN"  # 保持挂起

        if correction != "WAITING/PR_OPEN":
            self.state.setdefault("event_archive", []).append({
                "resolved_at": now_iso or guards["current_time"],
                "event": prev,
                "resolution": {"event_type": "EXTERNAL_PR_RECONCILE",
                               "remote_state": remote_state, "merged": merged}
            })
        self.save_state()
        return {"success": True, "correction": correction,
                "remote_state": remote_state, "merged": merged}

    def enter_review_wait(self, task_id: str, review_refs: list, parent_commit: str) -> None:
        """B3-1：评审反馈冻结。外部 CHANGES_REQUESTED -> WAITING (PR_REVIEW) 物理冻结。"""
        guards = self.state["runtime_guards"]
        guards["pending_external_event"] = {
            "event_type": "PR_REVIEW_CHANGES_REQUESTED",
            "opportunity_id": task_id,
            "review_refs": list(review_refs or []),
            "parent_commit": parent_commit
        }
        guards["system_status"] = "WAITING"
        self.save_state()

    def record_partial_write(self, task_id: str, branch: str, commit_sha: str, pr_error: str) -> dict:
        """
        现实证据保留（Reality is append-only）：
        push 成功但 create_pr 失败 -> PARTIAL_WRITE_PR_FAILED，
        落盘 branch 与 SHA 证据，严禁执行任何"静默删除远程分支"的抹除动作。
        """
        guards = self.state["runtime_guards"]
        opp = self.state["opportunity_memory"].get(task_id)
        if opp is not None:
            opp["status"] = "PARTIAL_WRITE_PR_FAILED"
            opp["pushed_branch"] = branch
            opp["pushed_sha"] = commit_sha
            opp["pr_error"] = pr_error
        self.state.setdefault("write_evidence", []).append({
            "task_id": task_id,
            "branch": branch,
            "commit_sha": commit_sha,
            "pr_error": pr_error,
            "recorded_at": guards["current_time"],
            "cleanup_action": "NONE"  # 明确声明：无删除动作
        })
        self.save_state()
        return {"success": True, "status": "PARTIAL_WRITE_PR_FAILED"}

    def recover_from_disk(self):
        """进程崩溃重启后的持久化恢复：重载磁盘状态。绝不重复扣费、绝不重复注册。"""
        self.state = self.load_state()
        return self

    # =========================================================================
    # 4. 单个模拟 Tick：决策 → 工具执行 → 验证 → 记账 → 轮询/推进
    # =========================================================================
    def tick(self, agent, dispatcher) -> None:
        guards = self.state["runtime_guards"]
        # 注入系统当前时间，供外部事件桩的时间门控使用
        dispatcher.current_time = guards["current_time"]

        if guards["system_status"] == "WAITING":
            # WAITING 阶段：轮询外部事件（内部结算），随后固定步进 300 秒
            self.poll_waiting_event(dispatcher)
            self.advance_time(300)
            return

        action, llm_decision_cost = agent.get_action(self.state)

        # 空闲 WAIT（无未决事件可等）不构成决策：不扣费、不记账、不拦截
        if action.get("action_type") == "WAIT" and not guards.get("pending_external_event"):
            return

        tool_name = action.get("tool_name")
        tool_result = None
        if tool_name not in (None, "none"):
            # 工具先行执行：硬成本已真实发生，验证拦截的是状态变更而非费用
            tool_result = dispatcher.dispatch(tool_name, action.get("tool_args") or {})

        valid, reason = self.validate_action(action)

        self._action_seq += 1
        action_id = f"act_{self._action_seq:04d}"

        self.apply_action(action_id, action, valid, reason, tool_result, llm_decision_cost)
        self.log_audit({
            "timestamp": guards["current_time"],
            "action_id": action_id,
            "action": action,
            "tool_executed": tool_result is not None,
            "validation": {"valid": valid, "reason": reason}
        })

    def advance_time(self, seconds: int) -> None:
        cur = _parse_iso(self.state["runtime_guards"]["current_time"])
        nxt = cur + timedelta(seconds=seconds)
        self.state["runtime_guards"]["current_time"] = nxt.isoformat()

        infra_cost = (seconds / 3600.0) * CONFIG["infra_hourly_cost"]
        self.state["financials"]["current_cash"] -= infra_cost
        self.state["financials"]["total_hard_cost"] += infra_cost
        self.state["financials"]["cost_breakdown"]["infrastructure"] += infra_cost
        self.save_state()
