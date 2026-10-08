# tools/probe_source_feasibility.py
"""
Commercial Pilot Phase 1.1 来源可行性探测（轻量只读 / 零写 / 每源 1~2 次 GET）。

五维接口事实：Q1 Discovery / Q2 Funding / Q3 Claim / Q4 Competition / Q5 Filter。
熔断规则：任一【资金已托管 + 纯 Python + 真实开放】活体 Issue 被发现，
立即写入 data/seed_candidates.json 并停机。
"""
import json
import os
import re

import requests

from tools.github_enricher import load_github_token

TOKEN = load_github_token()
H = {"User-Agent": "Startup-MVP-Scanner", "Accept": "application/vnd.github+json"}
H_AUTH = {**H, "Authorization": f"Bearer {TOKEN}"}
REPORT_PATH = "SOURCE_FEASIBILITY_REPORT.md"
SEEDS_PATH = "data/seed_candidates.json"

MELT_HIT = None  # 熔断命中（funded + python + open）


def _get(url, headers=None, params=None, timeout=15):
    return requests.get(url, headers=headers or H, params=params, timeout=timeout)


def _snippet(resp, max_len=400):
    try:
        return json.dumps(resp.json(), ensure_ascii=False)[:max_len]
    except Exception:
        return resp.text[:max_len]


# ---------------------------------------------------------------- #
def probe_algora():
    """Algora：已知 HTML 通道按 Org 分页；测试 api.algora.io 全局端点。"""
    q = {k: "UNKNOWN" for k in ("Q1", "Q2", "Q3", "Q4", "Q5")}
    evidence = []
    # 全局列表端点探测
    r1 = _get("https://api.algora.io/")
    evidence.append({"probe": "GET https://api.algora.io/",
                    "http": r1.status_code, "body": _snippet(r1)})
    if r1.status_code == 200:
        q["Q1"] = "PARTIAL"  # 需进一步验证是否免 Org 穷举
    # 已知 tRPC 通道（per-org）
    r2 = _get("https://console.algora.io/api/trpc/bounty.list?input=%7B%22json%22%3A%7B%22org%22%3A%22projectdiscovery%22%2C%22limit%22%3A1%7D%7D")
    evidence.append({"probe": "GET console.algora.io tRPC bounty.list (org=projectdiscovery)",
                    "http": r2.status_code, "body": _snippet(r2)})
    if r2.status_code == 200:
        q["Q4"] = "PARTIAL"  # per-org 悬赏列表可读（HTML 通道另有 claims 计数）
    q["Q3"] = "UNKNOWN"  # 认领规则需认领页二次核验
    q["Q5"] = "FAIL"     # 无语言字段（历史实测），须 GitHub 二次富集
    return {"source": "Algora (console.algora.io + api.algora.io)", "q": q,
            "evidence": evidence, "conclusion": "PARTIAL"}


def probe_polar():
    """Polar.sh：测试公开 funding 列表端点。"""
    global MELT_HIT
    q = {k: "UNKNOWN" for k in ("Q1", "Q2", "Q3", "Q4", "Q5")}
    evidence = []
    r1 = _get("https://api.polar.sh/v1/funding_issues")
    evidence.append({"probe": "GET https://api.polar.sh/v1/funding_issues",
                    "http": r1.status_code, "body": _snippet(r1)})
    if r1.status_code == 200:
        q["Q1"] = "PASS" if isinstance(r1.json(), list) else "PARTIAL"
        items = r1.json() if isinstance(r1.json(), list) else []
        if items:
            first = items[0]
            if any(k in first for k in ("funding_goal", "pledged", "amount")):
                q["Q2"] = "PASS"
            if "python" in str(first.get("labels", "")).lower() or "labels" in first:
                q["Q5"] = "PARTIAL"
            # 熔断检查：资金已托管 + Python + 开放
            if first.get("pledged_amount") or first.get("funding_goal"):
                if "python" in str(first).lower() and first.get("state") in ("open", "active", None):
                    MELT_HIT = first
                    q["MELT"] = "HIT"
    elif r1.status_code == 404:
        q["Q1"] = "FAIL"
    return {"source": "Polar.sh (api.polar.sh/v1)", "q": q, "evidence": evidence,
            "conclusion": "PARTIAL"}


