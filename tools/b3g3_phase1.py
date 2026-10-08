# tools/b3g3_phase1.py
"""
Gate 4.5-B3-3 Phase 1：Gen-1 跨库交付与评审监听等待。

底线禁令：
1. 严禁硬编码 Issue/PR 编号——所有实体 ID 严格从 API 创建响应动态提取；
2. 严禁改写历史：无 rebase / amend / force-push，线形 parent 链；
3. 写权限绝对隔离：upstream_push_counter 全生命周期恒 0，写仅指向 Fork。

阶段：
  discover   动态创建乘法算子 Issue -> GEN1_BUILDING
  build      沙箱实现 multiply + 单测 exit 0 -> GEN1_VERIFYING -> 冻结 Gen-1 工件
             -> 打印审批卡 -> 物理停机（零网络写）
  execute <sig>   签名后：Anti-TOCTOU 现场重算 -> push Fork (Commit 1) -> 动态创建跨库 PR
             -> 四重 SHA 对账 -> GEN1_PUSHED -> PR_OPEN -> AWAITING_REVIEW
  listen     只读轮询监听（30s 间隔），捕获 CHANGES_REQUESTED 即记录并停机；
             严禁任何 push/建分支/改代码。
"""
import base64
import json
import os
import sys
import time

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.approval_gate import ApprovalGate, canonical, sha256_hex, ArtifactTamperedError
from tools.b3_fork_driver import ForkFlow, ForkPusher, GitHubAPI
from tools.github_enricher import load_github_token
from tools.internal_tools import InternalTools
from tools.review_normalizer import ReviewNormalizer

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
FORK_REPO = os.environ.get("AIE_FORK_REPO", "")
TOKEN = load_github_token()
STATE_FILE = "b3g3_state.json"
BRANCH = "agent/multiply-issue"
WORK = "aie_g3_work"

PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -2,3 +2,7 @@\n"
    "     if not isinstance(a, int) or not isinstance(b, int):\n"
    "         raise TypeError(\"add requires integer arguments\")\n"
    "     return a + b\n"
    "+\n"
    "+\n"
    "+def multiply(a: int, b: int) -> int:\n"
    "+    return a * b\n"
    "--- a/test_calculator.py\n"
    "+++ b/test_calculator.py\n"
    "@@ -1,3 +1,3 @@\n"
    "-from calculator import add\n"
    "+from calculator import add, multiply\n"
    " \n"
    " import pytest\n"
    "@@ -6,3 +6,7 @@\n"
    " def test_add_rejects_non_numeric():\n"
    "     with pytest.raises(TypeError):\n"
    "         add(\"2\", 3)\n"
    "+\n"
    "+\n"
    "+def test_multiply():\n"
    "+    assert multiply(2, 3) == 6\n"
)


