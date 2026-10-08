# tools/upwork_mcp_client.py
"""
Phase 1.6 Upwork 官方 MCP 本地客户端（只读握手 + 单次批处理，零 confirm 零轮询）。

认证事实：OAuth 2.1 浏览器登录 + 动态客户端注册 Host 白名单；
无本地凭据时真实链路止步于认证缺口（如实返回，绝不伪造数据）。
"""
import requests

ENDPOINT = "https://mcp.upwork.com/mcp"


class UpworkMCPClient:
    def __init__(self, token: str = None):
        self.token = token  # 若用户完成浏览器 OAuth 后可注入（本环境当前为空）
        self.connects_spent = 0

    def handshake(self) -> dict:
        """认证握手探测（只读）。"""
        if not self.token:
            return {"ok": False, "state": "UNAUTHENTICATED",
                    "reason": "OAuth 2.1 browser login required (no local credential injected)"}
        resp = requests.post(ENDPOINT, json={"jsonrpc": "2.0", "method": "initialize",
                                             "id": 1},
                             headers={"Authorization": f"Bearer {self.token}",
                                      "User-Agent": "Startup-MVP-Scanner"}, timeout=15)
        return {"ok": resp.status_code == 200, "state": "INITIALIZED" if resp.status_code == 200
                else f"HTTP {resp.status_code}", "body": resp.text[:200]}

    def search_jobs(self, query: dict) -> dict:
        """单次定向只读搜索（无凭据时如实返回认证缺口）。"""
        if not self.token:
            return {"ok": False, "state": "UNAUTHENTICATED",
                    "reason": "search_jobs requires completed OAuth browser login"}
        return {"ok": False, "state": "NOT_EXECUTED",
                "reason": "live call requires interactive OAuth session (human gate)"}

    def draft_proposal(self, job: dict) -> dict:
        if not self.token:
            return {"ok": False, "state": "UNAUTHENTICATED"}
        return {"ok": False, "state": "NOT_EXECUTED"}
