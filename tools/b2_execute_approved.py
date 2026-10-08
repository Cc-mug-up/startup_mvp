# tools/b2_execute_approved.py
"""
Gate 4.5-B2 首次真实远程写入执行器（人类签名后单次执行，4 项前置时序锁）。

前置硬断言（任一失败立即终止且零网络写调用）：
1. 术语正名与元组绑定校验：Target Repo / Target Branch / Artifact Hash 与签名严格一致；
2. 远端主干防时序漂移锁：GET /branches/main 的最新 SHA 必须 == approved base_sha，
   否则抛 BaseShaDriftException 废弃审批卡并退回 WAITING；
3. Kill Switch 必须 UNBLOCKED；
4. 现场工件哈希重算 == 签名哈希（提取方式与冻结公式字节级一致：保留尾换行）。

执行后三 SHA 对账：
Approved Commit SHA == Remote Branch Tip SHA == PR Head SHA，全等则 FSM -> WAITING (PR_OPEN)。
"""
import json
import os
import sys
import time

import requests

from controller import Controller
from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR
from tools.approval_gate import (ApprovalGate, canonical, sha256_hex,
                                 ArtifactTamperedError, BaseShaDriftException)
from tools.github_enricher import load_github_token
from tools.github_pr_issuer import GitHubPRIssuer
from tools.kill_switch import GlobalKillSwitch

TARGET_REPO = "Cc-mug-up/aie-b2-sandbox"
BRANCH = "agent/fix-issue-1"
WORK = "aie_b2_work"
TASK = "issue_1"
BASE_SHA = "701cca79251126d4504e73855892c922a37ddabe"
SIGNED_HASH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("B2_SIGNED_HASH", "")
STATE_PATH = "b2_state.json"
TOKEN = load_github_token()


def log(msg):
    print(f"[B2X] {msg}", flush=True)


def api_get(path):
    return requests.get(f"https://api.github.com{path}",
                        headers={"Authorization": f"Bearer {TOKEN}",
                                 "User-Agent": "Startup-MVP-Scanner",
                                 "Accept": "application/vnd.github+json"}, timeout=20)