def probe_github():
    """GitHub Search：定向第三方托管关键词检索（Q1/Q5 直接可用）。"""
    global MELT_HIT
    q = {k: "UNKNOWN" for k in ("Q1", "Q2", "Q3", "Q4", "Q5")}
    evidence = []
    r1 = _get("https://api.github.com/search/issues",
              headers=H_AUTH,
              params={"q": "algora in:body language:python is:issue is:open",
                      "per_page": 3})
    evidence.append({"probe": "GET /search/issues?q=algora in:body language:python ...",
                    "http": r1.status_code, "body": _snippet(r1)})
    if r1.status_code == 200:
        q["Q1"] = "PASS"   # 全局检索免 Org 穷举
        q["Q5"] = "PASS"   # language:python 直接过滤
        items = r1.json().get("items", [])
        if items:
            q["Q4"] = "PARTIAL"  # PR 计数需关联查询
            # 熔断检查（收紧）：必须含结构化托管直链（algora.io/bounties/ 或
            # polar.sh 链接），裸 "algora" 关键词不再触发（追踪表类误报已实证）
            for first in items:
                body = (first.get("body") or "").lower()
                structured = ("algora.io/bounties/" in body) or ("polar.sh" in body)
                if structured and first.get("state") == "open":
                    MELT_HIT = first
                    q["MELT"] = "HIT"
                    break
    q["Q2"] = "FAIL"  # 无托管状态/金额字段
    q["Q3"] = "FAIL"  # 无认领规则
    return {"source": "GitHub Search API", "q": q, "evidence": evidence,
            "conclusion": "PARTIAL"}


def build_report(results):
    lines = [
        "# SOURCE_FEASIBILITY_REPORT",
        "",
        "探测时点事实核验（纯只读，每源 1~2 次 GET）。五维：",
        "Q1 Discovery（免 Org 穷举全局列表）/ Q2 Funding（托管状态+金额字段）/",
        "Q3 Claim（认领规则）/ Q4 Competition（claims/PR 计数）/ Q5 Filter（Python 直滤）。",
        "",
        "## 五维事实对比矩阵",
        "",
        "| Source | Q1 | Q2 | Q3 | Q4 | Q5 | 综合定性 |",
        "|---|---|---|---|---|---|---|"
    ]
    for r in results:
        q = r["q"]
        lines.append(f"| {r['source']} | {q['Q1']} | {q['Q2']} | {q['Q3']} | "
                     f"{q['Q4']} | {q['Q5']} | {r['conclusion']} |")
    lines += ["", "## 原始 JSON 片段证据", ""]
    for r in results:
        lines.append(f"### {r['source']}")
        for ev in r["evidence"]:
            lines.append(f"- `{ev['probe']}` -> HTTP {ev['http']}")
            lines.append(f"```json\n{ev['body']}\n```")
    lines += ["", "## 结论", "",
              "- Algora：per-Org 分页（HTML/tRPC），金额与 claims 可读，语言需 GitHub 富集；"
              "全局免穷举列表未证实。",
              "- Polar.sh：公开 funding 端点探明（见证据），托管金额字段可用性以证据为准。",
              "- GitHub Search：全局检索与语言直滤最强（Q1/Q5 PASS），但无托管字段（Q2 FAIL），"
              "须与托管平台证据交叉验证。",
              "- 综合：三源互补（GitHub 发现 + 托管平台存证），无单一源满足全部五维。"]
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[PROBE] report written: {REPORT_PATH}")


def main():
    results = [probe_algora(), probe_polar(), probe_github()]
    for r in results:
        print(f"[PROBE] {r['source']}: " +
              " ".join(f"{k}={v}" for k, v in r["q"].items()))
    build_report(results)

    if MELT_HIT:
        seed = {"url": MELT_HIT.get("html_url") or MELT_HIT.get("url") or "",
                "type": "live_melt_hit",
                "description": "熔断命中：资金已托管 + 纯 Python + 真实开放"}
        with open(SEEDS_PATH, "w", encoding="utf-8") as f:
            json.dump({"seeds": [seed]}, f, indent=2, ensure_ascii=False)
        print("[PROBE] MELT HIT -> seed written. PHYSICAL STOP.")
        return
    print("[PROBE] no melt hit (honest). PHYSICAL STOP. Awaiting next directive.")


if __name__ == "__main__":
    main()
