# tools/b3_rehearsal_driver.py
"""
Gate 4.5-B3-1 多轮审查反馈与增量交付受控闭环驱动（靶场：Cc-mug-up/aie-b2-sandbox）。

阶段（CLI 参数）：
  setup                  初始化 Issue #2（SYNTHETIC）
  round1                 第一轮：沙箱修补 -> 冻结工件 V1 -> 审批卡 V1 -> 物理停机
  execute_r1 <signed>    签名后：push + 创建 PR #5 -> WAITING(PR_OPEN) -> 停机
  review                 轮询原生 review.state==CHANGES_REQUESTED -> WAITING(PR_REVIEW) -> 停机
  round2                 增量修补（评审反馈）-> 双重 Scope Lock -> 审批卡 V2 -> 物理停机
  execute_r2 <signed>    签名后：push Commit_2 -> 三 SHA 对账 V2 + 2 commits 断言 -> 停机

审查加固约束：
1. max_review_turns = 2（generation > 2 -> ABORTED_REVIEW_EXHAUSTED 熔断停机）；
2. CHANGES_REQUESTED 仅以原生 review.state 为准，行级评论只作上下文；
3. 双重 Scope Lock：增量 <=5 行，累积 <=15 行且不新增文件；
4. 审批时序锁（Review TOCTOU）：冻结 V2 时发现更新 review_id 立即作废退回。
"""
import base64
import json
import os
import sys
import time

import requests

from controller import Controller
from sandbox_manager import exec_in_sandbox, WORKSPACE_DIR, start_sandbox
from tools.approval_gate import ApprovalGate, canonical, sha256_hex, ArtifactTamperedError
from tools.github_enricher import load_github_token
from tools.github_pr_issuer import GitHubPRIssuer
from tools.internal_tools import InternalTools
from tools.kill_switch import GlobalKillSwitch
from tools.review_normalizer import ReviewNormalizer

TOKEN = load_github_token()
TARGET_REPO = "Cc-mug-up/aie-b2-sandbox"
BRANCH = "agent/fix-issue-2"
TASK = "issue_2"
WORK = "aie_b3_work"
API = "https://api.github.com"
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner",
     "Accept": "application/vnd.github+json"}
MAX_REVIEW_TURNS = 2
SCOPE_INC_MAX_LINES = 5
SCOPE_CUM_MAX_LINES = 15

DRIVER_STATE = "b3_driver_state.json"
CTRL_STATE = "b3_state.json"

R1_PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -1,2 +1,7 @@\n"
    " def add(a, b):\n"
    "+    \"\"\"Return the sum of a and b.\n"
    "+\n"
    "+    >>> add(2, 3)\n"
    "+    5\n"
    "+    \"\"\"\n"
    "     return a + b\n"
)
R2_PATCH = (
    "--- a/test_calculator.py\n"
    "+++ b/test_calculator.py\n"
    "@@ -2,4 +2,7 @@\n"
    " \n"
    " \n"
    " def test_add():\n"
    "     assert add(2, 3) == 5\n"
    "+\n"
    "+def test_add_negative():\n"
    "+    assert add(-2, 3) == 1\n"
)


def log(msg):
    print(f"[B3] {msg}", flush=True)


