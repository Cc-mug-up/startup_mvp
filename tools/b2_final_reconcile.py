# tools/b2_final_reconcile.py
"""
Gate 4.5-B2 最终状态对账（只读）：PR #4 合并确认 -> FSM DELIVERED (PR_MERGED) -> 生命周期记录。
"""
import json
import time

import requests

from controller import Controller
from tools.github_enricher import load_github_token

TARGET_REPO = "Cc-mug-up/aie-b2-sandbox"
PR_NUMBER = 4
TASK = "issue_1"
STATE_PATH = "b2_state.json"
TOKEN = load_github_token()


class AuthTransport:
    """带鉴权的只读 GET 传输（控制器对账接口注入用）。"""

    def __init__(self, token):
        self.token = token

    def get(self, url, timeout=20):
        return requests.get(url, timeout=timeout,
                            headers={"Authorization": f"Bearer {self.token}",
                                     "User-Agent": "Startup-MVP-Scanner",
                                     "Accept": "application/vnd.github+json"})


def main():
    t0 = time.perf_counter()
    resp = requests.get(
        f"https://api.github.com/repos/{TARGET_REPO}/pulls/{PR_NUMBER}",
        headers={"Authorization": f"Bearer {TOKEN}",
                 "User-Agent": "Startup-MVP-Scanner",
                 "Accept": "application/vnd.github+json"}, timeout=20)
    poll_ms = (time.perf_counter() - t0) * 1000
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    pr = resp.json()

    # 1. 远端状态断言
    print(f"[R] remote state={pr.get('state')} merged={pr.get('merged')} "
          f"merged_at={pr.get('merged_at')} (poll {poll_ms:.0f}ms)")
    assert pr.get("state") == "closed"
    assert pr.get("merged") is True

    # 2. 内部 FSM 自愈翻转（经控制器对账接口，归档证据）
    ctrl = Controller(state_path=STATE_PATH)
    t0 = time.perf_counter()
    r = ctrl.poll_external_pr_status(TASK, TARGET_REPO, PR_NUMBER, transport=AuthTransport(TOKEN))
    reconcile_ms = (time.perf_counter() - t0) * 1000
    print(f"[R] controller reconciliation: {json.dumps(r, ensure_ascii=False)} "
          f"({reconcile_ms:.0f}ms)")
    assert r["correction"] == "DELIVERED/PR_MERGED"

    # 3. 生命周期记录（Lifecycle Record）
    opp = ctrl.state["opportunity_memory"][TASK]
    lifecycle = {
        "task_id": TASK,
        "issue_url": "https://github.com/Cc-mug-up/aie-b2-sandbox/issues/1",
        "pr_number": PR_NUMBER,
        "pr_url": pr.get("html_url"),
        "commit_sha": pr.get("head", {}).get("sha"),
        "base_sha": pr.get("base", {}).get("sha"),
        "merged_at": pr.get("merged_at"),
        "delivered_at": ctrl.state["runtime_guards"]["current_time"],
        "final_status": opp.get("status"),
        "payment_waiting": opp.get("payment_waiting"),
        "state_path": "ACTIVE -> WAITING(PENDING_HUMAN_APPROVAL) -> APPROVED -> push -> PR -> "
                      "WAITING(PR_OPEN) -> DELIVERED(PR_MERGED)",
        "external_write_count": 2,  # push + create_pr（唯一两次外部写）
        "poll_latency_ms": poll_ms,
        "reconcile_latency_ms": reconcile_ms
    }
    print(json.dumps(lifecycle, indent=2, ensure_ascii=False))

    print()
    print("| 项目 | 值 |")
    print("|---|---|")
    print(f"| 远端 PR 状态 | {pr.get('state')} / merged={pr.get('merged')} |")
    print(f"| 合并时间 | {pr.get('merged_at')} |")
    print(f"| 内部 FSM 终态 | {opp.get('status')} ({r['correction']}) |")
    print(f"| payment_waiting 标志 | {opp.get('payment_waiting')} |")
    print(f"| 对账延迟 | API 轮询 {poll_ms:.0f}ms / 控制器对账 {reconcile_ms:.0f}ms |")
    print(f"| 生命周期记录字段数 | {len(lifecycle)} |")
    print(f"| 收入账本 | total_revenue={ctrl.state['financials']['total_revenue']}（交付不等于结算） |")


if __name__ == "__main__":
    main()
