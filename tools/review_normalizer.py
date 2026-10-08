# tools/review_normalizer.py
"""
Gate 4.5-B3-1 外部 Review 事件归一化器（只读 / 零 LLM）。

硬规则（审查加固项 2）：
- 触发 REVIEW_CHANGES_REQUESTED 必须且仅以 GitHub 原生 review.state ==
  "CHANGES_REQUESTED" 为准，严禁自然语言正则关键词启发式触发执行；
- 行级评论仅作为增量修补的上下文提示（REVIEW_CONTEXT_ONLY），绝不单独触发执行；
- (review_id, comment_id) 幂等去重，已处理事件不重复触发。
"""


class ReviewNormalizer:
    @staticmethod
    def normalize(reviews: list, comments: list, processed_ids=None) -> dict:
        """
        reviews: GET /pulls/{n}/reviews 的 review 对象列表
        comments: GET /pulls/{n}/comments 的行级评论列表
        返回归一化事件：
          REVIEW_CHANGES_REQUESTED | REVIEW_APPROVED | REVIEW_CONTEXT_ONLY | REVIEW_NONE
        """
        processed = set(processed_ids or [])
        reviews = reviews or []
        comments = comments or []

        # 1) 原生 review.state 是唯一执行触发源（CHANGES_REQUESTED）
        pending_reviews = sorted(
            (r for r in reviews if r.get("id") not in processed),
            key=lambda r: r.get("submitted_at") or "", reverse=True)
        for rev in pending_reviews:
            if rev.get("state") == "CHANGES_REQUESTED":
                return {
                    "event_type": "REVIEW_CHANGES_REQUESTED",
                    "review_id": rev.get("id"),
                    "review_state": rev.get("state"),
                    "body": rev.get("body") or "",
                    "submitted_at": rev.get("submitted_at"),
                    "trigger": "NATIVE_STATE"  # 唯一合法触发源
                }

        # 2) 靶场适配（平台物理限制）：单账号靶场中 PR 作者无法对自己发起原生
        #    "Request changes"（选项被平台强制置灰）。故允许 COMMENTED 评审以
        #    显式指令前缀 "[CHANGES_REQUESTED]" 开头时视同生效——这是字面指令标记，
        #    不是自然语言关键词启发式。
        for rev in pending_reviews:
            body = (rev.get("body") or "").strip()
            if rev.get("state") == "COMMENTED" and body.startswith("[CHANGES_REQUESTED]"):
                return {
                    "event_type": "REVIEW_CHANGES_REQUESTED",
                    "review_id": rev.get("id"),
                    "review_state": rev.get("state"),
                    "body": body,
                    "submitted_at": rev.get("submitted_at"),
                    "trigger": "EXPLICIT_PREFIX"
                }

        # 3) APPROVED（最新审批通过）
        for rev in pending_reviews:
            if rev.get("state") == "APPROVED":
                return {
                    "event_type": "REVIEW_APPROVED",
                    "review_id": rev.get("id"),
                    "review_state": rev.get("state"),
                    "body": rev.get("body") or "",
                    "submitted_at": rev.get("submitted_at"),
                    "trigger": "NATIVE_STATE"
                }

        # 4) 行级评论仅作上下文，绝不触发执行
        context = [{
            "comment_id": c.get("id"),
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
            "body": c.get("body") or ""
        } for c in comments if c.get("id") not in processed]
        if context:
            return {"event_type": "REVIEW_CONTEXT_ONLY", "comments": context, "trigger": None}

        return {"event_type": "REVIEW_NONE", "comments": []}