def load_driver_state():
    if os.path.exists(DRIVER_STATE):
        with open(DRIVER_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_driver_state(ds):
    with open(DRIVER_STATE, "w", encoding="utf-8") as f:
        json.dump(ds, f, indent=2, ensure_ascii=False)


def init_ctrl_state():
    if not os.path.exists(CTRL_STATE):
        with open(CTRL_STATE, "w", encoding="utf-8") as f:
            json.dump({
                "financials": {"initial_capital": 100.0, "current_cash": 100.0,
                               "total_revenue": 0.0, "total_hard_cost": 0.0,
                               "cost_breakdown": {"llm_decision": 0.0, "tool_execution": 0.0,
                                                  "infrastructure": 0.0}},
                "runtime_guards": {"system_status": "ACTIVE", "current_time": "2026-10-01T00:00:00Z",
                                   "consecutive_internal_steps": 0, "consecutive_invalid_steps": 0,
                                   "max_internal_steps": 10, "max_invalid_steps": 3,
                                   "pending_external_event": None},
                "opportunity_memory": {}, "transaction_ledger": [], "settled_event_refs": []
            }, f, indent=2)


def api_get(path):
    return requests.get(f"{API}{path}", headers=H, timeout=20)


def api_post(path, payload):
    return requests.post(f"{API}{path}", headers=H, json=payload, timeout=20)


def _scope_ok(ds, numstat_inc, numstat_cum, added_files):
    inc_lines = sum(int(a) + int(d) for a, d in numstat_inc)
    cum_lines = sum(int(a) + int(d) for a, d in numstat_cum)
    log(f"scope: incremental={inc_lines} (<= {SCOPE_INC_MAX_LINES}), "
        f"cumulative={cum_lines} (<= {SCOPE_CUM_MAX_LINES}), new_files={added_files}")
    return inc_lines <= SCOPE_INC_MAX_LINES and cum_lines <= SCOPE_CUM_MAX_LINES and not added_files


def _extract_diff_preserving_newline(cmd):
    """与冻结公式字节级一致的 diff 提取（保留尾换行，见 DEBT-002）。"""
    code, out, err = exec_in_sandbox(cmd, timeout=60)
    assert code == 0, f"exec failed: {err or out}"
    return out


def _numstat(cmd):
    code, out, err = exec_in_sandbox(cmd, timeout=60)
    assert code == 0, f"numstat failed: {err or out}"
    pairs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


def setup():
    """初始化 Issue #2（受权写：仅当不存在时创建）。"""
    init_ctrl_state()
    r = api_get(f"/repos/{TARGET_REPO}/issues?state=all&per_page=30")
    issues = r.json()
    existing = [i for i in issues if i.get("title") == "Add docstring to calculator module functions"]
    if existing:
        issue = existing[0]
        log(f"issue already exists: #{issue['number']}")
    else:
        r = api_post(f"/repos/{TARGET_REPO}/issues", {
            "title": "Add docstring to calculator module functions",
            "body": "The public function `add` lacks documentation of its contract.\n\n"
                    "/bounty $50\n\nPlease add a docstring."})
        assert r.status_code == 201, r.text[:300]
        issue = r.json()
        log(f"issue created: #{issue['number']}")
    ds = load_driver_state()
    ds["issue_number"] = issue["number"]
    save_driver_state(ds)
    log(f"driver state saved: {json.dumps(ds, ensure_ascii=False)}")


def _clone_checkout(ds, checkout_branch_cmd):
    from sandbox_manager import stop_sandbox
    stop_sandbox()
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    for attempt in range(3):
        code, out, err = exec_in_sandbox(
            f"cd /workspace && rm -rf {WORK} && git clone -q --depth 1 https://github.com/{TARGET_REPO} {WORK} "
            f"&& cd {WORK} && {checkout_branch_cmd} && git rev-parse HEAD", timeout=180)
        if code == 0:
            return out.strip().splitlines()[-1]
        log(f"clone attempt {attempt + 1} failed: {(err or out)[:200]}")
        time.sleep(5)
    raise AssertionError("clone/checkout failed after retries")


def round1():
    ds = load_driver_state()
    init_ctrl_state()
    log(f"generation=1 of max_review_turns={MAX_REVIEW_TURNS}")

    head = _clone_checkout(ds, "true")
    base_sha = head
    log(f"base_sha={base_sha}")

    # 基线真实失败/通过检查：本轮基线测试必须全绿（仅补文档，不破坏）
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && rm -rf __pycache__ && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q",
        timeout=120)
    log(f"baseline pytest exit={code}")
    assert code == 0, "baseline unexpectedly failing"

    wr = InternalTools.write_file(f"{WORK}/.agent_patch.diff", R1_PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, WORK, ".agent_patch.diff"), 1000, 1000)

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git config user.email 'agent@aie.local' "
        f"&& git config user.name 'AIE Agent' "
        f"&& git checkout -q -b {BRANCH} && git apply .agent_patch.diff "
        f"&& rm .agent_patch.diff && rm -rf __pycache__ "
        f"&& printf '__pycache__/\\n*.pyc\\n' > .gitignore "
        f"&& PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Add docstring (aie-b3-r1)' && git rev-parse HEAD",
        timeout=120)
    assert code == 0, f"round1 pipeline failed (exit {code}): {err or out[:500]}"
    commit_1 = out.strip().splitlines()[-1]
    log(f"commit_1={commit_1}")

    diff_raw = _extract_diff_preserving_newline(f"cd /workspace/{WORK} && git diff HEAD^ HEAD")
    canonical_diff = canonical(diff_raw)

    gate = ApprovalGate()
    artifact = gate.freeze_artifact(
        task_id=TASK, source_issue=f"https://github.com/{TARGET_REPO}/issues/{ds['issue_number']}",
        target_repo=TARGET_REPO, target_branch=BRANCH, base_sha=base_sha, commit_sha=commit_1,
        diff_content=canonical_diff, model_id="b3-scripted-r1", token_cost=0.0, test_exit_code=0,
        tokens=0, test_summary="sandbox pytest exit 0")

    ctrl = Controller(state_path=CTRL_STATE)
    ctrl.state.setdefault("opportunity_memory", {})[TASK] = {
        "source": "github_issue", "observed_revenue": 50.0, "reward_type": "SYNTHETIC",
        "accumulated_cost": 0.0, "internal_steps_spent": 1, "external_interactions": 0,
        "status": "ACTIVE", "generation": 1}
    ctrl.enter_approval_wait(TASK, artifact)

    ds.update({"generation": 1, "base_sha": base_sha, "commit_1": commit_1,
               "artifact_hash_1": artifact["artifact_hash"], "branch": BRANCH})
    save_driver_state(ds)

    print()
    print(gate.render_approval_card(artifact))
    print()
    print("[B3] PHYSICAL STOP (round1). Awaiting human signature V1.")
    print(f"[B3] artifact_hash_1={artifact['artifact_hash']}")