def log(msg):
    print(f"[B3G3] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def api():
    return GitHubAPI(TOKEN)


# ---------------------------------------------------------------- #
def phase_discover():
    a = api()
    st = load_state() or {
        "upstream": {"repo": UPSTREAM_REPO, "approved_base_sha": None, "issue_number": None,
                     "pr_number": None, "pr_id": None},
        "fork": {"repo": FORK_REPO, "branch": BRANCH},
        "fsm": "DISCOVERED",
        "commit_sha": None, "artifact_hash": None, "review": None
    }
    # 确认上游当前 HEAD（动态锁定，杜绝硬编码）
    main_head = a.get(f"/repos/{UPSTREAM_REPO}/branches/main").json()["commit"]["sha"]
    st["upstream"]["approved_base_sha"] = main_head
    log(f"upstream main head locked: {main_head}")

    # 动态创建 Issue（乘法算子需求），从响应提取真实 number
    resp = a.post(f"/repos/{UPSTREAM_REPO}/issues", {
        "title": "Add multiply function with basic tests",
        "body": "calculator.py should provide a multiply(a, b) function supporting basic "
                "positive integers.\n\n/bounty $50\n"})
    assert resp.status_code == 201, f"issue create failed: {resp.status_code} {resp.text[:200]}"
    issue = resp.json()
    st["upstream"]["issue_number"] = int(issue["number"])  # 动态提取，严禁硬编码
    log(f"issue created dynamically: #{st['upstream']['issue_number']}")
    st["fsm"] = "GEN1_BUILDING"
    save_state(st)


def phase_build():
    st = load_state()
    stop_sandbox()
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    work = WORK
    for attempt in range(8):
        code, out, err = exec_in_sandbox(
            f"cd /workspace && rm -rf {work} && git clone -q --depth 1 https://github.com/{FORK_REPO} {work} "
            f"&& cd {work} && git remote add upstream https://github.com/{UPSTREAM_REPO} "
            f"&& git remote set-url --push upstream no_push://upstream_disabled "
            f"&& git fetch -q upstream main && git rev-parse upstream/main", timeout=180)
        if code == 0:
            break
        log(f"clone attempt {attempt + 1}/8 failed: {(err or out)[:150]}")
        time.sleep(15)
    assert code == 0, "clone failed after 8 attempts"
    upstream_head = out.strip().splitlines()[-1]
    assert upstream_head == st["upstream"]["approved_base_sha"], \
        "LOCK-1 VIOLATED: upstream drifted"

    wr = InternalTools.write_file(f"{work}/.agent_patch.diff", PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, work, ".agent_patch.diff"), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git config user.email 'agent@aie.local' "
        f"&& git config user.name 'AIE Agent' "
        f"&& git checkout -q -b {BRANCH} upstream/main "
        f"&& git apply .agent_patch.diff && rm .agent_patch.diff && rm -rf __pycache__ "
        f"&& printf '__pycache__/\\n*.pyc\\n' > .gitignore "
        f"&& PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Add multiply (aie-b3-3 gen1)' && git rev-parse HEAD",
        timeout=180)
    assert code == 0, f"build failed (exit {code}): {err or out[:400]}"
    commit = out.strip().splitlines()[-1]
    st["commit_sha"] = commit
    st["fsm"] = "GEN1_VERIFYING"
    save_state(st)

    code, out, err = exec_in_sandbox(f"cd /workspace/{work} && git diff HEAD^ HEAD", timeout=60)
    assert code == 0, err or out
    diff = canonical(out)
    diff_hash = sha256_hex(diff)
    # Gen-1 独立工件哈希：绑定 upstream/fork 命名空间 + base + commit + diff
    artifact_hash = sha256_hex(canonical(
        f"gen1{st['upstream']['issue_number']}{UPSTREAM_REPO}{FORK_REPO}"
        f"{st['upstream']['approved_base_sha']}{commit}{diff_hash}{BRANCH}"))
    st["artifact_hash"] = artifact_hash
    st["diff_hash"] = diff_hash
    st["fsm"] = "GEN1_WAITING_APPROVAL"
    save_state(st)

    print()
    print("==================== [ HUMAN APPROVAL CARD (B3-3 Gen-1) ] ====================")
    print(f"ISSUE NUMBER:   #{st['upstream']['issue_number']} (dynamic from API)")
    print(f"UPSTREAM REPO:  {UPSTREAM_REPO} (READ-ONLY)")
    print(f"FORK REPO:      {FORK_REPO} (WRITE-ONLY)")
    print(f"APPROVED BASE:  {st['upstream']['approved_base_sha']}")
    print(f"TARGET BRANCH:  {BRANCH}")
    print(f"COMMIT SHA:     {commit}")
    print(f"DIFF HASH:      {diff_hash}")
    print(f"ARTIFACT HASH:  {artifact_hash}")
    print(f"TEST RESULT:    EXIT 0 (sandbox pytest)")
    print("EXTERNAL ACTIONS REQUESTED:")
    print(f"  [X] GIT PUSH to FORK {FORK_REPO}:{BRANCH}")
    print(f"  [X] CREATE PR against {UPSTREAM_REPO}:main (head={FORK_REPO.split('/')[0]}:{BRANCH})")
    print("================================================================================")
    print()
    print("[B3G3] PHYSICAL STOP. Zero external writes. Awaiting human signature.")
    print(f"[B3G3] artifact_hash={artifact_hash}")


