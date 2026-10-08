# mock_agent.py
from typing import Dict, Any, Tuple

class ScriptedMockAgent:
    def __init__(self):
        self.step = 0

    def get_action(self, state: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        self.step += 1
        cost_per_decision = 0.001  # 每次决策假定消耗 $0.001 Token 费

        # Step 1: 发现机会
        if self.step == 1:
            return {
                "opportunity_id": "opp_global_discovery",
                "action_type": "INTERNAL",
                "tool_name": "discover_opportunities",
                "tool_args": {},
                "justification": "扫描市场公开机会"
            }, cost_per_decision

        # Step 2-4: 针对 opp_bounty_101 执行 3 次开发
        if self.step in [2, 3, 4]:
            return {
                "opportunity_id": "opp_bounty_101",
                "action_type": "INTERNAL",
                "tool_name": "write_file",
                "tool_args": {"path": "patch.py"},
                "justification": f"开发修复代码 step {self.step-1}"
            }, cost_per_decision

        # Step 5: 提交 PR 挂载外部事件 (转入 WAITING)
        if self.step == 5:
            return {
                "opportunity_id": "opp_bounty_101",
                "action_type": "EXTERNAL",
                "tool_name": "submit_pr",
                "tool_args": {"repo": "mock/repo"},
                "justification": "提交代码并发起悬赏评审"
            }, cost_per_decision

        # Step 6-15: 切换到 opp_scrape_202，硬磨 10 次内部工作
        if 6 <= self.step <= 15:
            return {
                "opportunity_id": "opp_scrape_202",
                "action_type": "INTERNAL",
                "tool_name": "shell",
                "tool_args": {"cmd": "python test_parser.py"},
                "justification": f"本地测试爬虫解析 step {self.step-5}"
            }, cost_per_decision

        # Step 16: 试探第 11 步内部工作（预期必须被控制器拦截！）
        if self.step == 16:
            return {
                "opportunity_id": "opp_scrape_202",
                "action_type": "INTERNAL",
                "tool_name": "read_file",
                "tool_args": {"path": "log.txt"},
                "justification": "试图绕过 10 步限制继续拖延"
            }, cost_per_decision

        # Step 17: 被拦截后，执行 ABANDON
        if self.step == 17:
            return {
                "opportunity_id": "opp_scrape_202",
                "action_type": "ABANDON",
                "tool_name": "none",
                "tool_args": {},
                "justification": "内部研发成本过高，放弃该机会"
            }, cost_per_decision

        # 默认：等待
        return {
            "opportunity_id": "opp_bounty_101",
            "action_type": "WAIT",
            "tool_name": "none",
            "tool_args": {},
            "justification": "无新动作，等待状态推进"
        }, cost_per_decision
