# tests/test_commercial_pilot.py
# Commercial Pilot 两层漏斗与种子注入单元测试（纯本地 / 零网络）
import json

import pytest

from tools.commercial_evaluator import (CommercialEvaluator, OpportunityFunnel,
                                        competition_signal, financial_truth,
                                        FINANCIAL_TRUTH_CONTRACT, COMPETITION_SIGNAL_CONTRACT,
                                        FUNDED_VERIFIED, FUNDING_UNVERIFIED, FUNDING_UNKNOWN)
from tools.commercial_trace import CommercialTrace, STAGES
from tools.seed_injector import SeedInjector


def _opportunity(funding=FUNDED_VERIFIED, open_pr=0, assigned=None,
                 language="python", title="Fix a small bug", method="platform_escrow_api",
                 claims=None):
    return {
        "opportunity_id": "opp_pilot_1",
        "title": title,
        "description": "A small python bug fix.",
        "language": language,
        "financial_truth": financial_truth(
            funding, "algora_escrow", "https://algora.io/bounties/x", method),
        "competition_signal": competition_signal(
            claims_count=3 if claims is None else claims,
            open_pr_count=open_pr, assigned_to=assigned),
        "maintainer_signal": {"last_commit_days": 3, "recent_issue_activity_days": 1,
                              "is_repo_archived": False}
    }


# ---------- 契约 ----------
def test_financial_truth_contract_complete():
    ft = financial_truth(FUNDED_VERIFIED, "algora_escrow", "https://x", "platform_escrow_api")
    assert all(k in ft and ft[k] for k in FINANCIAL_TRUTH_CONTRACT)


def test_financial_truth_rejects_incomplete():
    with pytest.raises(AssertionError):
        financial_truth("", "algora_escrow", "https://x", "platform_escrow_api")


def test_competition_signal_decoupled():
    cs = competition_signal(claims_count=5, open_pr_count=1, assigned_to="alice")
    assert cs == {"claims_count": 5, "open_pr_count": 1, "assigned_to": "alice"}


# ---------- 三态判定 ----------
def test_evaluator_qualified_full_chain():
    r = CommercialEvaluator().evaluate(_opportunity())
    assert r["state"] == "QUALIFIED"
    assert r["missing_evidence"] == [] and r["attribution"] == []


def test_evaluator_rejected_no_escrow():
    r = CommercialEvaluator().evaluate(_opportunity(funding=FUNDING_UNVERIFIED))
    assert r["state"] == "REJECTED"
    assert "REJECT_NO_ESCROW" in r["attribution"]


def test_evaluator_rejected_wrong_stack():
    r = CommercialEvaluator().evaluate(_opportunity(language="rust"))
    assert r["state"] == "REJECTED"
    assert "REJECT_WRONG_STACK" in r["attribution"]


def test_evaluator_rejected_competition():
    r = CommercialEvaluator().evaluate(_opportunity(open_pr=1))
    assert r["state"] == "REJECTED"
    assert "REJECT_COMPETITION" in r["attribution"]


def test_evaluator_rejected_scope():
    r = CommercialEvaluator().evaluate(_opportunity(title="Fix GPU kernel crash"))
    assert r["state"] == "REJECTED"
    assert "REJECT_SCOPE_TOO_LARGE" in r["attribution"]


def test_evaluator_unverified_escrow_missing():
    r = CommercialEvaluator().evaluate(_opportunity(funding=FUNDING_UNKNOWN))
    assert r["state"] == "UNVERIFIED"
    assert "escrow_proof_missing" in r["missing_evidence"]


def test_evaluator_unverified_stack_and_permission():
    r = CommercialEvaluator().evaluate(_opportunity(language="", assigned="maintainer_x"))
    assert r["state"] == "UNVERIFIED"
    assert "stack_unverified" in r["missing_evidence"]
    assert "permission_unconfirmed" in r["missing_evidence"]
    assert r["assignment_required"] is True


# ---------- 漏斗聚合器 ----------
def test_funnel_stages_and_attribution():
    f = OpportunityFunnel()
    for _ in range(10):
        f.record_discovered()
    f.record_verdict({"state": "REJECTED", "attribution": ["REJECT_NO_ESCROW"],
                      "missing_evidence": []})
    f.record_verdict({"state": "REJECTED", "attribution": ["REJECT_WRONG_STACK"],
                      "missing_evidence": []})
    f.record_verdict({"state": "UNVERIFIED", "attribution": [],
                      "missing_evidence": ["escrow_proof_missing"]})
    f.record_verdict({"state": "QUALIFIED", "attribution": [], "missing_evidence": []})
    f.advance("claimed")
    f.advance("merged")

    d = f.to_dict()
    assert d["counts"]["discovered"] == 10
    assert d["counts"]["evidence_checked"] == 4
    assert d["counts"]["qualified"] == 1
    assert d["loss_attribution"]["REJECT_NO_ESCROW"] == 1
    assert d["loss_attribution"]["REJECT_WRONG_STACK"] == 1
    assert d["missing_evidence_stats"]["escrow_proof_missing"] == 1
    rates = d["conversion_rates"]
    assert rates["qualify_rate"] == pytest.approx(0.1)
    assert rates["claimed"] if "claimed" in rates else True


