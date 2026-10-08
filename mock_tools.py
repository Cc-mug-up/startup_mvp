# mock_tools.py
from typing import Dict, Any

class MockToolDispatcher:
    def __init__(self):
        # 预设外部事件何时在模拟时间里被触发
        # 设定在系统时间推进到该时间戳后，PR 才会变更为 RESOLVED
        self.pr_resolve_after = "2026-10-01T06:00:00+00:00"
        # 由 Controller.tick() 在每次轮询/派发前注入系统当前时间（ISO +00:00 格式）
        self.current_time = None

    def dispatch(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        if tool_name == "discover_opportunities":
            return {
                "success": True,
                "hard_cost": 0.002,  # 抓取 API 成本
                "discovered_opportunities": [
                    {
                        "opportunity_id": "opp_bounty_101",
                        "source": "github_issue",
                        "observed_revenue": 20.0
                    },
                    {
                        "opportunity_id": "opp_scrape_202",
                        "source": "forum_request",
                        "observed_revenue": 35.0
                    }
                ]
            }

        elif tool_name in ["read_file", "write_file", "shell"]:
            # 每次本地工具调用硬成本 $0.0005（CPU/存储摊销）
            return {
                "success": True,
                "output": "Mock file/shell operation completed successfully.",
                "hard_cost": 0.0005
            }

        elif tool_name == "submit_pr":
            return {
                "success": True,
                "output": "PR submitted: https://github.com/mock/repo/pull/42",
                "hard_cost": 0.005,
                "pending_event": {
                    "event_type": "PR_REVIEW",
                    "opportunity_id": "opp_bounty_101",
                    "external_reference": "github_pr_42",
                    "timeout_timestamp": "2026-10-01T12:00:00+00:00"
                }
            }

        return {"success": False, "error": f"Unknown tool: {tool_name}", "hard_cost": 0.0}

    def check_pending_event(self, pending: Dict[str, Any]) -> Dict[str, Any]:
        # 依靠 tick() 注入的当前时间进行门控：未到 pr_resolve_after 前保持 PENDING
        ext_ref = pending.get("external_reference")
        if ext_ref == "github_pr_42":
            cur = self.current_time or "1970-01-01T00:00:00+00:00"
            if cur >= self.pr_resolve_after:  # 同格式 ISO 字符串可字典序比较
                return {
                    "status": "RESOLVED",
                    "verified_revenue": 20.0
                }
        return {"status": "PENDING"}
