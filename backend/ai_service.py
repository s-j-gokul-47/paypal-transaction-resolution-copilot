"""
ai_service.py - Record-grounded AI explanation, support summary, and dispute analysis service.

Uses OpenAI GPT-4o-mini when an API key is configured, with strict grounding
instructions so only verified transaction/dispute fields are cited. Falls back
to deterministic heuristic logic when no API key is configured.
"""
import json
from datetime import datetime
from typing import Optional

from models import AIRecommendationResponse
from status_engine import StatusEngine


# ---------------------------------------------------------------------------
# Heuristic helpers (used in mock/fallback mode)
# ---------------------------------------------------------------------------

_REASON_TO_ACTION: dict[str, tuple[str, str, list[str], int]] = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": (
        "ESCALATE",
        "Buyer claims non-delivery. Verify carrier tracking and delivery confirmation before deciding.",
        [
            "Request proof of delivery from the seller",
            "Review order fulfillment logs and carrier tracking",
            "Contact buyer to confirm delivery address",
            "If confirmed undelivered, process full refund or reshipment",
        ],
        65,
    ),
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": (
        "REQUEST_INFO",
        "Buyer alleges item differs from listing. Evidence from both sides is required.",
        [
            "Request photos/video evidence from the buyer",
            "Obtain original listing details and specifications from seller",
            "Compare buyer evidence against seller listing",
            "Determine if partial or full refund is appropriate",
        ],
        55,
    ),
    "UNAUTHORIZED": (
        "REFUND",
        "Potential unauthorized charge reported. Prioritize buyer protection and security verification.",
        [
            "Flag account for security review",
            "Verify transaction authenticity with payer",
            "Issue full refund if unauthorized transaction is confirmed",
            "Report to fraud team",
        ],
        90,
    ),
    "DUPLICATE_TRANSACTION": (
        "REFUND",
        "Duplicate charge reported. Verify both transactions and refund the duplicate capture if settled.",
        [
            "Compare original and duplicate transaction IDs and timestamps",
            "Confirm both payments reached COMPLETED status",
            "Refund the duplicate capture",
            "Notify buyer with refund reference ID",
        ],
        40,
    ),
    "INCORRECT_AMOUNT": (
        "REFUND",
        "Buyer charged different amount than agreed. Verify order total and refund the difference.",
        [
            "Review original order confirmation and invoice",
            "Calculate the exact overcharge difference",
            "Issue partial refund for the difference",
            "Update transaction notes",
        ],
        45,
    ),
}

_DEFAULT_HEURISTIC = (
    "REQUEST_INFO",
    "Additional verified context required before finalizing action.",
    [
        "Gather additional information from both buyer and seller",
        "Review transaction timeline and metadata",
        "Apply standard resolution policy",
    ],
    50,
)


def _heuristic_recommendation(dispute_data: dict, transaction_data: dict) -> AIRecommendationResponse:
    reason = (dispute_data.get("reason") or "").upper()
    amount = float(transaction_data.get("amount", 0) or 0)

    action, reasoning, steps, base_risk = _REASON_TO_ACTION.get(reason, _DEFAULT_HEURISTIC)

    if amount > 1000:
        base_risk = min(100, base_risk + 15)
    elif amount > 500:
        base_risk = min(100, base_risk + 7)

    confidence = 0.78 if reason in _REASON_TO_ACTION else 0.45

    time_map = {
        "REFUND": "1-4 hours",
        "REQUEST_INFO": "24-48 hours",
        "ESCALATE": "24-72 hours",
    }

    return AIRecommendationResponse(
        recommended_action=action,
        confidence=confidence,
        reasoning=reasoning,
        suggested_steps=steps,
        risk_score=base_risk,
        estimated_resolution_time=time_map.get(action, "24-48 hours"),
        mode="heuristic",
    )


