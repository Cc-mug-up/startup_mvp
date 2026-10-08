# tools/opportunity_filter.py
"""
Gate 4.4-B/4.4-C 粗筛过滤器与特征抽取器：纯内存客观规则剪枝。
4.4-C 裁决变更：
- 范围分析模块化：接入 tools/scope_analyzer.ScopeAnalyzer（词组/语境边界）。
- 异常枚举解耦：INVALID_SCHEMA（缺必要结构）/ MALFORMED_INPUT（字段类型错乱/脏数据），
  严禁归因为业务类 INACTIVE / SCOPE_UNCLEAR。
- 语言拦截收敛：仅显式非 Python 核心语言或底层编译扩展标签
  （rust-core / c-extension / ffi / binding）触发 UNSUPPORTED_LANGUAGE；
  纯正文提及或上层调用不触发。
- 安全长度上限：超长文本截断解析，严禁被拖死。
"""
import re
from datetime import datetime, timezone
from tools.scope_analyzer import ScopeAnalyzer

MAX_TEXT_LEN = 2000  # 安全长度上限

# 丢弃原因枚举全量规范（测试覆盖率以此为准）
VALID_DISCARD_REASONS = [
    "UNDERPRICED", "INACTIVE", "SCOPE_BLACKLISTED",
    "SCOPE_UNCLEAR", "UNSUPPORTED_LANGUAGE",
    "INVALID_SCHEMA", "MALFORMED_INPUT", "LANGUAGE_UNKNOWN"
]


class OpportunityFilter:
    DEFAULT_CONFIG = {
        "min_revenue": 50.0,
        "max_issue_age_days": 30,
        "allowed_languages": ["python"],
        "low_level_extension_labels": ["rust-core", "c-extension", "ffi", "binding"]
    }

    @staticmethod
    def _parse_iso(ts):
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @classmethod
    def _anomaly_result(cls, raw_opp, reason: str) -> dict:
        """结构/类型异常的系统级归因结果：单一原因，严禁混入业务归因。"""
        if isinstance(raw_opp, dict):
            opp_id = raw_opp.get("opportunity_id", "")
            revenue = float(raw_opp.get("observed_revenue") or 0.0)
        else:
            opp_id = ""
            revenue = 0.0
        return {
            "opportunity_id": opp_id,
            "status": "DISCARDED",
            "hard_filter": {
                "revenue_pass": False, "active_pass": False,
                "scope_pass": False, "stack_pass": False
            },
            "discard_reasons": [reason],
            "features": {
                "observed_revenue": revenue,
                "days_since_update": -1,
                "language": "unknown",
                "scope_tag": "none"
            },
            "raw_opportunity": raw_opp
        }

    @classmethod
    def evaluate(cls, raw_opp: dict, config: dict = None, current_time_iso: str = None) -> dict:
        """
        纯内存评估函数。输入单个标准 opportunity 字典，输出带特征分析的评估结果。
        """
        cfg = {**cls.DEFAULT_CONFIG, **(config or {})}

        # 0) 异常输入守恒：结构与类型校验先行（系统级归因，短路返回）
        if not isinstance(raw_opp, dict):
            return cls._anomaly_result(raw_opp, "MALFORMED_INPUT")
        metadata = raw_opp.get("metadata")
        if metadata is None:
            return cls._anomaly_result(raw_opp, "INVALID_SCHEMA")
        if not isinstance(metadata, dict):
            return cls._anomaly_result(raw_opp, "MALFORMED_INPUT")
        labels = metadata.get("labels")
        if labels is not None and not isinstance(labels, list):
            return cls._anomaly_result(raw_opp, "MALFORMED_INPUT")
        updated_at = metadata.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            return cls._anomaly_result(raw_opp, "MALFORMED_INPUT")
        title = metadata.get("title")
        if title is not None and not isinstance(title, str):
            return cls._anomaly_result(raw_opp, "MALFORMED_INPUT")

        opp_id = raw_opp.get("opportunity_id", "")
        title_s = (title or "")[:MAX_TEXT_LEN]
        label_names = [str(l.get("name", "")) if isinstance(l, dict) else str(l) for l in (labels or [])]
        description = str(raw_opp.get("description") or "")[:MAX_TEXT_LEN]

        # 1. revenue_pass
        revenue = float(raw_opp.get("observed_revenue") or 0.0)
        revenue_pass = revenue >= cfg["min_revenue"]

        # 2. active_pass
        state = metadata.get("state")
        cur = cls._parse_iso(current_time_iso) if current_time_iso else datetime.now(timezone.utc)
        upd = cls._parse_iso(updated_at)
        age_days = None
        age_ok = False
        if upd is not None and cur is not None:
            age_days = (cur - upd).days
            age_ok = 0 <= age_days <= cfg["max_issue_age_days"]
        repo_flag = metadata.get("repo_active_within_90d")
        active_pass = (state == "open") and age_ok and (repo_flag is not False)

        # 3. scope_pass：独立范围分析器（词组/语境边界，正文藏雷检测）
        scope = ScopeAnalyzer.analyze(title_s, labels, description)
        scope_pass = scope["scope_pass"]
        scope_reason = scope["reason"]
        scope_tag = scope["scope_tag"]

        # 4. stack_pass：语言三态判定（SUPPORTED / UNSUPPORTED / UNKNOWN）
        #    + 底层编译扩展标签硬拦截。
        #    缺少语言元数据归因 LANGUAGE_UNKNOWN，严禁直接判死为 UNSUPPORTED_LANGUAGE。
        language = metadata.get("language")
        if not language:
            for name in label_names:
                if name.lower() in cfg["allowed_languages"]:
                    language = name.lower()
                    break
        if language:
            language = str(language).lower()
            language_status = "SUPPORTED" if language in cfg["allowed_languages"] else "UNSUPPORTED"
        else:
            language = "unknown"
            language_status = "UNKNOWN"
        ext_hit = any(ext in name.lower()
                      for ext in cfg["low_level_extension_labels"]
                      for name in label_names)
        stack_pass = (language_status == "SUPPORTED") and (not ext_hit)

        discard_reasons = []
        if not revenue_pass:
            discard_reasons.append("UNDERPRICED")
        if not active_pass:
            discard_reasons.append("INACTIVE")
        if not scope_pass:
            discard_reasons.append(scope_reason)
        if language_status == "UNKNOWN":
            discard_reasons.append("LANGUAGE_UNKNOWN")
        elif not stack_pass:
            discard_reasons.append("UNSUPPORTED_LANGUAGE")

        all_passed = revenue_pass and active_pass and scope_pass and stack_pass

        return {
            "opportunity_id": opp_id,
            "status": "CANDIDATE" if all_passed else "DISCARDED",
            "hard_filter": {
                "revenue_pass": revenue_pass,
                "active_pass": active_pass,
                "scope_pass": scope_pass,
                "stack_pass": stack_pass
            },
            "discard_reasons": discard_reasons,
            "features": {
                "observed_revenue": revenue,
                "days_since_update": age_days if age_days is not None else -1,
                "language": language,
                "language_status": language_status,
                "scope_tag": scope_tag
            },
            "raw_opportunity": raw_opp
        }

    @classmethod
    def filter_candidates(cls, opportunities: list, config: dict = None, current_time_iso: str = None) -> list:
        """批量处理入口，仅返回 status == 'CANDIDATE' 的列表"""
        return [ev for ev in (cls.evaluate(o, config, current_time_iso) for o in opportunities)
                if ev["status"] == "CANDIDATE"]