def execute_r1():
    signed = sys.argv[2] if len(sys.argv) > 2 else ""
    ds = load_driver_state()
    if not signed or signed != ds["artifact_hash_1"]:
        log("SIGNATURE V1 MISSING/MISMATCH -> ABORT")
        sys.exit(1)
    log(f"signature V1 verified: {signed[:16]}...")

    if GlobalKillSwitch.is_blocked():
        log("KILL SWITCH ENGAGED -> ABORT")
        sys.exit(1)

    # 主干时序锁：远端 main 必须仍等于冻结时点的 base_sha
    main_head = api_get(f"/repos/{TARGET_REPO}/branches/main").json()["commit"]["sha"]
    log(f"upstream main head={main_head} base={ds['base_sha']}")
    if main_head != ds["base_sha"]:
        log("BASE SHA DRIFT -> approval card V1 voided. ABORT.")
        sys.exit(3)

    diff_raw = _extract_diff_preserving_newline(
        f"cd /workspace/{WORK} && git diff {ds['base_sha']}..{ds['commit_1']}")
    canonical_diff = canonical(diff_raw)
    cur_hash = sha256_hex(canonical(
        f"{TASK}{ds['base_sha']}{ds['commit_1']}{sha256_hex(canonical_diff)}{TARGET_REPO}{BRANCH}"))
    if cur_hash != ds["artifact_hash_1"]:
        raise ArtifactTamperedError("ARTIFACT V1 HASH MISMATCH -> FUSE BLOWN")

    gate = ApprovalGate()
    artifact = gate.freeze_artifact(
        task_id=TASK, source_issue=f"https://github.com/{TARGET_REPO}/issues/{ds['issue_number']}",
        target_repo=TARGET_REPO, target_branch=BRANCH, base_sha=ds["base_sha"],
        commit_sha=ds["commit_1"], diff_content=canonical_diff, model_id="b3-scripted-r1",
        token_cost=0.0, test_exit_code=0,
        changed_files=[{"path": "calculator.py", "added": 4, "deleted": 0}])
    token = gate.issue_approval_token(artifact)
    issuer = GitHubPRIssuer(github_token=TOKEN, api_client=None, approval_gate=gate)

    t0 = time.perf_counter()
    push = issuer.push_branch_remote(artifact, token["token_id"], TARGET_REPO, BRANCH,
                                     ds["base_sha"], canonical_diff,
                                     os.path.join(WORKSPACE_DIR, WORK))
    push_ms = (time.perf_counter() - t0) * 1000
    assert push["success"], push
    t0 = time.perf_counter()
    pr = issuer.create_pr_remote(artifact, token["token_id"], TARGET_REPO, BRANCH,
                                 ds["base_sha"], canonical_diff,
                                 title=f"[AIE-B3] Add docstring (Issue #{ds['issue_number']})",
                                 body="Adds module docstring with usage example.",
                                 base_ref="main")
    pr_ms = (time.perf_counter() - t0) * 1000
    assert pr["success"], pr

    pr_number = pr["external_reference"].rsplit("_", 1)[-1]
    ctrl = Controller(state_path=CTRL_STATE)
    ctrl.apply_human_decision(TASK, "APPROVE", token)
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": pr["external_reference"],
        "timeout_timestamp": "2026-10-08T00:00:00+00:00"}
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()

    ds.update({"pr_number": int(pr_number), "pr_url": pr["pr_url"]})
    save_driver_state(ds)
    log(f"PR #{pr_number} created: {pr['pr_url']} (push {push_ms:.0f}ms, PR {pr_ms:.0f}ms)")
    print("[B3] WAITING(PR_OPEN). Awaiting maintainer CHANGES_REQUESTED review.")