def _heuristic_risk(transaction_data: dict) -> dict:
    amount = float(transaction_data.get("amount", 0) or 0)
    status = str(transaction_data.get("status", "")).upper()

    if status in {"DISPUTED", "FAILED"}:
        level, score, flags = "HIGH", 75, [f"Recorded transaction status is {status}"]
    elif amount > 1000:
        level, score, flags = "HIGH", 70, ["High-value transaction (>= 1000)"]
    elif amount > 500:
        level, score, flags = "MEDIUM", 45, ["Moderate-value transaction (>= 500)"]
    elif amount > 100:
        level, score, flags = "LOW", 20, ["Standard transaction amount"]
    else:
        level, score, flags = "LOW", 10, ["Low-value transaction amount"]

    if status == "REFUNDED":
        flags.append("Transaction is marked REFUNDED")
        score = min(100, score + 10)

    return {
        "risk_level": level,
        "risk_score": score,
        "flags": flags,
        "recommendation": (
            "Requires immediate review" if level == "HIGH"
            else "Monitor closely" if level == "MEDIUM"
            else "Standard handling"
        ),
        "mode": "heuristic",
        "source": "heuristic",
    }


def _build_heuristic_support_summary(
    transaction_data: dict,
    timeline_events: list[dict],
    duplicate_candidates: list[dict],
    disputes: list[dict],
    audience: str,
    agent_notes: Optional[str],
) -> str:
    pp_id = transaction_data.get("paypal_transaction_id", "N/A")
    amount = float(transaction_data.get("amount", 0) or 0)
    currency = transaction_data.get("currency", "USD")
    status = transaction_data.get("status", "UNKNOWN")
    buyer = transaction_data.get("buyer_email") or "Not recorded"
    created_at = transaction_data.get("created_at") or "Not recorded"
    metadata = transaction_data.get("metadata") or {}
    order_id = metadata.get("order_id")
    error_code = metadata.get("error_code")

    active_dups = [d for d in duplicate_candidates if d.get("classification") == "POSSIBLE_DUPLICATE"]
    retry_dups = [d for d in duplicate_candidates if d.get("classification") == "RETRY_AFTER_FAILURE"]

    if audience == "buyer":
        lines = [
            f"Hello,",
            "",
            f"Here is the verified status update for your PayPal transaction {pp_id} ({amount:.2f} {currency}):",
            f"- Current Status: {status}",
            f"- Recorded Date (UTC): {created_at}",
        ]
        if order_id:
            lines.append(f"- Order Reference: {order_id}")
        if status == "COMPLETED":
            total_refunded = float(metadata.get("total_refunded") or 0.0)
            if metadata.get("partial_refund") and total_refunded > 0:
                lines.append(
                    f"- Details: Your payment was captured, and a partial refund of "
                    f"{total_refunded:.2f} {currency} has been processed."
                )
            else:
                lines.append("- Details: Your payment has been captured and settled.")
        elif status == "PENDING":
            lines.append("- Details: Your payment is currently pending final settlement/clearing.")
        elif status == "FAILED":
            lines.append(
                f"- Details: This payment attempt did not succeed"
                + (f" (reason: {error_code})" if error_code else "")
                + ", so no funds were settled for it."
            )
        elif status == "REFUNDED":
            lines.append("- Details: A refund has been processed for this transaction.")
        elif status == "DISPUTED":
            lines.append("- Details: This transaction currently has an active dispute under review.")

        if active_dups:
            lines.append(
                f"- Duplicate Check: We detected {len(active_dups)} matching charge(s) "
                f"({', '.join(d['paypal_transaction_id'] for d in active_dups)}) and are reviewing it for refund if unintended."
            )
        elif retry_dups:
            lines.append(
                "- Duplicate Check: We confirmed the earlier failed attempt was not settled, so you were not double-charged."
            )
        if agent_notes:
            lines.extend(["", f"Additional Note: {agent_notes}"])
        lines.extend(["", "Best regards,", "Merchant Support Team"])
        return "\n".join(lines)

    if audience == "escalation":
        lines = [
            f"ESCALATION BRIEF — {pp_id}",
            f"Status: {status} | Amount: {amount:.2f} {currency} | Buyer: {buyer}",
            f"Created (UTC): {created_at}",
            f"Metadata: {json.dumps(metadata)}" if metadata else "Metadata: None recorded",
            "",
            "Verified Timeline:",
        ]
        for ev in timeline_events:
            lines.append(f"  * [{ev.get('occurred_at')}] {ev.get('title')}: {ev.get('description') or ''}")
        if not timeline_events:
            lines.append("  * No timeline events recorded.")
        lines.extend([
            "",
            f"Duplicate Check: {len(duplicate_candidates)} related transaction(s) flagged.",
        ])
        for dup in duplicate_candidates:
            lines.append(f"  * {dup.get('paypal_transaction_id')} ({dup.get('classification')} / {dup.get('confidence')}): {dup.get('explanation')}")
        if disputes:
            lines.append("")
            lines.append("Linked Disputes:")
            for d in disputes:
                lines.append(f"  * {d.get('paypal_dispute_id') or d.get('id')} — {d.get('status')} ({d.get('reason')})")
        if agent_notes:
            lines.extend(["", f"Agent Notes: {agent_notes}"])
        return "\n".join(lines)

    # Default: internal support note
    lines = [
        f"Support Summary — {pp_id}",
        f"- Verified Status: {status}",
        f"- Amount: {amount:.2f} {currency}",
        f"- Buyer: {buyer}",
        f"- Recorded At (UTC): {created_at}",
    ]
    if order_id:
        lines.append(f"- Order ID: {order_id}")
    if metadata.get("partial_refund") and float(metadata.get("total_refunded") or 0.0) > 0:
        lines.append(
            f"- Partial Refund Recorded: {float(metadata.get('total_refunded') or 0.0):.2f} {currency} "
            f"(remaining refundable: {float(metadata.get('remaining_refundable') or 0.0):.2f} {currency})"
        )
    if error_code:
        lines.append(f"- Processor Error Code: {error_code}")
    if timeline_events:
        lines.append(f"- Timeline Events ({len(timeline_events)}): latest = {timeline_events[-1].get('title')} at {timeline_events[-1].get('occurred_at')}")
    if active_dups:
        lines.append(
            f"- Duplicate Alert: {len(active_dups)} possible duplicate(s) ({', '.join(d['paypal_transaction_id'] for d in active_dups)})"
        )
    else:
        lines.append("- Duplicate Alert: No active duplicate charges detected.")
    if disputes:
        lines.append(f"- Linked Disputes: {len(disputes)} ({', '.join(d.get('status', '') for d in disputes)})")
    if agent_notes:
        lines.append(f"- Agent Notes: {agent_notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AIService
# ---------------------------------------------------------------------------

class AIService:
    """
    Wraps OpenAI calls for grounded transaction explanations, support summaries,
    and secondary dispute analysis. Falls back to deterministic heuristic logic
    when `api_key` is absent or placeholder.
    """

    def __init__(self, api_key: str):
        self._api_key = (api_key or "").strip()
        self._has_key = bool(
            self._api_key
            and self._api_key not in {"your_openai_api_key", ""}
        )
        self.client = None
        self.model = "gpt-4o-mini"
        self._status_engine = StatusEngine()

        if self._has_key:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=self._api_key)

    # ------------------------------------------------------------------
    # Core MVP: Grounded Transaction Explanation
    # ------------------------------------------------------------------

    async def explain_transaction(
        self,
        transaction_data: dict,
        timeline_events: list[dict],
        duplicate_candidates: list[dict],
        disputes: list[dict],
    ) -> dict:
        """
        Explain a transaction's verified state strictly from DB/API records.
        Never invents unrecorded statuses or error codes.
        """
        heuristic_result = self._status_engine.explain_transaction_state(
            transaction_data, timeline_events, duplicate_candidates, disputes
        )
        if not self._has_key:
            return heuristic_result

        system_prompt = (
            "You are a PayPal Transaction Resolution Copilot. "
            "CRITICAL GROUNDING RULE: Only cite statuses, amounts, timestamps, metadata error codes, "
            "timeline events, duplicate candidates, and disputes explicitly present in the provided JSON. "
            "Never invent or guess missing transaction statuses, tracking numbers, or error codes. "
            "Respond ONLY with valid JSON matching:\n"
            "{\n"
            '  "headline": string,\n'
            '  "plain_english_summary": string,\n'
            '  "likely_causes": [string],\n'
            '  "recommended_actions": [string],\n'
            '  "verified_fields_used": [string]\n'
            "}"
        )
        user_payload = {
            "transaction": transaction_data,
            "timeline_events": timeline_events,
            "duplicate_candidates": duplicate_candidates,
            "disputes": disputes,
        }
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, default=str)},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(response.choices[0].message.content)
            return {
                "transaction_id": transaction_data.get("id"),
                "paypal_transaction_id": transaction_data.get("paypal_transaction_id"),
                "status": transaction_data.get("status"),
                "headline": parsed.get("headline", heuristic_result["headline"]),
                "plain_english_summary": parsed.get(
                    "plain_english_summary", heuristic_result["plain_english_summary"]
                ),
                "likely_causes": parsed.get("likely_causes", heuristic_result["likely_causes"]),
                "recommended_actions": parsed.get(
                    "recommended_actions", heuristic_result["recommended_actions"]
                ),
                "verified_fields_used": parsed.get(
                    "verified_fields_used", heuristic_result["verified_fields_used"]
                ),
                "mode": "openai",
                "data_source": "database",
            }
        except Exception:
            return heuristic_result

    # ------------------------------------------------------------------
    # Core MVP: Support Summary Preparation
    # ------------------------------------------------------------------

    async def generate_support_summary(
        self,
        transaction_data: dict,
        timeline_events: list[dict],
        duplicate_candidates: list[dict],
        disputes: list[dict],
        audience: str = "internal",
        agent_notes: Optional[str] = None,
    ) -> dict:
        """
        Prepare a copy-ready support summary grounded strictly in verified fields.
        """
        verified_facts = {
            "paypal_transaction_id": transaction_data.get("paypal_transaction_id"),
            "status": transaction_data.get("status"),
            "amount": transaction_data.get("amount"),
            "currency": transaction_data.get("currency"),
            "timeline_event_count": len(timeline_events),
            "duplicate_candidate_count": len(duplicate_candidates),
            "dispute_count": len(disputes),
        }

        if not self._has_key:
            summary_text = _build_heuristic_support_summary(
                transaction_data,
                timeline_events,
                duplicate_candidates,
                disputes,
                audience,
                agent_notes,
            )
            return {
                "transaction_id": transaction_data.get("id"),
                "paypal_transaction_id": transaction_data.get("paypal_transaction_id"),
                "audience": audience,
                "summary": summary_text,
                "verified_facts": verified_facts,
                "mode": "heuristic",
                "generated_at": datetime.utcnow(),
            }

        prompt = (
            f"Write a concise '{audience}' support summary for this PayPal transaction.\n"
            "STRICT RULE: Only mention facts present in the verified JSON below. Do not invent IDs, statuses, or dates.\n\n"
            f"Verified Record:\n{json.dumps({'transaction': transaction_data, 'timeline': timeline_events, 'duplicates': duplicate_candidates, 'disputes': disputes, 'agent_notes': agent_notes}, default=str, indent=2)}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return {
                "transaction_id": transaction_data.get("id"),
                "paypal_transaction_id": transaction_data.get("paypal_transaction_id"),
                "audience": audience,
                "summary": response.choices[0].message.content,
                "verified_facts": verified_facts,
                "mode": "openai",
                "generated_at": datetime.utcnow(),
            }
        except Exception:
            summary_text = _build_heuristic_support_summary(
                transaction_data,
                timeline_events,
                duplicate_candidates,
                disputes,
                audience,
                agent_notes,
            )
            return {
                "transaction_id": transaction_data.get("id"),
                "paypal_transaction_id": transaction_data.get("paypal_transaction_id"),
                "audience": audience,
                "summary": summary_text,
                "verified_facts": verified_facts,
                "mode": "heuristic",
                "generated_at": datetime.utcnow(),
            }

    # ------------------------------------------------------------------
    # Dispute Analysis (Secondary feature)
    # ------------------------------------------------------------------

    async def analyze_dispute(
        self,
        dispute_data: dict,
        transaction_data: dict,
    ) -> AIRecommendationResponse:
        if not self._has_key:
            return _heuristic_recommendation(dispute_data, transaction_data)

        system_prompt = (
            "You are an expert PayPal dispute resolution analyst. "
            "Use ONLY facts provided in the input JSON. "
            "Respond ONLY with valid JSON matching this schema:\n"
            "{\n"
            '  "recommended_action": "REFUND"|"ESCALATE"|"REQUEST_INFO"|"REJECT"|"ACCEPT",\n'
            '  "confidence": float (0-1),\n'
            '  "reasoning": string,\n'
            '  "suggested_steps": [string],\n'
            '  "risk_score": integer (0-100),\n'
            '  "estimated_resolution_time": string\n'
            "}"
        )
        user_prompt = (
            f"Dispute data:\n{json.dumps(dispute_data, indent=2, default=str)}\n\n"
            f"Transaction data:\n{json.dumps(transaction_data, indent=2, default=str)}"
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            raw = json.loads(response.choices[0].message.content)
            raw["mode"] = "openai"
            return AIRecommendationResponse(**raw)
        except Exception:
            return _heuristic_recommendation(dispute_data, transaction_data)

    async def generate_response_template(
        self,
        dispute_reason: str,
        merchant_perspective: str,
    ) -> str:
        if not self._has_key:
            return (
                f"Dear PayPal Dispute Resolution Team,\n\n"
                f"We are writing in response to the dispute raised regarding reason: "
                f"{dispute_reason}.\n\n"
                f"Merchant's perspective:\n{merchant_perspective}\n\n"
                f"Please find our verified documentation attached.\n\n"
                f"Sincerely,\nMerchant Support Team\n\n"
                f"---\n[Heuristic template generated without OpenAI key]"
            )

        prompt = (
            f"Write a professional, concise merchant dispute response letter for PayPal.\n"
            f"Dispute reason: {dispute_reason}\n"
            f"Merchant's perspective: {merchant_perspective}\n"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            return response.choices[0].message.content
        except Exception as exc:
            return f"[Error generating template: {exc}]\n\nFallback:\n{merchant_perspective}"

    async def classify_risk(self, transaction_data: dict) -> dict:
        if not self._has_key:
            return _heuristic_risk(transaction_data)

        system_prompt = (
            "You are a payment risk analyst. Base your assessment ONLY on the supplied transaction JSON. "
            "Respond ONLY with valid JSON:\n"
            "{\n"
            '  "risk_level": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",\n'
            '  "risk_score": integer (0-100),\n'
            '  "flags": [string],\n'
            '  "recommendation": string\n'
            "}"
        )
        user_prompt = f"Transaction data:\n{json.dumps(transaction_data, indent=2, default=str)}"

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            result["mode"] = "openai"
            result["source"] = "openai"
            return result
        except Exception:
            return _heuristic_risk(transaction_data)

    async def summarize_case(
        self,
        case_id: str,
        transaction: dict,
        dispute: dict,
        resolution_steps: list,
    ) -> str:
        def _format_step(s) -> str:
            if isinstance(s, dict):
                ts = s.get("timestamp", "")
                action = s.get("action", "STEP")
                notes = s.get("notes", "")
                agent = s.get("agent", "")
                return f"- [{ts}] {action}: {notes} ({agent})"
            return f"- {s}"

        steps_text = "\n".join(_format_step(step) for step in resolution_steps) if resolution_steps else "- No steps recorded yet"

        if not self._has_key:
            amount = transaction.get("amount", "N/A")
            currency = transaction.get("currency", "USD")
            return (
                f"# Case Summary - {case_id}\n\n"
                f"## Transaction\n"
                f"- ID: {transaction.get('paypal_transaction_id', 'N/A')}\n"
                f"- Amount: {amount} {currency}\n"
                f"- Status: {transaction.get('status', 'N/A')}\n"
                f"- Buyer: {transaction.get('buyer_email', 'N/A')}\n"
                f"- Seller: {transaction.get('seller_email', 'N/A')}\n\n"
                f"## Dispute\n"
                f"- Reason: {dispute.get('reason', 'N/A')}\n"
                f"- Status: {dispute.get('status', 'N/A')}\n"
                f"- Evidence Submitted: {dispute.get('evidence_submitted', False)}\n\n"
                f"## Resolution Steps\n{steps_text}\n\n"
                f"## Outcome\n"
                f"{dispute.get('resolution') or 'Pending resolution'}\n\n"
                f"---\n*Summary generated by heuristic engine (OpenAI key not configured)*"
            )

        prompt = (
            f"Generate a concise markdown case summary using ONLY the verified data below.\n\n"
            f"Case ID: {case_id}\n"
            f"Transaction:\n{json.dumps(transaction, indent=2, default=str)}\n\n"
            f"Dispute:\n{json.dumps(dispute, indent=2, default=str)}\n\n"
            f"Resolution steps:\n{steps_text}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return response.choices[0].message.content
        except Exception as exc:
            return f"[Error generating summary: {exc}]\n\nCase ID: {case_id}"
