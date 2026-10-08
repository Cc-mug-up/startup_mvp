# tools/b2_rehearsal_driver.py
"""
Gate 4.5-B2 真实环境受控交付驱动（Issue 发现 -> 沙箱修补 -> 审批卡 -> 物理停机）。

执行纪律：
- 审批前零外部写：本驱动在打印审批卡后【立即物理停机】，
  严禁调用 git push / create_pr 或任何写接口；
- 基线初始化（受权）：仅当目标仓库缺少基线文件时，按用户授权
  向 Cc-mug-up/aie-b2-sandbox 初始化 3 个基线文件与 1 个待修复 Issue；
- Token 仅宿主机内存使用（.env chmod 600），绝不进入沙箱或挂载目录
  （公开仓库 clone 走匿名 URL，无 Token 落盘）。
"""
import base64
import json
import os
import sys

import requests

from controller import Controller
from sandbox_manager import start_sandbox, exec_in_sandbox, WORKSPACE_DIR
from tools.approval_gate import ApprovalGate
from tools.github_adapter import GitHubAdapter
from tools.github_enricher import load_github_token
from tools.internal_tools import InternalTools
from tools.kill_switch import GlobalKillSwitch
from tools.opportunity_filter import OpportunityFilter

TOKEN = load_github_token()
TARGET_REPO = os.environ.get("TARGET_REPO") or "Cc-mug-up/aie-b2-sandbox"
BRANCH = "agent/fix-issue-1"
API = "https://api.github.com"
H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "Startup-MVP-Scanner",
     "Accept": "application/vnd.github+json"}

CALC_BUGGY = "def add(a, b):\n    return a - b\n"
TEST_CALC = "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
SMOKE_OK = "def test_env_sane():\n    assert 1 + 1 == 2\n"

PATCH = ("--- a/calculator.py\n"
         "+++ b/calculator.py\n"
         "@@ -1,2 +1,2 @@\n"
         " def add(a, b):\n"
         "-    return a - b\n"
         "+    return a + b\n")

ISSUE_TITLE = "Fix add function returns wrong result"
ISSUE_BODY = "The add function returns 2+3=-1.\n\n/bounty $50\n\nPlease fix it and add a passing test."


def log(msg):
    print(f"[B2] {msg}", flush=True)


def api_get(path):
    return requests.get(f"{API}{path}", headers=H, timeout=20)


def api_put(path, payload):
    return requests.put(f"{API}{path}", headers=H, json=payload, timeout=20)


def api_post(path, payload):
    return requests.post(f"{API}{path}", headers=H, json=payload, timeout=20)