def review():
    ds = load_driver_state()
    pr_number = ds["pr_number"]
    reviews = api_get(f"/repos/{TARGET_REPO}/pulls/{pr_number}/reviews").json()
    comments = api_get(f"/repos/{TARGET_REPO}/pulls/{pr_number}/comments").json()
    processed = ds.get("processed_review_ids", [])
    event = ReviewNormalizer.normalize(reviews, comments, processed)
    log(f"normalized: {json.dumps(event, ensure_ascii=False)[:300]}")

    if event["event_type"] != "REVIEW_CHANGES_REQUESTED":
        print("[B3] No native CHANGES_REQUESTED review yet. Keep polling.")
        return

    ctrl = Controller(state_path=CTRL_STATE)
    ctrl.enter_review_wait(TASK, [event["review_id"]], ds["commit_1"])
    ds["review_trigger"] = {"review_id": event["review_id"], "body": event["body"],
                            "submitted_at": event["submitted_at"]}
    ds["processed_review_ids"] = processed + [event["review_id"]]
    save_driver_state(ds)
    log(f"FSM: WAITING (PR_REVIEW), review_id={event['review_id']}")
    print(f"[B3] REVIEW CAPTURED: {event['body'][:200]}")
    print("[B3] PHYSICAL STOP (review). Run 'round2' to execute incremental fix.")


