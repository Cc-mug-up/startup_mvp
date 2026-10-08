# tools/model_adapter.py
"""
Gate 4.5-A1 统一模型调用抽象层：包装模型调用契约，强制产出遥测数据。

核心纪律：claimed_success 是模型自述（不可信事实），
真实成功与否只能由沙箱内 pytest 的 exit_code 判定。
"""
from typing import Any


class ModelAdapter:
    # 遥测计价常量（模拟阶段定价）
    PRICE_PER_1K_INPUT = 0.001
    PRICE_PER_1K_OUTPUT = 0.002

    def __init__(self, model_id: str = "mock-llm", client: Any = None):
        self.model_id = model_id
        self.client = client  # 注入桩；生产阶段接真实 LLM SDK
        self.telemetry = []   # 每次调用的遥测记录

    def generate(self, prompt: str, **kwargs) -> dict:
        """
        包装模型调用契约，强制产出遥测数据：
        {"model_id", "input_tokens", "output_tokens", "cost", "output_text", "claimed_success"}
        """
        if self.client is None:
            raise RuntimeError("No LLM client configured")
        resp = self.client.generate(prompt, **kwargs)
        input_tokens = int(resp.get("input_tokens") or 0)
        output_tokens = int(resp.get("output_tokens") or 0)
        cost = (input_tokens / 1000.0) * self.PRICE_PER_1K_INPUT + \
               (output_tokens / 1000.0) * self.PRICE_PER_1K_OUTPUT
        entry = {
            "model_id": self.model_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": cost,
            "output_text": str(resp.get("output_text") or ""),
            "claimed_success": bool(resp.get("claimed_success", False))
        }
        self.telemetry.append(entry)
        return entry

    @property
    def total_tokens(self) -> int:
        return sum(t["input_tokens"] + t["output_tokens"] for t in self.telemetry)

    @property
    def total_cost(self) -> float:
        return sum(t["cost"] for t in self.telemetry)
