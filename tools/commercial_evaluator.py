# tools/commercial_evaluator.py
"""
Commercial Pilot 两层漏斗架构（发现池 / 准入关）。

三态判定（废除单一 Reject 逻辑）：
  - QUALIFIED：全部证据链闭合；
  - REJECTED：确定拒绝（积极事实证伪：无托管背书、确认错误技术栈、已有 PR 竞争、
              范围过大）；
  - UNVERIFIED：线索留存（证据缺失而非证伪：escrow 证据缺失、技术栈未验证、
                范围无法判定、竞争态势未验证、权限未确认）。
missing_evidence 数组：escrow_proof_missing / stack_unverified / scope_too_large /
competition_high / permission_unconfirmed。

Opportunity Funnel 聚合器：
  Discovered -> Evidence_Checked -> Qualified -> Claimed -> Merged -> Paid
  流失归因字典（REJECT_NO_ESCROW / REJECT_WRONG_STACK / REJECT_COMPETITION /
  REJECT_SCOPE_TOO_LARGE 等）。
"""
FUNDED_VERIFIED = "FUNDED_VERIFIED"
FUNDING_UNVERIFIED = "FUNDING_UNVERIFIED"
FUNDING_UNKNOWN = "FUNDING_UNKNOWN"
VALID_VERIFICATION_METHODS = ("platform_escrow_api", "maintainer_confirm")

FINANCIAL_TRUTH_CONTRACT = ["funding_status", "funding_source", "evidence_url", "verification_method"]
COMPETITION_SIGNAL_CONTRACT = ["claims_count", "open_pr_count", "assigned_to"]

MISSING_EVIDENCE_FIELDS = [
    "escrow_proof_missing", "stack_unverified", "scope_too_large",
    "competition_high", "permission_unconfirmed", "maintainer_dormant"
]

MAINTAINER_SIGNAL_CONTRACT = ["last_commit_days", "recent_issue_activity_days", "is_repo_archived"]
SETTLEMENT_EVIDENCE_CONTRACT = ["settlement_ref", "amount_usd", "settled_at", "proof_url"]


FUNDING_EVIDENCE_CONTRACT = ["source", "evidence_url", "observed_at", "raw_reference"]


def funding_evidence(source: str, evidence_url: str, observed_at: str,
                     raw_reference: str) -> dict:
    """资金存证实体：第三方托管凭证四字段完整固化。"""
    d = {
        "source": source,
        "evidence_url": evidence_url,
        "observed_at": observed_at,
        "raw_reference": raw_reference
    }
    assert all(k in d and d[k] for k in FUNDING_EVIDENCE_CONTRACT), \
        "funding_evidence contract violated"
    return d


def settlement_evidence(settlement_ref: str, amount_usd: float, settled_at: str,
                        proof_url: str) -> dict:
    """结算证据契约：四字段齐备、非空，金额为正。"""
    d = {
        "settlement_ref": settlement_ref,
        "amount_usd": float(amount_usd),
        "settled_at": settled_at,
        "proof_url": proof_url
    }
    assert all(d[k] for k in SETTLEMENT_EVIDENCE_CONTRACT), "settlement_evidence contract violated"
    assert d["amount_usd"] > 0, "settlement amount must be positive"
    return d

SCOPE_BLOCKED_MARKERS = [
    "gpu", "cuda", "api key", "secret", "credential", "production db", "production database",
    "kubernetes", "terraform", "aws account", "staging environment"
]


def financial_truth(funding_status: str, funding_source: str, evidence_url: str,
                    verification_method: str) -> dict:
    d = {
        "funding_status": funding_status,
        "funding_source": funding_source,
        "evidence_url": evidence_url,
        "verification_method": verification_method
    }
    assert all(k in d and d[k] for k in FINANCIAL_TRUTH_CONTRACT), "FinancialTruth contract violated"
    return d


def competition_signal(claims_count: int = 0, open_pr_count: int = 0, assigned_to=None,
                       relevant_open_pr_count: int = 0) -> dict:
    """
    竞争信号（弹性解耦）：
      open_pr_count：提及该 Issue 的任意 PR 数；
      relevant_open_pr_count：明确尝试修复该 Issue 且仍活跃的 PR 数
      （仅后者构成高竞争硬风险，前者只作线索留存）。
    """
    return {
        "claims_count": int(claims_count),
        "open_pr_count": int(open_pr_count),
        "assigned_to": assigned_to,
        "relevant_open_pr_count": int(relevant_open_pr_count)
    }