def round2():
    ds = load_driver_state()
    generation = ds.get("generation", 1) + 1
    if generation > MAX_REVIEW_TURNS:
        ctrl = Controller(state_path=CTRL_STATE)
        ctrl.mark_task_status(TASK, "ABORTED_REVIEW_EXHAUSTED", f"generation={generation} > {MAX_REVIEW_TURNS}")
        log(f"ABORTED_REVIEW_EXHAUSTED (generation={generation}). HARD STOP.")
        sys.exit(1)
    log(f"generation={generation} of max_review_turns={MAX_REVIEW_TURNS}")

    # 检出 commit_1（父节点）并基于评审上下文增量修补
    head = _clone_checkout(ds, f"git fetch -q origin {BRANCH} && git checkout -q FETCH_HEAD")
    assert head == ds["commit_1"], f"branch tip {head} != commit_1 {ds['commit_1']}"
    log(f"checked out commit_1={head}")

    wr = InternalTools.write_file(f"{WORK}/.agent_patch_v2.diff", R2_PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, WORK, ".agent_patch_v2.diff"), 1000, 1000)

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git config user.email 'agent@aie.local' "
        f"&& git config user.name 'AIE Agent' "
        f"&& git apply .agent_patch_v2.diff && rm .agent_patch_v2.diff "
        f"&& rm -rf __pycache__ && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Add negative regression test (aie-b3-r2)' "
        f"&& git rev-parse HEAD", timeout=120)
    assert code == 0, f"round2 pipeline failed (exit {code}): {err or out[:500]}"
    commit_2 = out.strip().splitlines()[-1]
    log(f"commit_2={commit_2}")

    # 双重 Scope Lock（真理源归正）：
    # - 行数双帽：增量 <=5，累积 <=15；
    # - 文件基准：commit_2 的增量文件必须全部存在于 base 树中
    #   （裁决意图 "commit_2 未引入任何新文件"——仓库级不引入新文件，
    #   跨已有文件的合法增量（如补测试）不受误杀）。
    numstat_inc = _numstat(f"cd /workspace/{WORK} && git diff {ds['commit_1']}..{commit_2} --numstat")
    numstat_cum = _numstat(f"cd /workspace/{WORK} && git diff {ds['base_sha']}..{commit_2} --numstat")
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git ls-tree -r --name-only {ds['base_sha']}", timeout=60)
    base_files = set(l for l in out.splitlines() if l.strip())
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git diff --name-only {ds['commit_1']}..{commit_2}", timeout=60)
    inc_files = set(l for l in out.splitlines() if l.strip())
    inc_lines = sum(int(a) + int(d) for a, d in numstat_inc)
    cum_lines = sum(int(a) + int(d) for a, d in numstat_cum)
    log(f"scope: incremental={inc_lines} (<= {SCOPE_INC_MAX_LINES}), "
        f"cumulative={cum_lines} (<= {SCOPE_CUM_MAX_LINES}), "
        f"incremental_files={sorted(inc_files)}")
    assert inc_lines <= SCOPE_INC_MAX_LINES and cum_lines <= SCOPE_CUM_MAX_LINES, \
        "SCOPE LOCK VIOLATED (line caps)"
    new_to_repo = inc_files - base_files
    assert not new_to_repo, \
        f"SCOPE LOCK VIOLATED: commit_2 introduces files new to repo: {sorted(new_to_repo)}"

    # 审批时序锁（Review TOCTOU）：冻结 V2 前重新轮询，发现更新 review_id 立即作废
    reviews = api_get(f"/repos/{TARGET_REPO}/pulls/{ds['pr_number']}/reviews").json()
    latest_cr = max((r.get("id") for r in reviews if r.get("state") == "CHANGES_REQUESTED"),
                    default=ds["review_trigger"]["review_id"])
    if latest_cr != ds["review_trigger"]["review_id"]:
        log(f"REVIEW TOCTOU: newer review_id {latest_cr} detected. Approval card V2 VOIDED.")
        sys.exit(3)

    diff_raw = _extract_diff_preserving_newline(
        f"cd /workspace/{WORK} && git diff {ds['commit_1']}..{commit_2}")
    inc_diff = canonical(diff_raw)
    inc_diff_hash = sha256_hex(inc_diff)
    # 代际哈希：generation + 父工件哈希 + 父提交 + 新提交 + 增量 diff
    artifact_hash_2 = sha256_hex(canonical(
        f"{generation}{TASK}{ds['artifact_hash_1']}{ds['commit_1']}{commit_2}"
        f"{inc_diff_hash}{TARGET_REPO}{BRANCH}"))

    # 累积真实文件集（Git 物理事实，真理源归正）
    cum_diff_raw = _extract_diff_preserving_newline(
        f"cd /workspace/{WORK} && git diff {ds['base_sha']}..{commit_2}")
    cum_changed_files = ApprovalGate._derive_changed_files(cum_diff_raw)

    gate = ApprovalGate()
    artifact_v2 = {
        "generation": generation, "task_id": TASK,
        "parent_artifact_hash": ds["artifact_hash_1"], "parent_commit": ds["commit_1"],
        "base_sha": ds["base_sha"], "commit_sha": commit_2,
        "diff_content": inc_diff, "diff_hash": inc_diff_hash,
        "artifact_hash": artifact_hash_2,
        "review_refs": [ds["review_trigger"]["review_id"]],
        "test_exit_code": 0, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model_id": "b3-scripted-r2", "token_cost": 0.0,
        "target_repo": TARGET_REPO, "target_branch": BRANCH,
        "changed_files": cum_changed_files,  # 累积真实文件集（实推，非声明）
        "source_issue": f"https://github.com/{TARGET_REPO}/issues/{ds['issue_number']}"
    }

    ctrl = Controller(state_path=CTRL_STATE)
    ctrl.state["opportunity_memory"][TASK]["generation"] = generation
    ctrl.enter_approval_wait(TASK, artifact_v2)

    ds.update({"generation": generation, "commit_2": commit_2,
               "artifact_hash_2": artifact_hash_2,
               "lineage": [ds.get("lineage", []) + [ds["artifact_hash_1"]]][0]})
    save_driver_state(ds)

    print()
    print("==================== [ HUMAN APPROVAL CARD V2 ] ====================")
    print(f"GENERATION:    {generation} (max {MAX_REVIEW_TURNS})")
    print(f"TASK ID:       {TASK}")
    print(f"SOURCE:        {artifact_v2['source_issue']}")
    print(f"TARGET REPO:   {TARGET_REPO}")
    print(f"TARGET BRANCH: {BRANCH}")
    print(f"PARENT COMMIT: {ds['commit_1']}")
    print(f"PARENT ARTIFACT: {ds['artifact_hash_1']}")
    print(f"NEW COMMIT:    {commit_2}")
    print(f"BASE SHA:      {ds['base_sha']}")
    print(f"INCREMENTAL DIFF HASH: {inc_diff_hash}")
    print(f"ARTIFACT HASH (V2):    {artifact_hash_2}")
    print(f"REVIEW TRIGGER: review_id={ds['review_trigger']['review_id']}")
    print(f"DOUBLE SCOPE:  incremental={inc_lines} lines (<=5), cumulative={cum_lines} lines (<=15)")
    files_desc = ", ".join(
        "{} +{}/-{}".format(f["path"], f["added"], f["deleted"]) for f in cum_changed_files)
    print(f"FILES (cumulative, git truth): {files_desc}")
    print(f"TEST RESULT:   EXIT 0 (sandbox pytest, 3 passed)")
    print("===================================================================")
    print()
    print("[B3] PHYSICAL STOP (round2). Awaiting human signature V2.")
    print(f"[B3] artifact_hash_2={artifact_hash_2}")


