# tools/scope_analyzer.py
"""
Gate 4.4-C 独立范围分析器：从 title / labels / description 提取范围信号。

词组/语境边界设计：
- 黑名单短语（规范化空白后子串匹配）：仅命中明确宏大需求，如 "rewrite whole"。
  单独的 "rewrite" 不是黑名单——"rewrite typo" 属于小修补，绝不被误杀。
- 黑名单词（\\b 词边界匹配）：高语义负载词（performance / refactor / architecture /
  migration / redesign）几乎必然指代大规模变更。
- 白名单（子串匹配，与 Gate 4.4-B 语义完全一致）：scope_tag 优先级即列表顺序。
"""
import re


class ScopeAnalyzer:
    BLACKLIST_PHRASES = [
        "rewrite whole", "rewrite the whole", "rewrite the entire",
        "complete rewrite", "full rewrite",
        "architecture refactor", "refactor the architecture",
        "database migration", "migrate the database",
        "performance optimization", "system redesign",
        "major refactor", "large refactor"
    ]
    BLACKLIST_WORDS = ["performance", "refactor", "architecture", "migration", "redesign"]
    WHITELIST_WORDS = ["bug", "fix", "doc", "test", "patch", "error", "traceback"]

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower()).strip()

    @classmethod
    def _label_names(cls, labels) -> list:
        names = []
        for lbl in (labels or []):
            if isinstance(lbl, dict):
                names.append(cls._norm(str(lbl.get("name") or "")))
            else:
                names.append(cls._norm(str(lbl)))
        return names

    @classmethod
    def _whitelist_tag(cls, blob_tag: str):
        for w in cls.WHITELIST_WORDS:
            if w in blob_tag:  # 子串匹配：与 4.4-B 白名单语义一致（"document" 命中 "doc"）
                return w
        return None

    @classmethod
    def analyze(cls, title: str, labels, description: str) -> dict:
        """
        输出:
        {"scope_pass": bool,
         "reason": "SCOPE_BLACKLISTED" | "SCOPE_UNCLEAR" | None,
         "scope_tag": str}
        黑名单扫描范围：title + labels + description（正文藏雷检测）。
        白名单扫描范围：title + labels。
        """
        title_n = cls._norm(title)
        label_names = cls._label_names(labels)
        desc_n = cls._norm(description)

        blob_all = " ".join([title_n] + label_names + [desc_n])
        blob_tag = " ".join([title_n] + label_names)

        phrase_hit = next((p for p in cls.BLACKLIST_PHRASES if p in blob_all), None)
        word_hit = next((w for w in cls.BLACKLIST_WORDS
                         if re.search(rf"\b{re.escape(w)}\b", blob_all)), None)
        if phrase_hit or word_hit:
            return {
                "scope_pass": False,
                "reason": "SCOPE_BLACKLISTED",
                "scope_tag": cls._whitelist_tag(blob_tag) or "none"
            }

        tag = cls._whitelist_tag(blob_tag)
        if tag:
            return {"scope_pass": True, "reason": None, "scope_tag": tag}
        return {"scope_pass": False, "reason": "SCOPE_UNCLEAR", "scope_tag": "none"}