def phase_execute():
    signed = sys.argv[2] if len(sys.argv) > 2 else ""
    st = load_state()
    if signed != st["artifact_hash"]:
        log("SIGNATURE MISSING/MISMATCH -> ABORT")
        sys.exit(1)
    a = api()
    # LOCK-1 复验 + Anti-TOCTOU 现场重算
    main_head = a.get(f"/repos/{UPSTREAM_REPO}/branches/main").json()["commit"]["sha"]
    assert main_head == st["upstream"]["approved_base_sha"], "LOCK-1 VIOLATED: drift"
    code, out, err = exec_in_sandbox(f"cd /workspace/{WORK} && git diff HEAD^ HEAD", timeout=60)
    assert code == 0, err or out
    diff = canonical(out)
    cur_hash = sha256_hex(canonical(
        f"gen1{st['upstream']['issue_number']}{UPSTREAM_REPO}{FORK_REPO}"
        f"{st['upstream']['approved_base_sha']}{st['commit_sha']}"
        f"{sha256_hex(diff)}{BRANCH}"))
    if cur_hash != st["artifact_hash"]:
        raise ArtifactTamperedError("ARTIFACT HASH MISMATCH -> FUSE BLOWN")
    log("Anti-TOCTOU recompute: MATCH")

    pusher = ForkPusher(FORK_REPO, UPSTREAM_REPO, TOKEN)
    t0 = time.perf_counter()
    res = pusher.push(FORK_REPO, BRANCH, st["commit_sha"], os.path.join(WORKSPACE_DIR, WORK))
    push_ms = (time.perf_counter() - t0) * 1000
    assert res["success"], res
    assert pusher.upstream_push_counter == 0
    log(f"pushed to fork ({push_ms:.0f}ms), upstream_push_counter=0")
    st["fsm"] = "GEN1_PUSHED"
    save_state(st)

    # 动态创建跨库 PR（head 严格 owner:branch），从响应提取 pr_number/pr_id
    t0 = time.perf_counter()
    resp = a.post(f"/repos/{UPSTREAM_REPO}/pulls", {
        "title": f"[AIE-B3-3] Add multiply (Issue #{st['upstream']['issue_number']})",
        "head": f"{FORK_REPO.split('/')[0]}:{BRANCH}",
        "base": "main",
        "body": "Adds multiply(a, b) with regression test. Sandbox pytest exit 0."})
    pr_ms = (time.perf_counter() - t0) * 1000
    assert resp.status_code in (200, 201), f"PR failed: {resp.status_code} {resp.text[:200]}"
    pr = resp.json()
    st["upstream"]["pr_number"] = int(pr["number"])   # 动态提取
    st["upstream"]["pr_id"] = int(pr["id"])           # 动态提取
    log(f"PR created dynamically: #{st['upstream']['pr_number']} (pr_id={st['upstream']['pr_id']}, {pr_ms:.0f}ms)")

    # 四重 SHA
    tip = a.get(f"/repos/{FORK_REPO}/branches/{BRANCH}").json()["commit"]["sha"]
    pr_data = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{st['upstream']['pr_number']}").json()
    triple = (st["commit_sha"] == tip == pr_data["head"]["sha"])
    base_ok = (pr_data["base"]["sha"] == st["upstream"]["approved_base_sha"])
    assert triple and base_ok, f"quad-SHA FAIL: triple={triple} base_ok={base_ok}"
    st["fsm"] = "AWAITING_REVIEW"
    save_state(st)
    log(f"quad-SHA PASS. FSM=AWAITING_REVIEW. PR URL: {pr_data.get('html_url')}")
    print(json.dumps({"fsm": "AWAITING_REVIEW", "pr_number": st["upstream"]["pr_number"],
                      "pr_url": pr_data.get("html_url"), "triple_sha": "PASS",
                      "upstream_push_counter": 0}, indent=2))


