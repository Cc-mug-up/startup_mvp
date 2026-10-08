# tools/phase1_algora_scan.py
"""
Commercial Pilot Phase 1 补充：Algora 真实 Python 活体任务自动检索。

过滤硬条件：FUNDED_VERIFIED（平台托管）+ 富集语言 Python + open_prs == 0。
命中第一个活体任务 -> 写入 data/seed_candidates.json -> 自动重跑 Phase 1 核验。
零外部写（不留言/不 claim/不推 PR）。
"""
import json
import os
import time

from tools.github_enricher import GitHubEnricher, load_github_token
from tools.opportunity_sources.algora_source import AlgoraSource
from tools.phase1_verifier import Phase1Verifier

# Python 生态组织扩扫清单（Algora 公开悬赏页）
PYTHON_ORGS = [
    "mindsdb", "pymc-labs", "chaoss", "litellm", "berriai", "langchain-ai",
    "pydantic", "fastapi", "encode", "pallets", "django", "psf", "streamlit",
    "gradio-app", "huggingface", "sqlalchemy", "pytest-dev", "jupyter",
    "astral-sh", "projectdiscovery"  # 对照组
]


def log(msg):
    print(f"[ALG] {msg}", flush=True)


def main():
    source = AlgoraSource(timeout=15)
    enricher = GitHubEnricher(token=load_github_token(), timeout=10)

    hit = None
    for org in PYTHON_ORGS:
        r = source.fetch_bounties(org)
        count = len(r.get("items", []))
        log(f"org={org}: items={count}")
        if not r["success"] or not r["items"]:
            continue
        for raw in r["items"]:
            try:
                opp = source.normalize(raw)
            except Exception:
                continue
            if not (opp["reward_verified"] and opp["reward_amount"] > 0):
                continue
            # 富集语言（未知语言不得默认为 Python）
            lang = ""
            if opp.get("repo"):
                try:
                    lang = enricher.enrich({"repo": opp["repo"],
                                            "issue_url": opp.get("issue_url", "")}
                                           ).get("primary_language") or ""
                except Exception:
                    lang = ""
            log(f"  candidate: {opp['repo']}#{opp['issue_id']} ${opp['reward_amount']} "
                f"lang={lang} claims={opp['claim_count']} open_prs={opp['open_pr_count']}")
            if (opp["reward_verified"] and opp["reward_amount"] >= 50
                    and (lang or "").lower() == "python"
                    and opp["open_pr_count"] == 0):
                hit = {"url": opp["issue_url"], "reward": opp["reward_amount"],
                       "repo": opp["repo"], "issue_id": opp["issue_id"],
                       "title": opp["title"], "source": "algora_scan"}
                break
        if hit:
            break

    if not hit:
        log("NO QUALIFYING LIVE BOUNTY FOUND (honest zero).")
        return

    # 写入单种子文件（Phase 1 N=1）
    seed = {"url": hit["url"], "type": "algora_live_python",
            "expected_outcome": "PASS", "description": f"Algora 活体 {hit['repo']}#{hit['issue_id']}"}
    with open("data/seed_candidates.json", "w", encoding="utf-8") as f:
        json.dump({"seeds": [seed]}, f, indent=2, ensure_ascii=False)
    log(f"seed written: {hit['url']}")

    # 自动重跑 Phase 1 核验
    report = Phase1Verifier().run(seed)
    v = report["verdict"]
    f = report["facts"]
    print()
    if v["state"] == "QUALIFIED":
        print("==================== [ REAL BOUNTY READY FOR HUMAN GATE 1 ] ====================")
        print(f"title: {f['title']}")
        print(f"issue: {hit['url']}")
        print(f"escrow_amount_usd: {hit['reward']}")
        print(f"funding_evidence: {json.dumps(f.get('funding_evidence'), ensure_ascii=False)}")
        print(f"maintainer: last_commit_days={f.get('last_commit_days')} archived={f.get('archived')}")
        print(f"scope_estimate: {f.get('estimated_scope_files')} files")
        print(f"competition: open_prs={f.get('open_pr_count')} relevant={f.get('relevant_open_pr_count')}")
        print("================================================================================")
        print("[ALG] PHYSICAL STOP. Awaiting Human Gate 1 confirmation.")
    else:
        print("VERDICT:", json.dumps(v, ensure_ascii=False))
        print("facts:", json.dumps({k: f.get(k) for k in
               ("repo", "issue_number", "language", "archived", "last_commit_days",
                "escrow_marker", "stack_pure_python", "open_pr_count",
                "relevant_open_pr_count")}, ensure_ascii=False))
        print("[ALG] PHYSICAL STOP. Candidate not qualified (evidence gap).")


if __name__ == "__main__":
    main()
