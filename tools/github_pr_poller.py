# tools/github_pr_poller.py
"""
Gate 4.2-C 现实世界事件适配器：纯数据事实提取，零网络 I/O。
Poller 仅充当"现实事实传感器"，严禁包含任何与金额、预算、收入相关的逻辑。
"""


class GitHubPRPoller:
    @staticmethod
    def parse_poll_response(mock_api_payload: dict) -> dict:
        """
        纯数据解析函数，仅提取客观事实：
        - merged == True 且 state == "closed" -> {"event_type": "PR_MERGED", "external_reference": ..., "timestamp": ...}
        - merged == False 且 state == "closed" -> {"event_type": "PR_REJECTED", "external_reference": ..., "timestamp": ...}
        - state == "open" -> {"event_type": "PR_PENDING", "external_reference": ..., "timestamp": ...}
        严禁在 Poller 内编写任何与金额、预算、收入相关的逻辑。
        """
        if not isinstance(mock_api_payload, dict):
            return {"event_type": "PR_PENDING", "external_reference": None, "timestamp": None}

        merged = bool(mock_api_payload.get("merged"))
        state = mock_api_payload.get("state")

        if merged and state == "closed":
            event_type = "PR_MERGED"
        elif state == "closed":
            event_type = "PR_REJECTED"
        else:
            event_type = "PR_PENDING"

        return {
            "event_type": event_type,
            "external_reference": mock_api_payload.get("external_reference"),
            "timestamp": mock_api_payload.get("timestamp")
        }