# ---------- 种子注入（Mock） ----------
class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._p = payload

    def json(self):
        return self._p


def test_seed_injector_load_seeds(tmp_path):
    seed_file = tmp_path / "seeds.json"
    seed_file.write_text(json.dumps({"seeds": [
        "https://github.com/acme/py/issues/1",
        "https://github.com/acme/py/issues/2"]}), encoding="utf-8")
    inj = SeedInjector(token="tok")
    seeds = inj.load_seeds(str(seed_file))
    assert len(seeds) == 2

    seed_file.write_text(json.dumps({"seeds": ["x"] * 11}), encoding="utf-8")
    with pytest.raises(AssertionError):
        inj.load_seeds(str(seed_file))


def test_seed_injector_enrich_and_run(tmp_path, monkeypatch):
    def fake_get(self, path):
        if path.endswith("/issues/1"):
            return FakeResp(200, {
                "number": 1, "title": "Fix a small bug", "body": "please fix",
                "labels": [{"name": "bug"}], "state": "open",
                "assignee": None, "comments": 2})
        if path.endswith("/issues/2"):
            return FakeResp(200, {
                "number": 2, "title": "Algora bounty task",
                "body": "hosted at https://algora.io/bounties/x",
                "labels": [], "state": "open", "assignee": {"login": "alice"},
                "comments": 5})
        return FakeResp(200, {"language": "Python"})

    monkeypatch.setattr(SeedInjector, "_get", fake_get)
    inj = SeedInjector(token="tok")
    out = str(tmp_path / "out.json")
    report = inj.run(seeds=["https://github.com/acme/py/issues/1",
                            "https://github.com/acme/py/issues/2"], out_path=out)

    records = report["records"]
    # 种子 1：无托管证据 -> UNVERIFIED + escrow_proof_missing
    assert records[0]["verdict"]["state"] == "UNVERIFIED"
    assert "escrow_proof_missing" in records[0]["verdict"]["missing_evidence"]
    # 种子 2：Algora 托管证据 + 已指派 -> UNVERIFIED + permission_unconfirmed
    assert records[1]["escrow_marker"] is True
    assert records[1]["verdict"]["state"] == "UNVERIFIED"
    assert "permission_unconfirmed" in records[1]["verdict"]["missing_evidence"]
    # 漏斗与落盘
    assert report["funnel"]["counts"]["discovered"] == 2
    assert report["funnel"]["counts"]["evidence_checked"] == 2
    persisted = json.load(open(out, encoding="utf-8"))
    assert len(persisted) == 2


# ---------- 商业轨迹 ----------
def test_trace_stage_order_enforced():
    t = CommercialTrace("opp_1")
    t.mark("t0_discovered")
    with pytest.raises(AssertionError):
        t.mark("t3_work_started")


def test_trace_elapsed_and_pnl():
    t = CommercialTrace("opp_1")
    ts = ["2026-10-02T00:00:00Z", "2026-10-02T00:01:00Z", "2026-10-02T00:03:00Z",
          "2026-10-02T00:03:30Z", "2026-10-02T00:10:00Z", "2026-10-02T00:15:00Z",
          "2026-10-02T00:20:00Z"]
    for stage, tstamp in zip(STAGES[:7], ts):
        t.mark(stage, ts=tstamp)
    deltas = t.stage_elapsed()
    assert deltas["t0_discovered->t1_human_qualified"] == 60.0

    t.record_cost("llm_tokens", 5.0, "analysis")
    t.record_cost("sandbox_time", 15.0, "delivery")
    t.confirm_settlement(100.0)
    pnl = t.economic_pnl()
    assert pnl["revenue"] == 100.0
    assert pnl["total_cost"] == 20.0
    assert pnl["net_profit"] == 80.0
    assert pnl["roi"] == 4.0


def test_trace_settlement_requires_revenue_positive():
    t = CommercialTrace("opp_2")
    with pytest.raises(AssertionError):
        t.confirm_settlement(0.0)