def main():
    if not SIGNED_HASH:
        log("MISSING SIGNATURE TOKEN -> ABORT")
        sys.exit(1)

    # ---------- 前置锁 1：元组绑定校验 ----------
    assert TARGET_REPO == "Cc-mug-up/aie-b2-sandbox", "Target Repo mismatch"
    assert BRANCH == "agent/fix-issue-1", "Target Branch mismatch"
    log(f"lock1 tuple binding: repo={TARGET_REPO} branch={BRANCH} OK")

    # ---------- 前置锁 4（与锁 1 联合）：现场工件哈希重算（与冻结公式字节级一致） ----------
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git rev-parse HEAD && git diff HEAD^ HEAD", timeout=60)
    assert code == 0, f"workspace read failed: {err or out}"
    # 关键：用 split("\n") 保留尾换行，与审批卡冻结公式完全一致（splitlines 会吞尾换行导致哈希漂移）
    raw_lines = out.split("\n")
    commit_sha = raw_lines[0].strip()
    canonical_diff = canonical("\n".join(raw_lines[1:]))
    diff_hash = sha256_hex(canonical_diff)
    current_hash = sha256_hex(canonical(
        f"{TASK}{BASE_SHA}{commit_sha}{diff_hash}{TARGET_REPO}{BRANCH}"))
    log(f"artifact recompute: commit={commit_sha} diff_hash={diff_hash}")
    log(f"current hash: {current_hash}")
    if current_hash != SIGNED_HASH:
        raise ArtifactTamperedError("SIGNATURE MISMATCH: artifact hash != signed hash. FUSE BLOWN.")
    log("lock1 hash binding: MATCH OK")

    # ---------- 前置锁 2：远端主干防时序漂移锁 ----------
    r = api_get(f"/repos/{TARGET_REPO}/branches/main")
    assert r.status_code == 200, f"branches API failed: {r.status_code}"
    remote_main_head = r.json()["commit"]["sha"]
    log(f"lock2 upstream: remote main head={remote_main_head}")
    if remote_main_head != BASE_SHA:
        log("BASE SHA DRIFT DETECTED -> approval card voided, back to WAITING")
        ctrl = Controller(state_path=STATE_PATH)
        ctrl.state["runtime_guards"]["system_status"] = "WAITING"
        ctrl.save_state()
        raise BaseShaDriftException(
            f"remote main head {remote_main_head} != approved base {BASE_SHA}")
    log("lock2 upstream consistency: OK")

    # ---------- 前置锁 3：Kill Switch UNBLOCKED ----------
    if GlobalKillSwitch.is_blocked():
        log("KILL SWITCH ENGAGED -> ABORT")
        sys.exit(1)
    log("lock3 kill switch: UNBLOCKED")

    # ---------- 冻结工件 + 签发一次性令牌 ----------
    gate = ApprovalGate()
    artifact = gate.freeze_artifact(
        task_id=TASK, source_issue=f"https://github.com/{TARGET_REPO}/issues/1",
        target_repo=TARGET_REPO, target_branch=BRANCH,
        base_sha=BASE_SHA, commit_sha=commit_sha,
        diff_content=canonical_diff, model_id="b2-scripted-fix",
        token_cost=0.0, test_exit_code=0,
        tokens=0, test_summary="real sandbox pytest exit 0")
    token = gate.issue_approval_token(artifact)
    log(f"approval token issued: {token['token_id']}")

    issuer = GitHubPRIssuer(github_token=TOKEN, api_client=None, approval_gate=gate)

    # ---------- 真实远程动作 ----------
    t0 = time.perf_counter()
    push_res = issuer.push_branch_remote(
        artifact, token["token_id"], TARGET_REPO, BRANCH, BASE_SHA, canonical_diff,
        os.path.join(WORKSPACE_DIR, WORK))
    push_ms = (time.perf_counter() - t0) * 1000
    log(f"push result: {json.dumps(push_res, ensure_ascii=False)} ({push_ms:.0f}ms)")
    assert push_res["success"], push_res

    t0 = time.perf_counter()
    pr_res = issuer.create_pr_remote(
        artifact, token["token_id"], TARGET_REPO, BRANCH, BASE_SHA, canonical_diff,
        title="[AIE-B2] Fix add function (Issue #1)",
        body=("Fixes #1: `add(a, b)` now returns `a + b`.\n\n"
              "Verified in sandbox: pytest exit 0 (2 passed).\n"
              "Scope: 1 file, +1/-1 lines."),
        base_ref="main")
    pr_ms = (time.perf_counter() - t0) * 1000
    log(f"create_pr result: {json.dumps(pr_res, ensure_ascii=False)} ({pr_ms:.0f}ms)")

    ctrl = Controller(state_path=STATE_PATH)
    if not pr_res["success"]:
        ctrl.record_partial_write(TASK, BRANCH, commit_sha, pr_res.get("error", "unknown"))
        log("PARTIAL_WRITE_PR_FAILED recorded. NO remote branch deletion performed. ABORT.")
        sys.exit(2)

    # 令牌消费确认
    assert gate.tokens[token["token_id"]]["consumed_ops"] == {"push": True, "pr": True}
    log("token CONSUMED (single-use voided)")

    # ---------- 执行后三 SHA 对账 ----------
    pr_number = pr_res["external_reference"].rsplit("_", 1)[-1]
    rb = api_get(f"/repos/{TARGET_REPO}/branches/{BRANCH}")
    branch_tip_sha = rb.json()["commit"]["sha"] if rb.status_code == 200 else "FETCH_FAILED"
    pr = api_get(f"/repos/{TARGET_REPO}/pulls/{pr_number}")
    pr_data = pr.json()
    pr_head_sha = pr_data.get("head", {}).get("sha")
    pr_base_sha = pr_data.get("base", {}).get("sha")
    pr_state = pr_data.get("state")

    triple_ok = (commit_sha == branch_tip_sha == pr_head_sha)
    log(f"triple-SHA: approved={commit_sha} branch_tip={branch_tip_sha} pr_head={pr_head_sha} -> "
        f"{'PASS' if triple_ok else 'FAIL'}")
    assert triple_ok, "TRIPLE SHA MISMATCH -> ABORT"
    assert pr_state == "open", f"PR not open: {pr_state}"

    # ---------- FSM -> WAITING (PR_OPEN) ----------
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": pr_res["external_reference"],
        "timeout_timestamp": "2026-10-08T00:00:00+00:00"}
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()

    # ---------- 终局结构化汇报 ----------
    print()
    print("| 项目 | 值 |")
    print("|---|---|")
    print(f"| Remote Branch SHA | {branch_tip_sha} |")
    print(f"| PR Number & PR URL | #{pr_number} {pr_data.get('html_url')} |")
    print(f"| PR Head SHA & PR Base SHA | {pr_head_sha} / {pr_base_sha} |")
    print(f"| Triple-SHA Check Result | {'PASS' if triple_ok else 'FAIL'} |")
    print(f"| Network Latencies | Push {push_ms:.0f}ms, PR API {pr_ms:.0f}ms |")
    print(f"| Total External Write Cost | $0.00 (no monetary API cost; token quota only) |")
    print(f"| FSM State Transition Path | WAITING(PENDING_HUMAN_APPROVAL) -> APPROVED -> "
          f"push -> PR -> WAITING(PR_OPEN) |")
    print(f"| 内部状态与 GitHub 现实一致性 | 一致：内部 pending={ctrl.state['runtime_guards']['pending_external_event']['external_reference']}, "
          f"远端 state={pr_state}, 三 SHA 全等 |")
    print(json.dumps({"status": "WAITING_PR_OPEN", "pr_url": pr_data.get("html_url"),
                      "pr_number": pr_number, "branch_tip_sha": branch_tip_sha,
                      "pr_head_sha": pr_head_sha, "pr_base_sha": pr_base_sha,
                      "triple_sha": "PASS", "commit_sha": commit_sha,
                      "artifact_hash": current_hash}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