def main():
    # 0. 断路器就绪性确认（审批前必须处于未激活状态，且写接口随时可被物理阻断）
    if GlobalKillSwitch.is_blocked():
        log("KILL SWITCH ENGAGED -> ABORT")
        sys.exit(1)
    log(f"kill switch disengaged; target={TARGET_REPO}")

    # 1. 基线初始化（受权；缺失才写）
    contents = api_get(f"/repos/{TARGET_REPO}/contents/").json()
    names = [x["name"] for x in contents] if isinstance(contents, list) else []
    baseline_files = {"calculator.py": CALC_BUGGY, "test_calculator.py": TEST_CALC, "test_baseline.py": SMOKE_OK}
    for fname, content in baseline_files.items():
        if fname in names:
            log(f"baseline exists: {fname}")
            continue
        r = api_put(f"/repos/{TARGET_REPO}/contents/{fname}", {
            "message": f"init baseline {fname}", "branch": "main",
            "content": base64.b64encode(content.encode()).decode()})
        log(f"baseline init {fname}: HTTP {r.status_code}")
        assert r.status_code in (200, 201), r.text[:300]

    issues = api_get(f"/repos/{TARGET_REPO}/issues?state=open").json()
    if not issues:
        r = api_post(f"/repos/{TARGET_REPO}/issues", {"title": ISSUE_TITLE, "body": ISSUE_BODY})
        log(f"issue init: HTTP {r.status_code}")
        assert r.status_code == 201, r.text[:300]
        # 直接使用 POST 创建响应的实体（避免 GitHub 列表接口的最终一致性延迟）
        issues = [r.json()]

    # 2. Discovery & Enrichment（只读）
    # 按管线自身规则选择：结构化赏金提取 >= 50 的首个开放 Issue（不硬编码编号）
    issue, opp = None, None
    for cand in issues:
        c = GitHubAdapter.parse_issue(cand)
        if c.get("success") and c["discovered_opportunities"][0]["observed_revenue"] >= 50.0:
            issue, opp = cand, c["discovered_opportunities"][0]
            break
    assert issue is not None, f"no issue with structured bounty >= $50 among {len(issues)} open issues"
    # 经济信号标记：本测试任务的赏金为人工合成信号，严禁混入 MARKET 数据
    opp["reward_type"] = "SYNTHETIC"
    log(f"discovered: {opp['opportunity_id']} reward={opp['observed_revenue']} "
        f"reward_type={opp['reward_type']}")

    from tools.github_enricher import GitHubEnricher
    obs = {"repo": TARGET_REPO, "issue_url": issue["html_url"], "title": issue["title"],
           "body": issue["body"] or ""}
    enriched = GitHubEnricher(token=TOKEN).enrich(obs)
    language = enriched.get("primary_language")
    if not language:
        # 结构性事实兜底：仓库内容以 .py 为主时识别为 Python（待 GitHub 语言探测更新）
        if any(n.endswith(".py") for n in names):
            language = "Python"
    log(f"enriched: language={language} repo_active_within_90d={enriched.get('repo_active_within_90d')}")

    verdict = OpportunityFilter.evaluate({
        "opportunity_id": opp["opportunity_id"],
        "observed_revenue": opp["observed_revenue"],
        "description": issue["body"] or "",
        "metadata": {"title": issue["title"], "state": "open",
                     "updated_at": issue.get("updated_at") or "2026-10-01T00:00:00Z",
                     "language": language, "labels": issue.get("labels") or [],
                     "repo_active_within_90d": True}
    }, current_time_iso="2026-10-04T00:00:00Z")
    log(f"filter: {verdict['status']} reasons={verdict['discard_reasons']}")
    assert verdict["status"] == "CANDIDATE", verdict

    # 3. 沙箱受控执行（UID 1000 / --memory-swap=512m 容器）
    from sandbox_manager import stop_sandbox
    stop_sandbox()  # 清理同名残留容器，避免与并发测试的容器名竞态
    start_sandbox()
    os.chown(WORKSPACE_DIR, 1000, 1000)  # 挂载根归属沙箱用户
    work = "aie_b2_work"
    clone_ok = False
    for attempt in range(3):
        code, out, err = exec_in_sandbox(
            f"cd /workspace && rm -rf {work} && git clone -q --depth 1 https://github.com/{TARGET_REPO} {work} "
            f"&& cd {work} && git rev-parse HEAD",
            timeout=120)
        if code == 0:
            clone_ok = True
            break
        print(f"[B2] clone attempt {attempt + 1} failed: {(err or out)[:200]}", flush=True)
    assert clone_ok, "clone failed after retries"
    base_sha = out.strip().splitlines()[-1]
    log(f"cloned; base_sha={base_sha}")

    # 基线真实失败（exit != 0）；清 __pycache__ + 禁写字节码（DEBT-001 强制项）
    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && rm -rf __pycache__ && "
        f"PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q")
    log(f"baseline pytest exit={code} (must be non-zero)")
    assert code != 0, "baseline unexpectedly green"

    # 4. 修补（模型产物为确定性脚本补丁；沙箱 pytest 是唯一裁判）
    wr = InternalTools.write_file(f"{work}/.agent_patch.diff", PATCH)
    assert wr["success"], wr
    os.chown(os.path.join(WORKSPACE_DIR, work, ".agent_patch.diff"), 1000, 1000)

    code, out, err = exec_in_sandbox(
        f"cd /workspace/{work} && git config user.email 'agent@aie.local' "
        f"&& git config user.name 'AIE Agent' "
        f"&& git checkout -q -b {BRANCH} "
        f"&& git apply .agent_patch.diff "
        f"&& git apply --numstat .agent_patch.diff "
        f"&& rm .agent_patch.diff && rm -rf __pycache__ "
        f"&& printf '__pycache__/\\n*.pyc\\n' > .gitignore "  # DEBT-001 强制项
        f"&& PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q "
        f"&& git add -A && git commit -qm 'Fix add function (aie-b2)' && git rev-parse HEAD")
    assert code == 0, f"fix pipeline failed (exit {code}): {err or out}"
    lines = [l for l in out.strip().splitlines() if l.strip()]
    numstat_line = next((l for l in lines if "\t" in l and l.split("\t")[0].strip().isdigit()), None)
    adds, dels = (numstat_line.split("\t")[0], numstat_line.split("\t")[1]) if numstat_line else ("?", "?")
    commit_sha = lines[-1]
    log(f"fix committed: commit_sha={commit_sha} numstat=+{adds}/-{dels}")

    # Scope Lock：单文件且 <= 10 行
    scope = ApprovalGate.check_scope([{"path": "calculator.py", "added": int(adds), "deleted": int(dels)}])
    log(f"scope check: {scope}")
    assert scope["ok"], scope

    # 5. 不可变工件冻结（Anti-TOCTOU）与审批卡
    code, out, err = exec_in_sandbox(f"cd /workspace/{work} && git diff HEAD^ HEAD")
    canonical_diff = "\n".join(l.rstrip() for l in out.replace("\r\n", "\n").split("\n"))
    gate = ApprovalGate()
    artifact = gate.freeze_artifact(
        task_id=f"issue_{issue['number']}",
        source_issue=issue["html_url"],
        target_repo=TARGET_REPO, target_branch=BRANCH,
        base_sha=base_sha, commit_sha=commit_sha,
        diff_content=canonical_diff, model_id="b2-scripted-fix",
        token_cost=0.0, test_exit_code=0,
        tokens=0, test_summary="real sandbox pytest exit 0")

    # 控制器：WAITING (PENDING_HUMAN_APPROVAL)
    if not os.path.exists("b2_state.json"):
        with open("b2_state.json", "w", encoding="utf-8") as f:
            json.dump({
                "financials": {"initial_capital": 100.0, "current_cash": 100.0,
                               "total_revenue": 0.0, "total_hard_cost": 0.0,
                               "cost_breakdown": {"llm_decision": 0.0, "tool_execution": 0.0,
                                                  "infrastructure": 0.0}},
                "runtime_guards": {"system_status": "ACTIVE",
                                   "current_time": "2026-10-01T00:00:00Z",
                                   "consecutive_internal_steps": 0, "consecutive_invalid_steps": 0,
                                   "max_internal_steps": 10, "max_invalid_steps": 3,
                                   "pending_external_event": None},
                "opportunity_memory": {}, "transaction_ledger": [], "settled_event_refs": []
            }, f, indent=2)
    ctrl = Controller(state_path="b2_state.json")
    ctrl.state.setdefault("opportunity_memory", {})[f"issue_{issue['number']}"] = {
        "source": "github_issue", "observed_revenue": 50.0, "reward_type": "SYNTHETIC",
        "accumulated_cost": 0.0, "internal_steps_spent": 1, "external_interactions": 0,
        "status": "ACTIVE"}
    ctrl.enter_approval_wait(f"issue_{issue['number']}", artifact)
    log(f"controller: {ctrl.state['runtime_guards']['system_status']} pending="
        f"{ctrl.state['runtime_guards']['pending_external_event']['event_type']}")

    # 6. 打印审批卡 -> 物理停机
    print()
    print(gate.render_approval_card(artifact))
    print()
    print("[B2] ================== PHYSICAL STOP ==================")
    print("[B2] NO git push, NO create_pr issued. Awaiting human signature.")
    print(f"[B2] artifact_hash={artifact['artifact_hash']}")
    print(f"[B2] base_sha={base_sha} commit_sha={commit_sha} branch={BRANCH}")
    print("[B2] token_cost=0.0 (scripted fix; no LLM tokens consumed this run)")
    print(json.dumps({"status": "AWAITING_HUMAN_APPROVAL",
                      "artifact": {k: artifact[k] for k in
                                   ("task_id", "target_repo", "target_branch", "base_sha",
                                    "commit_sha", "diff_hash", "artifact_hash", "test_exit_code",
                                    "token_cost")}}, indent=2))


if __name__ == "__main__":
    main()