def phase_listen():
    """
    只读轮询监听（30s，严格三条件判定基准）：
    1. 仅捕获原生 review.state == "CHANGES_REQUESTED"（严禁 COMMENTED 降级或文本解析）；
    2. review.commit_id 必须严格等于 Gen-1 Commit SHA；
    3. review.user.login 必须不等于 PR 创建者（FORK_REPO owner）。
    捕获合法事件后：提取真实 review_id -> REVIEW_CAPTURED -> 物理停机，
    输出原始 JSON 载荷。严禁任何 push/建分支/改代码，严禁自行触发 Gen-2。
    """
    st = load_state()
    a = api()
    pr_number = st["upstream"]["pr_number"]
    commit_sha = st["commit_sha"]
    creator = FORK_REPO.split("/")[0]  # PR 创建者 = fork owner
    processed = st.get("processed_review_ids", [])
    for i in range(18):
        reviews = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{pr_number}/reviews").json()
        for rev in sorted(reviews, key=lambda r: r.get("submitted_at") or "", reverse=True):
            rid = rev.get("id")
            if rid in processed:
                continue
            state = rev.get("state")
            commit_id = rev.get("commit_id")
            login = (rev.get("user") or {}).get("login")
            # 严格三条件：缺一即不捕获
            cond1 = (state == "CHANGES_REQUESTED")
            cond2 = (commit_id == commit_sha)
            cond3 = (login is not None and login != creator)
            verdict = "OK" if (cond1 and cond2 and cond3) else \
                f"skip(state={state}, commit_ok={cond2}, login={login}, creator={creator})"
            if cond1 and cond2 and cond3:
                st["review"] = {
                    "review_id": rid,
                    "raw_payload": rev,  # 原始 JSON 载荷完整留存
                    "body": rev.get("body") or "",
                    "commit_id": commit_id,
                    "user_login": login,
                    "state": state,
                    "submitted_at": rev.get("submitted_at")
                }
                st["processed_review_ids"] = processed + [rid]
                st["fsm"] = "REVIEW_CAPTURED"
                save_state(st)
                with open("b3g3_review_payload.json", "w", encoding="utf-8") as f:
                    json.dump(rev, f, indent=2, ensure_ascii=False)
                log(f"REVIEW CAPTURED (strict): review_id={rid} user={login} "
                    f"commit_id={commit_id} fsm=REVIEW_CAPTURED")
                print(f"[B3G3] RAW REVIEW PAYLOAD: {json.dumps(rev, ensure_ascii=False)[:600]}")
                print("[B3G3] PHYSICAL STOP. Phase 2 (Gen-2) NOT started. Awaiting directive.")
                return
            if state == "CHANGES_REQUESTED":
                log(f"rejected review (fails strict criteria): {verdict}")
        if i % 4 == 0:
            log(f"listen poll {i + 1}/18: no qualifying review yet")
        time.sleep(30)
    log("listen window exhausted; no qualifying review yet. Re-run listen.")


GEN2_PATCH = (
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -7,2 +7,4 @@\n"
    " def multiply(a: int, b: int) -> int:\n"
    "+    if not isinstance(a, int) or not isinstance(b, int):\n"
    "+        raise TypeError(\"multiply requires integer arguments\")\n"
    "     return a * b\n"
    "--- a/test_calculator.py\n"
    "+++ b/test_calculator.py\n"
    "@@ -11,2 +11,8 @@\n"
    " def test_multiply():\n"
    "     assert multiply(2, 3) == 6\n"
    "+\n"
    "+\n"
    "+def test_multiply_negative():\n"
    "+    assert multiply(-2, 3) == -6\n"
    "+\n"
    "+\n"
    "+def test_multiply_rejects_non_numeric():\n"
    "+    with pytest.raises(TypeError):\n"
    "+        multiply(\"2\", 3)\n"
)


