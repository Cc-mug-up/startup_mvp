# tools/payment_adapter.py
"""
Gate 4.3-A 经济结算适配器：外部支付网关 Webhook 的纯数据事实提取。
严禁计算与推测，仅做字段校验与标准化输出，零网络 I/O。
"""


class PaymentAdapter:
    @staticmethod
    def parse_payment_webhook(payload: dict) -> dict:
        """
        纯数据解析函数，严禁计算与推测。
        输入：模拟的外部支付网关 Webhook 字典
        校验：
          - 必须包含 payment_provider (如 "stripe")
          - 必须包含 payment_ref (如 "pay_tx_12345")
          - 必须包含 opportunity_id (如 "opp_gh_xxx")
          - 必须包含 amount (float, 且 > 0)
          - status 必须为 "confirmed"
        输出标准化事件元组：
        {
            "event_type": "PAYMENT_CONFIRMED",
            "payment_ref": str,
            "opportunity_id": str,
            "amount": float,
            "timestamp": str
        }
        """
        if not isinstance(payload, dict):
            return {"event_type": "PAYMENT_INVALID", "error": "Payload is not a dict"}

        for field in ("payment_provider", "payment_ref", "opportunity_id", "amount"):
            if payload.get(field) in (None, ""):
                return {"event_type": "PAYMENT_INVALID", "error": f"Missing field: {field}"}

        if payload.get("status") != "confirmed":
            return {"event_type": "PAYMENT_INVALID", "error": "Payment status not confirmed"}

        amount = payload.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or float(amount) <= 0:
            return {"event_type": "PAYMENT_INVALID", "error": "Invalid amount: must be positive number"}

        return {
            "event_type": "PAYMENT_CONFIRMED",
            "payment_ref": str(payload["payment_ref"]),
            "opportunity_id": str(payload["opportunity_id"]),
            "amount": float(amount),
            "timestamp": payload.get("timestamp")
        }