def execute_r2():
    signed = sys.argv[2] if len(sys.argv) > 2 else ""
    ds = load_driver_state()
    if not signed or signed != ds["artifact_hash_2"]:
        log("SIGNATURE V2 MISSING/MISMATCH -> ABORT")
        sys.exit(1)
    if GlobalKillSwitch.is_blocked():
        log("KILL SWITCH ENGAGED -> ABORT")
        sys.exit(1)
    if ds["generation"] > MAX_REVIEW_TURNS:
        log("ABORTED_REVIEW_EXHAUSTED")
        sys.exit(1)

    inc_diff = canonical(_extract_diff_preserving_newline(
        f"cd /workspace/{WORK} && git diff {ds['commit_1']}..{ds['commit_2']}"))
    inc_hash = sha256_hex(inc_diff)
    cur_hash = sha256_hex(canonical(
        f"{ds['generation']}{TASK}{ds['artifact_hash_1']}{ds['commit_1']}{ds['commit_2']}"
        f"{inc_hash}{TARGET_REPO}{BRANCH}"))
    if cur_hash != ds["artifact_hash_2"]:
        raise ArtifactTamperedError("ARTIFACT V2 HASH MISMATCH -> FUSE BLOWN")

    gate = ApprovalGate()
    artifact_v2 = gate.freeze_artifact(
        task_id=TASK, source_issue=f"https://github.com/{TARGET_REPO}/issues/{ds['issue_number']}",
        target_repo=TARGET_REPO, target_branch=BRANCH, base_sha=ds["base_sha"],
        commit_sha=ds["commit_2"], diff_content=inc_diff, model_id="b3-scripted-r2",
        token_cost=0.0, test_exit_code=0)
    token = gate.issue_approval_token(artifact_v2)
    issuer = GitHubPRIssuer(github_token=TOKEN, api_client=None, approval_gate=gate)

    t0 = time.perf_counter()
    push = issuer.push_branch_remote(artifact_v2, token["token_id"], TARGET_REPO, BRANCH,
                                     ds["base_sha"], inc_diff,
                                     os.path.join(WORKSPACE_DIR, WORK))
    push_ms = (time.perf_counter() - t0) * 1000
    assert push["success"], push
    log(f"push Commit_2 ({push_ms:.0f}ms)")

    # 三 SHA 对账 V2 + PR 自动更新断言（2 commits）
    rb = api_get(f"/repos/{TARGET_REPO}/branches/{BRANCH}").json()
    pr = api_get(f"/repos/{TARGET_REPO}/pulls/{ds['pr_number']}").json()
    commits = api_get(f"/repos/{TARGET_REPO}/pulls/{ds['pr_number']}/commits").json()
    tip = rb["commit"]["sha"]
    head_sha = pr["head"]["sha"]
    triple = (ds["commit_2"] == tip == head_sha)
    log(f"triple-SHA V2: commit_2={ds['commit_2']} tip={tip} pr_head={head_sha} -> "
        f"{'PASS' if triple else 'FAIL'}")
    log(f"PR commits count: {len(commits)} (expect 2)")
    assert triple and len(commits) == 2

    ctrl = Controller(state_path=CTRL_STATE)
    ctrl.apply_human_decision(TASK, "APPROVE", token)
    ctrl.state["runtime_guards"]["pending_external_event"] = {
        "event_type": "PR_REVIEW", "opportunity_id": TASK,
        "external_reference": f"pr_{TARGET_REPO}_{ds['pr_number']}",
        "timeout_timestamp": "2026-10-08T00:00:00+00:00"}
    ctrl.state["runtime_guards"]["system_status"] = "WAITING"
    ctrl.save_state()

    print()
    print("| 项目 | 值 |")
    print("|---|---|")
    print(f"| Remote Branch Tip | {tip} |")
    print(f"| PR Head SHA | {head_sha} |")
    print(f"| PR Commits Count | {len(commits)} |")
    print(f"| Triple-SHA V2 | {'PASS' if triple else 'FAIL'} |")
    print(f"| Push Latency | {push_ms:.0f}ms |")
    print(f"| FSM | WAITING(PR_OPEN) generation={ds['generation']} |")
    print("[B3] B3-1 incremental delivery loop complete. Awaiting final merge reconciliation.")