def phase_gen2_build():
    st = load_state()
    assert st["fsm"] == "REVIEW_CAPTURED", f"fsm={st['fsm']} not REVIEW_CAPTURED"
    parent = st["commit_sha"]
    assert st["review"]["commit_id"] == parent, "review target commit mismatch"
    st["fsm"] = "GEN2_BUILDING"
    save_state(st)
    log(f"GEN2_BUILDING on branch {BRANCH} (parent={parent})")

    # 复用 Phase 1 工作区（严禁 reset/rebase，保持线形 parent 链）
    work = WORK
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git rev-parse HEAD 2>/dev/null || echo MISSING", timeout=60)
    if out.strip() != parent:
        stop_sandbox()
        start_sandbox()
        os.chown(WORKSPACE_DIR, 1000, 1000)
        for attempt in range(8):
            code, out, err = exec_in_sandbox(
                f"cd /workspace && rm -rf {work} && git clone -q --depth 5 https://github.com/{FORK_REPO} {work} "
                f"&& cd {work} && git fetch -q origin {BRANCH} && git checkout -q FETCH_HEAD "
                f"&& git rev-parse HEAD", timeout=180)
            if code == 0 and out.strip().splitlines()[-1] == parent:
                break
            log(f"re-clone attempt {attempt + 1}/8 failed: {(err or out)[:120]}")
            time.sleep(15)
        assert code == 0 and out.strip().splitlines()[-1] == parent, "workspace restore failed"

    wr = InternalTools.write_file(f"{work}/.agent_patch_v2.diff", GEN2_PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, work, ".agent_patch_v2.diff"), 1000, 1000)
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git apply .agent_patch_v2.diff && rm .agent_patch_v2.diff "
        f"&& rm -rf __pycache__ && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Harden multiply (aie-b3-3 gen2)' && git rev-parse HEAD",
        timeout=180)
    assert code == 0, f"gen2 build failed (exit {code}): {err or out[:400]}"
    commit2 = out.strip().splitlines()[-1]
    # 物理断言：线形 parent 链（严禁 reset/rebase）
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git rev-parse HEAD^", timeout=60)
    assert out.strip() == parent, f"LINEAGE VIOLATED: parent={out.strip()} != {parent}"
    st["commit2_sha"] = commit2
    st["fsm"] = "GEN2_VERIFYING"
    save_state(st)
    log(f"commit2={commit2} (parent={parent}, linear lineage asserted)")

    # Scope Lock：仅允许 calculator.py 与单测文件
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git diff --name-only {parent}..{commit2}", timeout=60)
    inc_files = {l for l in out.splitlines() if l.strip()}
    assert inc_files <= {"calculator.py", "test_calculator.py"}, \
        f"SCOPE VIOLATED: {sorted(inc_files)}"
    log(f"scope files ok: {sorted(inc_files)}")

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git diff {parent}..{commit2}", timeout=60)
    assert code == 0, err or out
    inc_diff = canonical(out)
    diff_hash = sha256_hex(inc_diff)
    # Gen-2 工件哈希：密码学包含全部 8 个代际绑定要素
    artifact_hash2 = sha256_hex(canonical(
        f"{st['artifact_hash']}|{st['review']['review_id']}|{parent}|{commit2}"
        f"|{diff_hash}|{UPSTREAM_REPO}|{FORK_REPO}|{st['upstream']['approved_base_sha']}"))
    st["artifact_hash_gen2"] = artifact_hash2
    st["diff_hash_gen2"] = diff_hash
    st["fsm"] = "GEN2_WAITING_APPROVAL"
    save_state(st)

    print()
    print("==================== [ HUMAN APPROVAL CARD (B3-3 Gen-2) ] ====================")
    print(f"ISSUE NUMBER:   #{st['upstream']['issue_number']}")
    print(f"PR NUMBER:      #{st['upstream']['pr_number']}")
    print(f"REVIEW BINDING: review_id={st['review']['review_id']} "
          f"reviewer={st['review']['user_login']}")
    print(f"LINEAGE:        Commit 1 {parent}  ->  Commit 2 {commit2}")
    print(f"GEN1 ARTIFACT:  {st['artifact_hash']}")
    print(f"UPSTREAM REPO:  {UPSTREAM_REPO} (READ-ONLY)")
    print(f"FORK REPO:      {FORK_REPO} (WRITE-ONLY)")
    print(f"APPROVED BASE:  {st['upstream']['approved_base_sha']}")
    print(f"TARGET BRANCH:  {BRANCH}")
    print(f"INCREMENTAL DIFF HASH: {diff_hash}")
    print(f"ARTIFACT HASH (Gen-2): {artifact_hash2}")
    print(f"CHANGED FILES:  {', '.join(sorted(inc_files))} (scope-locked)")
    print(f"TEST RESULT:    EXIT 0 (sandbox full-suite pytest)")
    print("EXTERNAL ACTIONS REQUESTED:")
    print(f"  [X] GIT PUSH Commit 2 to FORK {FORK_REPO}:{BRANCH}")
    print("================================================================================")
    print()
    print("[B3G3] PHYSICAL STOP (gen2). Zero external writes. Awaiting human signature Gen-2.")
    print(f"[B3G3] artifact_hash_gen2={artifact_hash2}")


