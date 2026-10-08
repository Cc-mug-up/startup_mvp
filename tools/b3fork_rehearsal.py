# tools/b3fork_rehearsal.py
"""
Gate 4.5-B3-2 跨库 Fork 演练 CLI（参数化：AIE_UPSTREAM_REPO / AIE_FORK_REPO）。

阶段：
  ensure    创建/确认 fork -> FORK_ENSURED
  lockbase  锁定 upstream main HEAD 为 approved_base_sha -> UPSTREAM_SYNCED
  issue     初始化上游基线（若缺）+ 创建 Issue #1
  sandbox   沙箱受控修补（origin=fork / upstream 只读 no_push://）-> SANDBOX_VERIFIED
  freeze    冻结跨库拓扑工件 -> 打印审批卡 -> 物理停机（零网络写）
  execute <sig>   签名后：LOCK-1/2 -> push fork -> PUSHED_TO_FORK
  pr        open_pr：跨库 PR + 四重 SHA -> PR_OPEN
  reconcile 终态对账
"""
import base64
import json
import os
import sys
import time

from sandbox_manager import exec_in_sandbox, start_sandbox, stop_sandbox, WORKSPACE_DIR
from tools.approval_gate import ApprovalGate, canonical, sha256_hex
from tools.b3_fork_driver import ForkFlow, ForkPusher, GitHubAPI
from tools.github_enricher import load_github_token
from tools.internal_tools import InternalTools

UPSTREAM_REPO = os.environ.get("AIE_UPSTREAM_REPO", "")
FORK_REPO = os.environ.get("AIE_FORK_REPO", "")
TOKEN = load_github_token()
STATE_FILE = "b3fork_state.json"
BRANCH = "agent/fix-issue-1"

# 与上游真实预置内容对齐：add(a: int, b: int) 无边界校验
CALC_BUGGY = "def add(a: int, b: int) -> int:\n    return a + b\n"
TEST_CALC = ("from calculator import add\n\nimport pytest\n\n\n"
             "def test_add_rejects_non_numeric():\n"
             "    with pytest.raises(TypeError):\n"
             "        add(\"2\", 3)\n")
SMOKE = "def test_env_sane():\n    assert 1 + 1 == 2\n"

PATCH = ("--- a/calculator.py\n"
         "+++ b/calculator.py\n"
         "@@ -1,2 +1,4 @@\n"
         " def add(a: int, b: int) -> int:\n"
         "+    if not isinstance(a, int) or not isinstance(b, int):\n"
         "+        raise TypeError(\"add requires integer arguments\")\n"
         "     return a + b\n")


def log(msg):
    print(f"[B3F] {msg}", flush=True)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def sandbox_exec(cmd, timeout=180):
    return exec_in_sandbox(cmd, timeout=timeout)


def phase_ensure():
    api = GitHubAPI(TOKEN)
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=api, sandbox_exec=sandbox_exec)
    st = load_state() or flow.state
    flow.state = st
    if st["fsm"] in ("NEW",):
        flow.ensure_fork()
        save_state(flow.state)
        log(f"fsm={flow.state['fsm']} fork_source_sha={flow.state['fork']['source_sha']}")
    # fork 异步就绪轮询
    for i in range(24):
        r = api.get(f"/repos/{FORK_REPO}")
        if r.status_code == 200:
            break
        time.sleep(5)
    assert r.status_code == 200, f"fork not ready: {r.status_code}"
    log(f"fork ready: {FORK_REPO} (fsm={flow.state['fsm']})")


def phase_lockbase():
    api = GitHubAPI(TOKEN)
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=api, sandbox_exec=sandbox_exec)
    st = load_state() or flow.state
    flow.state = st
    # 手动 fork 场景：确认 fork 存在 -> FORK_ENSURED
    r = api.get(f"/repos/{FORK_REPO}")
    assert r.status_code == 200, f"fork not found: {r.status_code}"
    if st["fsm"] in ("NEW",):
        st["fsm"] = "FORK_ENSURED"
        st["fork"]["created_at"] = st["fork"].get("created_at") or \
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = api.get(f"/repos/{UPSTREAM_REPO}/branches/main")
    head = r.json()["commit"]["sha"]
    flow.state["upstream"]["approved_base_sha"] = head
    flow.state["fork"]["source_sha"] = flow.state["fork"].get("source_sha") or head
    if flow.state["fsm"] == "FORK_ENSURED":
        flow._transition("UPSTREAM_SYNCED")
    save_state(flow.state)
    log(f"approved_base_sha={head} fsm={flow.state['fsm']}")