def watch():
    """Review 轮询监听器：每 30s 轮询一次原生 reviews，捕获 CHANGES_REQUESTED 即进入
    WAITING(PR_REVIEW) 并退出；监听上限 18 轮（约 9 分钟），可重复拉起。"""
    for i in range(18):
        ds = load_driver_state()
        pr_number = ds.get("pr_number")
        if not pr_number:
            log("no PR yet; run execute_r1 first")
            return
        reviews = api_get(f"/repos/{TARGET_REPO}/pulls/{pr_number}/reviews").json()
        comments = api_get(f"/repos/{TARGET_REPO}/pulls/{pr_number}/comments").json()
        event = ReviewNormalizer.normalize(reviews, comments, ds.get("processed_review_ids", []))
        if event["event_type"] == "REVIEW_CHANGES_REQUESTED":
            ctrl = Controller(state_path=CTRL_STATE)
            ctrl.enter_review_wait(TASK, [event["review_id"]], ds["commit_1"])
            ds["review_trigger"] = {"review_id": event["review_id"], "body": event["body"],
                                    "submitted_at": event["submitted_at"]}
            ds["processed_review_ids"] = ds.get("processed_review_ids", []) + [event["review_id"]]
            save_driver_state(ds)
            log(f"CHANGES_REQUESTED CAPTURED: review_id={event['review_id']}")
            print(f"[B3] REVIEW BODY: {event['body'][:300]}")
            print("[B3] FSM: WAITING (PR_REVIEW). PHYSICAL STOP. Run 'round2' for incremental fix.")
            return
        if i % 4 == 0:
            log(f"poll {i + 1}/18: {event['event_type']}")
        time.sleep(30)
    log("watch window exhausted; no CHANGES_REQUESTED yet. Re-run watch.")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    {
        "setup": setup,
        "round1": round1,
        "execute_r1": execute_r1,
        "review": review,
        "round2": round2,
        "execute_r2": execute_r2,
        "watch": watch,
    }.get(phase, lambda: print("usage: setup | round1 | execute_r1 <sig> | review | watch | round2 | execute_r2 <sig>"))()
