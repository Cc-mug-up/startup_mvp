# tools/commercial_pilot.py
"""
Commercial Pilot v0.1 定向机会扫描（只读 / 零留言 / 零申领 / 零远端执行）。

数据源：Algora 公开托管（HTML 通道）+ GitHub bounty 标签搜索。
产出：标准 Opportunity 工件 + 商业审计轨迹 + 转化率统计报告。
"""
import json
import os
import time

from tools.commercial_evaluator import (CommercialEvaluator, competition_signal,
                                        financial_truth, FUNDED_VERIFIED,
                                        FUNDING_UNVERIFIED)
from tools.commercial_trace import CommercialTrace
from tools.github_enricher import GitHubEnricher, load_github_token
from tools.github_scanner import GitHubScanner
from tools.opportunity_sources.algora_source import AlgoraSource
from tools.opportunity_sources.python_org_pool import PYTHON_ORG_POOL

TOKEN = load_github_token()
OUT = "data/commercial_pilot_opportunities.json"
TRACES_OUT = "data/commercial_pilot_traces.json"


def log(msg):
    print(f"[PILOT] {msg}", flush=True)


def scan_algora():
    """Algora 托管源：平台托管背书 -> FUNDED_VERIFIED（escrow）。"""
    source = AlgoraSource(timeout=15)
    items = []
    for entry in PYTHON_ORG_POOL:
        r = source.fetch_bounties(entry["org"])
        log(f"algora org={entry['org']}: ok={r['success']} count={len(r.get('items', []))}")
        if not r["success"]:
            continue
        for raw in r["items"]:
            try:
                opp = source.normalize(raw)
            except Exception:
                continue
            if opp["reward_verified"] and opp["reward_amount"] > 0:
                opp["financial_truth"] = financial_truth(
                    FUNDED_VERIFIED, "algora_escrow", opp["issue_url"] or opp["source_id"],
                    "platform_escrow_api")
                opp["competition_signal"] = competition_signal(
                    claims_count=opp["claim_count"],
                    open_pr_count=opp["open_pr_count"])
                items.append(opp)
    return items


def scan_github():
    """GitHub label:bounty 源：无平台托管背书 -> FUNDING_UNVERIFIED（诚实标注）。"""
    scanner = GitHubScanner(token=TOKEN, timeout=15)
    scan = scanner.scan_bounty_issues(query="label:bounty language:python is:issue is:open",
                                      max_results=30)
    log(f"github scan: success={scan.get('success')} total={scan.get('total_scanned')}")
    items = []
    if not scan.get("success"):
        return items
    for raw in scan.get("_raw_items", []):
        opp = raw
        opp["financial_truth"] = financial_truth(
            FUNDING_UNVERIFIED, "github_label_only",
            opp.get("html_url") or "", "none")
        opp["competition_signal"] = competition_signal(
            claims_count=0, open_pr_count=0, assigned_to=None)
        opp["language"] = "python"
        items.append(opp)
    return items


def main():
    # 一、扫描
    candidates = scan_algora() + scan_github()
    total_scanned = len(candidates)
    log(f"total scanned: {total_scanned}")

    # 二、准入过滤（先富集语言事实——未知语言严禁默认为 Python 放行）
    evaluator = CommercialEvaluator()
    enricher = GitHubEnricher(token=TOKEN, timeout=10)
    qualified, traces = [], []
    for opp in candidates:
        if not (opp.get("language") or "").strip():
            try:
                enriched = enricher.enrich({"repo": opp.get("repo", ""),
                                            "issue_url": opp.get("issue_url", "")})
                opp["language"] = enriched.get("primary_language") or ""
                log(f"enriched language: {opp.get('repo')} -> {opp['language']}")
            except Exception as e:
                opp["language"] = ""
        verdict = evaluator.evaluate({
            "opportunity_id": f"{opp.get('source', 'gh')}_{opp.get('source_id', 'x')}",
            "title": opp.get("title", ""),
            "description": opp.get("body") or opp.get("description") or "",
            "language": opp.get("language") or "",
            "financial_truth": opp["financial_truth"],
            "competition_signal": opp["competition_signal"]
        })
        opp["admission_verdict"] = verdict
        trace = CommercialTrace(verdict["opportunity_id"] if False else
                                f"{opp.get('source','gh')}_{opp.get('source_id','x')}")
        if verdict["pass"]:
            trace.mark("t0_discovered")
            trace.mark("t1_filtered")
            qualified.append(opp)
            traces.append(trace)
    log(f"qualified: {len(qualified)} / {total_scanned}")

    # 三、工件与轨迹落盘（只读扫描的本地数据产物）
    os.makedirs("data", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2, ensure_ascii=False)
    with open(TRACES_OUT, "w", encoding="utf-8") as f:
        json.dump([t.to_dict() for t in traces], f, indent=2, ensure_ascii=False)

    # 四、报告
    print()
    print("==================== [ COMMERCIAL PILOT OPPORTUNITY REPORT ] ====================")
    print(f"scan_total={total_scanned}")
    print(f"qualified={len(qualified)}")
    print(f"conversion_rate={len(qualified) / total_scanned if total_scanned else 0:.2%}")
    print()
    if qualified:
        for opp in qualified:
            ft = opp["financial_truth"]
            cs = opp["competition_signal"]
            print(f"- [{opp.get('title', 'untitled')[:80]}]")
            print(f"  url={ft['evidence_url']}")
            print(f"  funding={ft['funding_status']} source={ft['funding_source']} "
                  f"amount={opp.get('reward_amount')}")
            print(f"  competition: claims={cs['claims_count']} open_prs={cs['open_pr_count']} "
                  f"assigned={cs['assigned_to']}")
            print(f"  language={opp.get('language') or 'unknown'}")
    else:
        print("- 无可入列候选：扫描时点各数据源无可通过 FUNDED_VERIFIED + 零 PR + 纯 Python "
              "准入链的任务（诚实报告，不造假）。")
    print("=================================================================================")
    print("[PILOT] PHYSICAL STOP. Zero external writes. Awaiting human first-target decision.")
    print(f"[PILOT] artifacts: {OUT} / {TRACES_OUT}")


if __name__ == "__main__":
    main()