def phase_issue():
    api = GitHubAPI(TOKEN)
    r = api.get(f"/repos/{UPSTREAM_REPO}/contents/")
    names = [x["name"] for x in r.json()] if r.status_code == 200 else []
    for fname, content in (("calculator.py", CALC_BUGGY), ("test_calculator.py", TEST_CALC),
                           ("test_baseline.py", SMOKE)):
        if fname in names:
            log(f"baseline exists: {fname}")
            continue
        resp = api.put(f"/repos/{UPSTREAM_REPO}/contents/{fname}",
                       {"message": f"init baseline {fname}", "branch": "main",
                        "content": base64.b64encode(content.encode()).decode()})
        log(f"baseline init {fname}: HTTP {resp.status_code}")
        if resp.status_code not in (200, 201):
            log(f"baseline init failed: {resp.text[:200]}")
    r = api.get(f"/repos/{UPSTREAM_REPO}/issues?state=all")
    issues = [i for i in r.json() if i.get("title", "").startswith("Add boundary validation")]
    if issues:
        log(f"issue exists: #{issues[0]['number']}")
        return
    resp = api.post(f"/repos/{UPSTREAM_REPO}/issues",
                    {"title": "Add boundary validation and tests",
                     "body": "`divide` raises ZeroDivisionError on zero divisor; it should raise "
                             "ValueError instead.\n\n/bounty $50\n"})
    assert resp.status_code == 201, resp.text[:200]
    log(f"issue created: #{resp.json()['number']}")


def phase_sandbox():
    stop_sandbox()
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=GitHubAPI(TOKEN),
                    sandbox_exec=sandbox_exec)
    flow.state = load_state()
    work = "aie_fork_work"
    for attempt in range(3):
        code, out, err = exec_in_sandbox(
            f"cd /workspace && rm -rf {work} && git clone -q --depth 1 https://github.com/{FORK_REPO} {work} "
            f"&& cd {work} && git remote add upstream https://github.com/{UPSTREAM_REPO} "
            f"&& git remote set-url --push upstream no_push://upstream_disabled "
            f"&& git fetch -q upstream main && git rev-parse upstream/main", timeout=180)
        if code == 0:
            break
        log(f"clone attempt {attempt + 1} failed: {(err or out)[:150]}")
        time.sleep(5)
    assert code == 0, "clone failed"
    upstream_head = out.strip().splitlines()[-1]
    assert upstream_head == flow.state["upstream"]["approved_base_sha"], \
        f"LOCK-1 VIOLATED: upstream moved {upstream_head} != approved"
    log(f"LOCK-1 OK: upstream main == approved base {upstream_head[:12]}...")

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
        f"&& git add -A && git commit -qm 'Fork fix (aie-b3-2)' && git rev-parse HEAD", timeout=180)
    assert code == 0, f"sandbox verify failed: {err or out[:400]}"
    commit = out.strip().splitlines()[-1]
    flow.state["commit_sha"] = commit
    flow.state["fork"]["branch"] = BRANCH
    flow._transition("SANDBOX_VERIFIED")
    save_state(flow.state)
    log(f"sandbox verified: commit={commit} fsm={flow.state['fsm']}")