def phase_gen2_execute():
    signed = sys.argv[2] if len(sys.argv) > 2 else ""
    st = load_state()
    if signed != st["artifact_hash_gen2"]:
        log("SIGNATURE GEN-2 MISSING/MISMATCH -> ABORT")
        sys.exit(1)
    a = api()
    # LOCK-1 复验
    main_head = a.get(f"/repos/{UPSTREAM_REPO}/branches/main").json()["commit"]["sha"]
    assert main_head == st["upstream"]["approved_base_sha"], "LOCK-1 VIOLATED: drift"
    # Anti-TOCTOU 现场重算（8 要素代际哈希）
    parent = st["commit_sha"]
    commit2 = st["commit2_sha"]
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{WORK} && git diff {parent}..{commit2}", timeout=60)
    assert code == 0, err or out
    inc_diff = canonical(out)
    cur_hash = sha256_hex(canonical(
        f"{st['artifact_hash']}|{st['review']['review_id']}|{parent}|{commit2}"
        f"|{sha256_hex(inc_diff)}|{UPSTREAM_REPO}|{FORK_REPO}|{st['upstream']['approved_base_sha']}"))
    if cur_hash != st["artifact_hash_gen2"]:
        raise ArtifactTamperedError("ARTIFACT GEN-2 HASH MISMATCH -> FUSE BLOWN")
    log("Anti-TOCTOU gen2 recompute: MATCH")

    pusher = ForkPusher(FORK_REPO, UPSTREAM_REPO, TOKEN)
    t0 = time.perf_counter()
    res = pusher.push(FORK_REPO, BRANCH, commit2, os.path.join(WORKSPACE_DIR, WORK))
    push_ms = (time.perf_counter() - t0) * 1000
    assert res["success"], res
    assert pusher.upstream_push_counter == 0
    log(f"pushed Commit 2 to fork ({push_ms:.0f}ms), upstream_push_counter=0")

    # 五重一致性对账（PR head 刷新是异步的：加收敛重试，最长 40s）
    pr = commits = tip = None
    for attempt in range(8):
        pr = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{st['upstream']['pr_number']}").json()
        commits = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{st['upstream']['pr_number']}/commits").json()
        tip = a.get(f"/repos/{FORK_REPO}/branches/{BRANCH}").json()["commit"]["sha"]
        if pr["head"]["sha"] == commit2 and tip == commit2:
            break
        log(f"head convergence attempt {attempt + 1}/8: pr_head={pr['head']['sha'][:10]} "
            f"fork_tip={tip[:10]} (waiting for {commit2[:10]})")
        time.sleep(5)
    c1 = (pr["head"]["sha"] == commit2)
    c2 = (pr["base"]["sha"] == st["upstream"]["approved_base_sha"])
    c3 = (len(commits) == 2)
    c4 = (commits[0]["sha"] == st["commit_sha"])  # Commit 1 完整保留
    c5 = (tip == pr["head"]["sha"])
    log(f"five-fold: head==commit2:{c1} base_ok:{c2} commits==2:{c3} "
        f"commit1_preserved:{c4} fork_tip==head:{c5}")
    assert all([c1, c2, c3, c4, c5]), "FIVE-FOLD RECONCILIATION FAILED"
    st["fsm"] = "AWAITING_FINAL_REVIEW"
    save_state(st)
    print(json.dumps({"fsm": "AWAITING_FINAL_REVIEW",
                      "pr_head": pr["head"]["sha"], "pr_base": pr["base"]["sha"],
                      "commits_count": len(commits), "commit1": commits[0]["sha"],
                      "commit2": commits[1]["sha"], "fork_tip": tip,
                      "five_fold": "PASS", "upstream_push_counter": 0,
                      "pr_url": pr.get("html_url")}, indent=2))


def phase_final_listen():
    """终审监听：只读轮询 PR #4 的 merged 状态与 reviews（30s）。合并即记录停机。"""
    st = load_state()
    a = api()
    pr_number = st["upstream"]["pr_number"]
    for i in range(18):
        pr = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{pr_number}").json()
        reviews = a.get(f"/repos/{UPSTREAM_REPO}/pulls/{pr_number}/reviews").json()
        if pr.get("merged"):
            st["merged_at"] = pr.get("merged_at")
            st["fsm"] = "DELIVERED_PR_MERGED"
            save_state(st)
            log(f"MERGED CAPTURED: merged_at={pr.get('merged_at')} fsm=DELIVERED_PR_MERGED")
            print(json.dumps({"fsm": "DELIVERED_PR_MERGED",
                              "merged_at": pr.get("merged_at"),
                              "merge_commit": pr.get("merge_commit_sha")}, indent=2))
            print("[B3G3] PHYSICAL STOP. Awaiting closure directive.")
            return
        approved = [r for r in reviews if r.get("state") == "APPROVED"]
        if approved and i % 4 == 0:
            log(f"poll {i + 1}/18: APPROVED seen ({len(approved)}), not merged yet")
        elif i % 4 == 0:
            log(f"poll {i + 1}/18: not merged yet")
        time.sleep(30)
    log("final listen window exhausted; PR not merged yet. Re-run final_listen.")


if __name__ == "__main__":
    if not UPSTREAM_REPO or not FORK_REPO:
        print("AIE_UPSTREAM_REPO / AIE_FORK_REPO required (parameterized).")
        sys.exit(1)
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    {"discover": phase_discover, "build": phase_build,
     "execute": phase_execute, "listen": phase_listen,
     "gen2_build": phase_gen2_build, "gen2_execute": phase_gen2_execute,
     "final_listen": phase_final_listen}.get(phase, lambda: print(
        "usage: discover | build | execute <sig> | listen | gen2_build | gen2_execute <sig> | final_listen"))()