class CommercialEvaluator:
    def __init__(self, allowed_languages=("python",)):
        self.allowed_languages = set(allowed_languages)

    def evaluate(self, opportunity: dict) -> dict:
        """
        两层漏斗准入关：输出三态 + missing_evidence + 流失归因。
        """
        ft = opportunity.get("financial_truth") or {}
        cs = opportunity.get("competition_signal") or {}
        missing = []
        attribution = []
        assignment_required = cs.get("assigned_to") is not None

        # 1. 资金：积极证伪 -> REJECTED；证据未知 -> UNVERIFIED 留存
        funding = ft.get("funding_status")
        if funding == FUNDING_UNVERIFIED:
            attribution.append("REJECT_NO_ESCROW")
        elif funding == FUNDING_UNKNOWN:
            missing.append("escrow_proof_missing")

        # 2. 技术栈：确认非 Python -> REJECTED；未验证 -> UNVERIFIED 留存
        language = str(opportunity.get("language") or "").strip().lower()
        if language and language not in self.allowed_languages:
            attribution.append("REJECT_WRONG_STACK")
        elif not language:
            missing.append("stack_unverified")

        # 3. 竞争（弹性解耦）：仅 relevant_open_pr_count（明确修复该 Issue 的活跃 PR）
        #    构成硬风险；提及性 PR 只作线索留存，不再机械拦截
        relevant_pr = int(cs.get("relevant_open_pr_count") or 0)
        open_pr = int(cs.get("open_pr_count") or 0)
        if relevant_pr > 0:
            attribution.append("REJECT_COMPETITION")
        elif open_pr > 0:
            missing.append("competition_high")
        elif cs.get("claims_count") is None:
            missing.append("competition_high")

        # 4. 范围：命中基础设施标记 -> REJECTED；范围无法判定 -> UNVERIFIED 留存
        blob = " ".join([str(opportunity.get("title") or ""),
                         str(opportunity.get("description") or "")]).lower()
        if any(m in blob for m in SCOPE_BLOCKED_MARKERS):
            attribution.append("REJECT_SCOPE_TOO_LARGE")

        # 5. 权限：已指派但权限未确认 -> UNVERIFIED 留存
        if assignment_required:
            missing.append("permission_unconfirmed")

        # 6. 维护者活跃度硬规则：last_commit_days <= 30 且未归档，
        #    否则 missing: maintainer_dormant -> UNVERIFIED 降级
        ms = opportunity.get("maintainer_signal") or {}
        last_commit = ms.get("last_commit_days")
        archived = ms.get("is_repo_archived")
        if last_commit is None or archived is None:
            missing.append("maintainer_dormant")
        elif last_commit > 30 or archived is True:
            missing.append("maintainer_dormant")

        if attribution:
            state = "REJECTED"
        elif missing:
            state = "UNVERIFIED"
        else:
            state = "QUALIFIED"

        return {
            "state": state,
            "missing_evidence": missing,
            "attribution": attribution,
            "assignment_required": assignment_required,
            "assigned_to": cs.get("assigned_to"),
            "evidence_chain": {
                "evidence_url": ft.get("evidence_url"),
                "verification_method": ft.get("verification_method"),
                "funding_source": ft.get("funding_source")
            }
        }


class OpportunityFunnel:
    """发现池漏斗聚合器：六阶段计数 + 流失归因 + 缺失证据统计。"""
    STAGES = ["discovered", "evidence_checked", "qualified", "claimed", "merged", "paid"]

    def __init__(self):
        self.counts = {s: 0 for s in self.STAGES}
        self.loss_attribution = {}
        self.missing_evidence_stats = {f: 0 for f in MISSING_EVIDENCE_FIELDS}

    def record_discovered(self):
        self.counts["discovered"] += 1

    def record_verdict(self, verdict: dict):
        """准入关裁决入漏斗。"""
        self.counts["evidence_checked"] += 1
        if verdict["state"] == "QUALIFIED":
            self.counts["qualified"] += 1
        else:
            for a in verdict.get("attribution", []):
                self.loss_attribution[a] = self.loss_attribution.get(a, 0) + 1
            for m in verdict.get("missing_evidence", []):
                if m in self.missing_evidence_stats:
                    self.missing_evidence_stats[m] += 1

    def advance(self, stage: str):
        assert stage in ("claimed", "merged", "paid"), stage
        self.counts[stage] += 1

    def conversion_rates(self) -> dict:
        d = self.counts["discovered"]
        return {
            "evidence_check_rate": self.counts["evidence_checked"] / d if d else 0.0,
            "qualify_rate": self.counts["qualified"] / d if d else 0.0,
            "claim_rate": self.counts["claimed"] / d if d else 0.0,
            "merge_rate": self.counts["merged"] / d if d else 0.0,
            "paid_rate": self.counts["paid"] / d if d else 0.0
        }

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "loss_attribution": self.loss_attribution,
            "missing_evidence_stats": self.missing_evidence_stats,
            "conversion_rates": self.conversion_rates()
        }
