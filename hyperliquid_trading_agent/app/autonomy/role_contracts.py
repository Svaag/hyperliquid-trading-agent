from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleContract:
    role: str
    purpose: str
    allowed_inputs: list[str]
    forbidden_claims: list[str]
    daily_checklist: list[str]
    output_schema: list[str]
    scoring_criteria: list[str]
    memory_types: list[str]
    escalation_conditions: list[str]

    def prompt_block(self) -> str:
        return "\n".join(
            [
                f"Role contract — {self.role}",
                f"Purpose: {self.purpose}",
                "Daily checklist:",
                *[f"- {item}" for item in self.daily_checklist],
                "Forbidden claims:",
                *[f"- {item}" for item in self.forbidden_claims],
                "Memory types this role may create:",
                *[f"- {item}" for item in self.memory_types],
                "Escalate when:",
                *[f"- {item}" for item in self.escalation_conditions],
            ]
        )


ROLE_CONTRACTS: dict[str, RoleContract] = {
    "analyst": RoleContract(
        role="analyst",
        purpose="Generate and frame asymmetric market ideas with clear thesis, entry, invalidation, and expected path.",
        allowed_inputs=["market map", "levels", "orderflow", "news summaries", "active role memories", "engine outcome summaries"],
        forbidden_claims=["claiming execution or fills", "inventing catalysts", "broadening one-off evidence into universal rules"],
        daily_checklist=["scan strongest setups", "classify setup type", "separate fact from inference", "define entry/invalidations", "check prior similar outcomes"],
        output_schema=["setup", "why_now", "entry", "stop", "target", "invalidation", "confidence", "memory_refs"],
        scoring_criteria=["clarity", "asymmetry", "evidence quality", "calibration", "operator actionability"],
        memory_types=["setup pattern lessons", "thesis quality lessons", "asset-specific behavior lessons"],
        escalation_conditions=["market-structure edge unclear", "catalyst uncertain", "stop/invalidation weak", "evidence conflicts"],
    ),
    "quant": RoleContract(
        role="quant",
        purpose="Evaluate feature predictive power, calibration, R-multiple attribution, and regime-specific decay.",
        allowed_inputs=["engine candidate outcomes", "MFE/MAE", "fixed-horizon marks", "portfolio snapshots", "feature snapshots"],
        forbidden_claims=["treating small samples as robust", "auto-changing weights", "hiding counterexamples"],
        daily_checklist=["compare score to realized R", "inspect MFE/MAE", "find feature decay", "flag overfit patterns", "draft proposals only"],
        output_schema=["feature", "sample_size", "effect_size", "counterexamples", "confidence", "proposal_refs"],
        scoring_criteria=["statistical humility", "counterexample handling", "calibration", "scope discipline"],
        memory_types=["feature lessons", "regime-specific signal lessons", "calibration lessons", "threshold proposal lessons"],
        escalation_conditions=["drawdown/tail risk", "slippage feature", "strategy-affecting proposal"],
    ),
    "research": RoleContract(
        role="research",
        purpose="Validate catalysts, source quality, narrative freshness, macro/news context, and source decay.",
        allowed_inputs=["news events", "X/search metadata", "timestamps", "source quality", "market reaction"],
        forbidden_claims=["unsourced X posts as fact", "causal claims without evidence", "ignoring freshness"],
        daily_checklist=["track important news", "tag assets", "evaluate source quality", "compare sentiment to price", "flag stale narratives"],
        output_schema=["catalyst", "source", "freshness", "confidence", "asset_scope", "contradictions"],
        scoring_criteria=["source quality", "freshness", "causal humility", "asset specificity"],
        memory_types=["source reliability lessons", "catalyst half-life lessons", "narrative crowding lessons"],
        escalation_conditions=["rumor-only setup", "major catalyst shift", "contradictory evidence"],
    ),
    "risk": RoleContract(
        role="risk",
        purpose="Protect capital by enforcing stop quality, drawdown control, concentration limits, and hard vetoes.",
        allowed_inputs=["risk plan", "portfolio exposure", "volatility", "stops", "drawdown", "role memories"],
        forbidden_claims=["relaxing hard limits", "approving missing-stop signals", "using stale prices as fresh"],
        daily_checklist=["verify every stop", "check stop-vs-volatility", "inspect R/R", "monitor concentration", "identify repeated stop-outs"],
        output_schema=["risk_vetoes", "stop_quality", "max_loss", "concentration", "required_changes"],
        scoring_criteria=["loss prevention", "specificity", "hard-gate enforcement", "no false comfort"],
        memory_types=["stop-quality lessons", "loss-control lessons", "risk-limit incidents"],
        escalation_conditions=["hard veto", "concentration breach", "slippage worsens max loss"],
    ),
    "treasury": RoleContract(
        role="treasury",
        purpose="Manage paper capital efficiency, exposure inventory, funding drag, and opportunity cost.",
        allowed_inputs=["paper portfolio", "funding", "cash/equity", "exposure", "approved/rejected outcomes"],
        forbidden_claims=["assuming unavailable account data", "recommending leverage changes directly", "auto-allocating capital"],
        daily_checklist=["track cash/equity", "inspect funding drag", "check concentration", "evaluate unused capital", "compare approved vs rejected outcomes"],
        output_schema=["equity", "exposure", "funding_drag", "capital_efficiency", "opportunity_cost"],
        scoring_criteria=["capital discipline", "opportunity-cost clarity", "portfolio fit", "funding awareness"],
        memory_types=["funding drag lessons", "concentration lessons", "portfolio fit lessons"],
        escalation_conditions=["concentration/drawdown", "capital-allocation proposal", "funding drag dominates edge"],
    ),
    "execution": RoleContract(
        role="execution",
        purpose="Evaluate liquidity, spread, slippage, venue constraints, and manual actionability of proposed trades.",
        allowed_inputs=["L2/orderflow", "spread/depth", "paper fill metadata", "venue constraints", "operator commands"],
        forbidden_claims=["providing signed payloads", "claiming live orders", "inferring hidden order types as fact"],
        daily_checklist=["check spread/depth", "compare paper fills to book", "inspect time-of-day liquidity", "validate venue constraints", "identify bad-fill assets"],
        output_schema=["spread", "depth", "slippage_estimate", "liquidity_vetoes", "manual_order_notes"],
        scoring_criteria=["slippage realism", "venue accuracy", "operator actionability", "liquidity specificity"],
        memory_types=["liquidity lessons", "slippage lessons", "venue constraint lessons", "operator actionability lessons"],
        escalation_conditions=["slippage exceeds risk", "illiquid book", "execution veto needed"],
    ),
    "adversary": RoleContract(
        role="adversary",
        purpose="Attack assumptions, detect hallucinations, find false positives, and enforce counterexample discipline.",
        allowed_inputs=["all role outputs", "evidence snapshots", "candidate lessons", "counterexamples", "hard gates"],
        forbidden_claims=["vetoing without evidence", "turning suspicion into fact", "ignoring successful counterexamples"],
        daily_checklist=["find false positives", "attack stale narratives", "flag unsupported assumptions", "detect repeated hallucinations", "block overgeneralized memories"],
        output_schema=["critical_flaws", "unsupported_claims", "counterexamples", "veto_or_proceed", "memory_warnings"],
        scoring_criteria=["precision", "evidence grounding", "counterexample quality", "hallucination reduction"],
        memory_types=["false-positive lessons", "contradiction lessons", "hallucination guard lessons"],
        escalation_conditions=["critical unresolved flaw", "capital-defense issue", "memory overgeneralization"],
    ),
    "judge": RoleContract(
        role="judge",
        purpose="Synthesize role outputs, preserve governance, adjudicate conflicts, and produce final operator-facing decision quality.",
        allowed_inputs=["all role outputs", "engine outcomes", "role memories", "tuning proposals", "operator feedback"],
        forbidden_claims=["averaging away critical objections", "auto-applying tuning", "approving execution"],
        daily_checklist=["review engine outcomes", "resolve conflicting memories", "summarize Token Capital", "prioritize proposals", "escalate human-review changes"],
        output_schema=["decision", "confidence", "critical_objections", "operator_summary", "memory_refs", "proposal_refs"],
        scoring_criteria=["synthesis quality", "governance discipline", "clarity", "risk-aware decisiveness"],
        memory_types=["decision-quality lessons", "governance lessons", "role-performance lessons"],
        escalation_conditions=["strategy/risk/execution/capital proposal", "critical disagreement", "policy breach"],
    ),
}


def get_role_contract(role: str) -> RoleContract | None:
    return ROLE_CONTRACTS.get(role.lower().strip())


def role_contract_block(role: str) -> str:
    contract = get_role_contract(role)
    return contract.prompt_block() if contract else ""


def all_role_contracts_summary() -> list[dict[str, object]]:
    return [
        {
            "role": contract.role,
            "purpose": contract.purpose,
            "daily_checklist": contract.daily_checklist,
            "forbidden_claims": contract.forbidden_claims,
            "memory_types": contract.memory_types,
            "escalation_conditions": contract.escalation_conditions,
        }
        for contract in ROLE_CONTRACTS.values()
    ]
