# tools/diagnose_algora.py
# Gate 4.5-B0.1 诊断脚本（排查真实 tRPC 请求格式，排查后归档，不参与运行链）
# 只读诊断：绝不向 Algora 发起任何写/认领/评论操作。
import json
import sys
import urllib.parse
import requests

BASE = "https://console.algora.io/api/trpc/bounty.list"
UA = {"User-Agent": "Mozilla/5.0 (Startup-MVP calibration diagnostic)"}


def probe(input_payload: dict, timeout: int = 15):
    url = f"{BASE}?input=" + urllib.parse.quote(json.dumps({"json": input_payload}))
    try:
        resp = requests.get(url, headers=UA, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"http": None, "error": str(e)[:80]}
    if resp.status_code != 200:
        return {"http": resp.status_code, "body": resp.text[:80]}
    try:
        payload = resp.json()
        items = payload[0]["result"]["data"]["json"].get("items", [])
        return {"http": 200, "count": len(items), "sample_keys": list(items[0].keys())[:12] if items else []}
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"http": 200, "parse_error": str(e)[:80], "body": resp.text[:120]}


def main():
    org = sys.argv[1] if len(sys.argv) > 1 else "projectdiscovery"

    # 1. 参数字段名探测
    print(f"=== Field-name probes for org={org} ===")
    for field in ["org", "orgSlug", "slug", "organization", "org_name", "handle", "orgId"]:
        r = probe({field: org, "limit": 50})
        print(f"  input[{field!r}] -> {r}")

    # 2. status 过滤语义探测（配合正确字段名逐一尝试）
    print("=== Status semantics ===")
    for status in [None, "active", "open", "all"]:
        p = {"org": org, "limit": 50}
        if status:
            p["status"] = status
        r = probe(p)
        print(f"  status={status!r} -> {r}")

    # 3. 公开页面取证：官网 bounties 页面的数据线索
    print("=== Public page evidence ===")
    page_url = f"https://console.algora.io/org/{org}/bounties"
    try:
        resp = requests.get(page_url, headers=UA, timeout=15)
        html = resp.text
        print(f"  page http={resp.status_code} len={len(html)}")
        for hint in ["nuclei", "data-bounty", "bounty.list", "projectdiscovery", "__NEXT_DATA__"]:
            idx = html.find(hint)
            if idx >= 0:
                print(f"  hint {hint!r} at {idx}: {html[max(0, idx - 60):idx + 120]!r}")
            else:
                print(f"  hint {hint!r}: NOT FOUND")
    except requests.exceptions.RequestException as e:
        print(f"  page fetch error: {e}")


if __name__ == "__main__":
    main()