def phase_freeze():
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=GitHubAPI(TOKEN),
                    sandbox_exec=sandbox_exec)
    flow.state = load_state()
    work = "aie_fork_work"
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git diff HEAD^ HEAD", timeout=60)
    assert code == 0, err or out
    diff = canonical(out)
    st, artifact = flow.freeze_card("issue_1",
                                    f"https://github.com/{UPSTREAM_REPO}/issues/1",
                                    diff, model_id="b3fork-scripted-r1")
    save_state(flow.state)
    print()
    print("==================== [ HUMAN APPROVAL CARD (B3-2 跨库专用) ] ====================")
    print(f"TASK ID:        {artifact['task_id']}")
    print(f"SOURCE:         {artifact['source_issue']}")
    print(f"UPSTREAM REPO:  {artifact['upstream_repo']}  (READ-ONLY)")
    print(f"FORK REPO:      {artifact['fork_repo']}  (WRITE-ONLY)")
    print(f"APPROVED BASE:  {artifact['approved_base_sha']}")
    print(f"TARGET BRANCH:  {artifact['branch']}")
    print(f"COMMIT SHA:     {artifact['commit_sha']}")
    print(f"DIFF HASH:      {artifact['diff_hash']}")
    print(f"ARTIFACT HASH:  {artifact['artifact_hash']}")
    files_desc = ", ".join(
        "{} +{}/-{}".format(f["path"], f["added"], f["deleted"])
        for f in artifact["changed_files"])
    print(f"CHANGED FILES:  {files_desc}")
    print(f"TEST RESULT:    EXIT 0 (sandbox pytest)")
    print("EXTERNAL ACTIONS REQUESTED:")
    print(f"  [X] GIT PUSH to FORK {artifact['fork_repo']}:{artifact['branch']}")
    print(f"  [X] CREATE PR against {artifact['upstream_repo']}:main (head={FORK_REPO.split('/')[0]}:{artifact['branch']})")
    print("================================================================================")
    print()
    print("[B3F] PHYSICAL STOP. Zero external writes. Awaiting human signature.")
    print(f"[B3F] artifact_hash={artifact['artifact_hash']}")


def phase_execute():
    signed = sys.argv[2] if len(sys.argv) > 2 else ""
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=GitHubAPI(TOKEN),
                    sandbox_exec=sandbox_exec)
    flow.state = load_state()
    work = "aie_fork_work"
    code, out, err = exec_in_sandbox(f"cd /workspace/{work} && git diff HEAD^ HEAD", timeout=60)
    assert code == 0, err or out
    diff = canonical(out)
    t0 = time.perf_counter()
    flow.execute_push(signed, diff, os.path.join(WORKSPACE_DIR, work))
    push_ms = (time.perf_counter() - t0) * 1000
    save_state(flow.state)
    log(f"pushed to fork ({push_ms:.0f}ms): fsm={flow.state['fsm']} "
        f"upstream_push_counter={flow.pusher.upstream_push_counter}")
    assert flow.pusher.upstream_push_counter == 0, "UPSTREAM PUSH ATTEMPTED!"


def phase_pr():
    flow = ForkFlow(UPSTREAM_REPO, FORK_REPO, TOKEN, api=GitHubAPI(TOKEN),
                    sandbox_exec=sandbox_exec)
    flow.state = load_state()
    st, pr = flow.open_pr("[AIE-B3-2] Add boundary validation (Issue #1)",
                          "Fixes #1: add() now raises TypeError on non-integer arguments.\n\n"
                          "Verified in sandbox: pytest exit 0.",
                          "issue_1")
    save_state(flow.state)
    v = flow.verify_quad_sha(pr["number"])
    assert v["triple_sha"] is True, v
    log(f"fsm={flow.state['fsm']} pr_url={v['pr_url']}")
    print()
    print("| 项目 | 值 |")
    print("|---|---|")
    print(f"| SHA-A (Approved) | {v['approved_base'] and flow.state['commit_sha']} |")
    print(f"| SHA-B (Fork Tip) | {v['fork_tip']} |")
    print(f"| SHA-C (PR Head) | {v['pr_head']} |")
    print(f"| LOCK-1 (upstream main) | {v['main_head']} |")
    print(f"| LOCK-3 (PR.base.sha) | {v['pr_base']} |")
    print(f"| Triple-SHA | {'PASS' if v['triple_sha'] else 'FAIL'} |")
    print(f"| PR URL | {v['pr_url']} |")
    print(f"| upstream_push_counter | {flow.pusher.upstream_push_counter} |")
    print(f"| FSM | {flow.state['fsm']} |")


if __name__ == "__main__":
    if not UPSTREAM_REPO or not FORK_REPO:
        print("AIE_UPSTREAM_REPO / AIE_FORK_REPO required (parameterized).")
        sys.exit(1)
    phase = sys.argv[1] if len(sys.argv) > 1 else "help"
    {"ensure": phase_ensure, "lockbase": phase_lockbase, "issue": phase_issue,
     "sandbox": phase_sandbox, "freeze": phase_freeze,
     "execute": phase_execute, "pr": phase_pr}.get(phase, lambda: print(
        "usage: ensure | lockbase | issue | sandbox | freeze | execute <sig> | pr"))()
