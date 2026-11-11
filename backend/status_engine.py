"""
status_engine.py - Core transaction-resolution engine + secondary dispute SLA logic.

Provides:
  - Duplicate payment detection across verified transaction records
  - Deterministic, record-grounded transaction state explanations
  - Dispute priority, next actions, and SLA tracking (secondary workflow)
"""
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Iterable


# ---------------------------------------------------------------------------
# Priority Enum & SLA Configuration (Dispute / Case management)
# ---------------------------------------------------------------------------

class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


SLA_HOURS: dict[Priority, int] = {
    Priority.CRITICAL: 4,
    Priority.HIGH: 24,
    Priority.MEDIUM: 72,
    Priority.LOW: 168,
}

_HIGH_PRIORITY_REASONS = {
    "UNAUTHORIZED",
    "FRAUD",
    "IDENTITY_THEFT",
}

_MEDIUM_PRIORITY_REASONS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
    "SIGNIFICANTLY_NOT_AS_DESCRIBED",
    "DUPLICATE_TRANSACTION",
}

_NEXT_ACTIONS: dict[str, list[str]] = {
    "OPEN": [
        "Review dispute details",
        "Contact buyer for additional information",
        "Gather evidence from seller",
        "Submit evidence to PayPal",
        "Accept claim",
        "Escalate to PayPal",
    ],
    "UNDER_REVIEW": [
        "Monitor PayPal review progress",
        "Respond to PayPal information requests",
        "Prepare for potential escalation",
        "Issue voluntary refund to expedite resolution",
    ],
    "RESOLVED": [
        "Review resolution outcome",
        "Process any required refunds",
        "Update internal records",
        "Send resolution notification to parties",
        "Archive case",
    ],
    "CLOSED": [
        "Archive case documentation",
        "Review for process improvement opportunities",
        "Generate post-resolution report",
    ],
}


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.rstrip("Z"))
        except ValueError:
            return None
    return None


def _get_attr(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# Allowed capture/webhook status transitions to prevent out-of-order overwrites.
# Keys are current persisted status; values are statuses a capture/dispute webhook
# may transition to (excluding explicit merchant dispute-win resolution).
_ALLOWED_CAPTURE_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"PENDING", "COMPLETED", "FAILED", "DISPUTED", "REFUNDED"},
    "COMPLETED": {"COMPLETED", "DISPUTED", "REFUNDED"},
    "DISPUTED": {"DISPUTED", "REFUNDED"},
    "FAILED": {"FAILED", "DISPUTED", "REFUNDED"},
    "REFUNDED": {"REFUNDED"},
}


def is_valid_webhook_status_transition(current_status: str, target_status: str) -> bool:
    """Return True if transitioning `current_status` -> `target_status` via capture/dispute webhook is monotonic."""
    curr = (current_status or "PENDING").upper()
    tgt = (target_status or "PENDING").upper()
    return tgt in _ALLOWED_CAPTURE_TRANSITIONS.get(curr, {tgt})


def get_refund_summary(
    txn_amount: float,
    txn_status: str,
    metadata: Optional[dict],
) -> tuple[float, float, list[dict]]:
    """Compute `(total_refunded, remaining_refundable, refund_entries)` from verified metadata."""
    meta = dict(metadata or {})
    entries = [dict(r) for r in (meta.get("refunds") or []) if isinstance(r, dict)]
    if entries:
        summed = sum(
            float(r.get("amount") or 0)
            for r in entries
            if (r.get("status") or "COMPLETED").upper() not in {"FAILED", "CANCELLED"}
        )
        recorded_total = float(meta.get("total_refunded") or 0.0)
        total_refunded = max(summed, recorded_total)
    elif (txn_status or "").upper() == "REFUNDED":
        total_refunded = float(txn_amount or 0.0)
    else:
        total_refunded = float(meta.get("total_refunded") or 0.0)

    total_refunded = round(min(max(0.0, total_refunded), float(txn_amount or 0.0)), 2)
    remaining = round(max(0.0, float(txn_amount or 0.0) - total_refunded), 2)
    return total_refunded, remaining, entries


def record_refund_state(
    txn_amount: float,
    txn_status: str,
    metadata: Optional[dict],
    *,
    refund_id: Optional[str],
    amount: Optional[float],
    currency: str,
    source: str,
    refund_status: str = "COMPLETED",
    paypal_total_refunded: Optional[float] = None,
    is_simulated: bool = False,
    webhook_confirmed: bool = False,
    is_reversal: bool = False,
) -> dict:
    """
    Reconcile partial/full refund or capture reversal state idempotently across API and webhook paths.

    Deduplicates repeated webhook deliveries by `refund_id` even across distinct PayPal event IDs,
    avoids double-counting API-initiated refunds when confirmed by webhook, and keeps `status`
    at `COMPLETED`/`DISPUTED` until cumulative refunded amount covers the full transaction.
    """
    meta = dict(metadata or {})
    curr_status = (txn_status or "COMPLETED").upper()
    norm_refund_status = (refund_status or "COMPLETED").upper()
    total_before, _, entries = get_refund_summary(txn_amount, curr_status, meta)

    clean_refund_id = (refund_id or "").strip() or None
    refund_amt = round(float(amount), 2) if amount is not None else round(float(txn_amount or 0.0), 2)

    existing_idx = next(
        (i for i, e in enumerate(entries) if clean_refund_id and e.get("refund_id") == clean_refund_id),
        None,
    )

    if existing_idx is not None:
        existing = entries[existing_idx]
        if (
            webhook_confirmed
            and existing.get("webhook_confirmed")
            and (existing.get("status") or "").upper() == norm_refund_status
        ):
            remaining = round(max(0.0, float(txn_amount or 0.0) - total_before), 2)
            return {
                "action": "duplicate_ignored",
                "new_status": curr_status,
                "metadata": meta,
                "total_refunded": total_before,
                "remaining_refundable": remaining,
                "is_partial": 0 < total_before < float(txn_amount or 0.0) - 0.005,
                "refund_amount": float(existing.get("amount") or refund_amt),
            }

        existing["status"] = norm_refund_status
        if webhook_confirmed:
            existing["webhook_confirmed"] = True
        if amount is not None:
            existing["amount"] = refund_amt
        action = "confirmed" if webhook_confirmed else "recorded"
    else:
        entries.append(
            {
                "refund_id": clean_refund_id or f"REVERSAL-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                "amount": refund_amt,
                "currency": (currency or "USD").upper(),
                "status": norm_refund_status,
                "source": source,
                "webhook_confirmed": webhook_confirmed,
                "is_reversal": is_reversal,
                "recorded_at": datetime.utcnow().isoformat() + "Z",
            }
        )
        action = "recorded"

    summed = sum(
        float(r.get("amount") or 0)
        for r in entries
        if (r.get("status") or "COMPLETED").upper() not in {"FAILED", "CANCELLED"}
    )
    if paypal_total_refunded is not None:
        total_refunded = round(max(summed, float(paypal_total_refunded)), 2)
    else:
        total_refunded = round(summed, 2)
    total_refunded = round(min(total_refunded, float(txn_amount or 0.0)), 2)
    remaining = round(max(0.0, float(txn_amount or 0.0) - total_refunded), 2)

    is_pending_refund = norm_refund_status == "PENDING"
    fully_refunded = is_reversal or (
        not is_pending_refund and total_refunded >= float(txn_amount or 0.0) - 0.005
    )
    is_partial = (not fully_refunded) and total_refunded > 0

    if curr_status == "REFUNDED" or fully_refunded:
        new_status = "REFUNDED"
    else:
        new_status = curr_status

    meta["refunds"] = entries
    if clean_refund_id:
        meta["refund_id"] = clean_refund_id
    meta["refund_simulated"] = bool(meta.get("refund_simulated") or is_simulated)
    meta["total_refunded"] = total_refunded
    meta["remaining_refundable"] = remaining
    meta["partial_refund"] = is_partial

    return {
        "action": action,
        "new_status": new_status,
        "metadata": meta,
        "total_refunded": total_refunded,
        "remaining_refundable": remaining,
        "is_partial": is_partial,
        "refund_amount": refund_amt,
    }


class StatusEngine:
    """
    Core transaction-resolution engine and secondary dispute SLA engine.
    """

    # ------------------------------------------------------------------
    # Duplicate Payment Detection (Core MVP)
    # ------------------------------------------------------------------

    def detect_duplicate_transactions(
        self,
        target_txn,
        all_txns: Iterable,
        window_hours: int = 24,
    ) -> list[dict]:
        """
        Identify potential duplicate charges or retry patterns for `target_txn`
        by comparing verified fields (`buyer_email`, `merchant_id`, `amount`,
        `currency`, `created_at`, `status`).
        """
        target_id = _get_attr(target_txn, "id")
        target_buyer = (_get_attr(target_txn, "buyer_email") or "").strip().lower()
        target_merchant = (_get_attr(target_txn, "merchant_id") or "").strip().lower()
        target_currency = (_get_attr(target_txn, "currency") or "USD").strip().upper()
        target_amount = float(_get_attr(target_txn, "amount") or 0)
        target_status = (_get_attr(target_txn, "status") or "PENDING").upper()
        target_created = _parse_dt(_get_attr(target_txn, "created_at"))

        if not target_buyer or not target_created:
            return []

        candidates: list[dict] = []
        max_seconds = window_hours * 3600

        for other in all_txns:
            other_id = _get_attr(other, "id")
            if other_id == target_id:
                continue

            other_buyer = (_get_attr(other, "buyer_email") or "").strip().lower()
            other_merchant = (_get_attr(other, "merchant_id") or "").strip().lower()
            other_currency = (_get_attr(other, "currency") or "USD").strip().upper()
            other_amount = float(_get_attr(other, "amount") or 0)
            other_status = (_get_attr(other, "status") or "PENDING").upper()
            other_created = _parse_dt(_get_attr(other, "created_at"))
            other_pp_id = _get_attr(other, "paypal_transaction_id") or other_id

            if not other_created:
                continue

            if other_buyer != target_buyer or other_currency != target_currency:
                continue
            if abs(other_amount - target_amount) > 0.01:
                continue
            if target_merchant and other_merchant and other_merchant != target_merchant:
                continue

            diff_seconds = int(abs((target_created - other_created).total_seconds()))
            if diff_seconds > max_seconds:
                continue

            matching_fields = ["buyer_email", "amount", "currency"]
            if target_merchant == other_merchant and target_merchant:
                matching_fields.append("merchant_id")

            if diff_seconds <= 900:
                confidence = "HIGH"
            elif diff_seconds <= 4 * 3600:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

            minutes = max(1, diff_seconds // 60)
            if diff_seconds < 60:
                time_phrase = f"{diff_seconds} seconds apart"
            elif minutes < 60:
                time_phrase = f"{minutes} minute(s) apart"
            else:
                time_phrase = f"{round(diff_seconds / 3600, 1)} hour(s) apart"

            pair_statuses = {target_status, other_status}
            if "FAILED" in pair_statuses and pair_statuses != {"FAILED"}:
                classification = "RETRY_AFTER_FAILURE"
                explanation = (
                    f"Matched {other_pp_id} ({other_status}) for {other_amount:.2f} {other_currency} "
                    f"({time_phrase}). One payment failed, indicating a customer retry rather than a double capture."
                )
                recommended_action = "Confirm to buyer that the failed attempt was not settled; no duplicate refund needed."
            elif "REFUNDED" in pair_statuses:
                classification = "ALREADY_REFUNDED"
                explanation = (
                    f"Matched {other_pp_id} ({other_status}) for {other_amount:.2f} {other_currency} "
                    f"({time_phrase}). A matching payment has already been refunded."
                )
                recommended_action = "Share the refund record with the buyer; no additional refund required."
            else:
                classification = "POSSIBLE_DUPLICATE"
                explanation = (
                    f"Matched {other_pp_id} ({other_status}) for {other_amount:.2f} {other_currency} "
                    f"from {target_buyer} ({time_phrase}). Both payments are active/settled."
                )
                recommended_action = (
                    "Verify order fulfillment; if only one order was placed, refund the duplicate capture."
                )

            candidates.append(
                {
                    "transaction_id": other_id,
                    "paypal_transaction_id": other_pp_id,
                    "status": other_status,
                    "amount": other_amount,
                    "currency": other_currency,
                    "created_at": other_created,
                    "time_diff_seconds": diff_seconds,
                    "classification": classification,
                    "confidence": confidence,
                    "matching_fields": matching_fields,
                    "explanation": explanation,
                    "recommended_action": recommended_action,
                }
            )

        candidates.sort(key=lambda c: c["time_diff_seconds"])
        return candidates

    # ------------------------------------------------------------------
    # Record-Grounded Transaction State Explainer (Core MVP)
    # ------------------------------------------------------------------

    def explain_transaction_state(
        self,
        txn,
        timeline_events: list[dict],
        duplicate_candidates: list[dict],
        disputes: list[dict],
    ) -> dict:
        """
        Produce a grounded explanation of a transaction's current state using
        only fields present in the verified DB/API record, explicitly disclosing
        any unavailable data without guessing unrecorded causes or outcomes.
        """
        txn_id = str(_get_attr(txn, "id") or "")
        raw_pp_id = _get_attr(txn, "paypal_transaction_id")
        pp_id = str(raw_pp_id).strip() if raw_pp_id else "UNAVAILABLE"

        raw_status = _get_attr(txn, "status")
        status = str(raw_status).strip().upper() if raw_status else "UNKNOWN"

        raw_amount = _get_attr(txn, "amount")
        has_amount = raw_amount is not None
        amount = float(raw_amount) if has_amount else 0.0

        raw_currency = _get_attr(txn, "currency")
        currency = str(raw_currency).strip().upper() if raw_currency else ""
        amount_phrase = (
            f"{amount:.2f} {currency or 'currency-unrecorded'}"
            if has_amount
            else "unavailable amount"
        )

        metadata = _get_attr(txn, "metadata_") or _get_attr(txn, "metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        buyer_email = _get_attr(txn, "buyer_email")
        created_at = _get_attr(txn, "created_at")
        updated_at = _get_attr(txn, "updated_at")
        order_id = metadata.get("order_id") or metadata.get("paypal_order_id")
        error_code = metadata.get("error_code")
        expected_ship = metadata.get("expected_ship")
        pending_reason = metadata.get("pending_reason") or (
            ((metadata.get("paypal_capture") or {}).get("status_details") or {}).get("reason")
            if isinstance(metadata.get("paypal_capture"), dict)
            else None
        )

        verified_fields: list[str] = []
        unavailable_fields: list[str] = []

        if raw_pp_id:
            verified_fields.append("paypal_transaction_id")
        else:
            unavailable_fields.append("paypal_transaction_id")

        if raw_status and status != "UNKNOWN":
            verified_fields.append("status")
        else:
            unavailable_fields.append("status")

        if has_amount:
            verified_fields.append("amount")
        else:
            unavailable_fields.append("amount")

        if currency:
            verified_fields.append("currency")
        else:
            unavailable_fields.append("currency")

        if created_at:
            verified_fields.append("created_at")
        else:
            unavailable_fields.append("created_at")

        if updated_at:
            verified_fields.append("updated_at")

        if buyer_email:
            verified_fields.append("buyer_email")
        else:
            unavailable_fields.append("buyer_email")

        if order_id:
            verified_fields.append("metadata.order_id")
        if error_code:
            verified_fields.append("metadata.error_code")
        if pending_reason:
            verified_fields.append("metadata.pending_reason")
        if expected_ship:
            verified_fields.append("metadata.expected_ship")

        if timeline_events:
            verified_fields.append(f"timeline_events({len(timeline_events)})")
        else:
            unavailable_fields.append("timeline_events")

        if duplicate_candidates:
            verified_fields.append(f"duplicate_candidates({len(duplicate_candidates)})")
        if disputes:
            verified_fields.append(f"disputes({len(disputes)})")

        stale_events = [
            ev
            for ev in timeline_events
            if (ev.get("event_type") or "").upper() == "WEBHOOK_STALE_IGNORED"
        ]
        if stale_events:
            verified_fields.append(f"stale_webhook_events({len(stale_events)})")

        active_dups = [d for d in duplicate_candidates if d["classification"] == "POSSIBLE_DUPLICATE"]
        total_refunded, remaining, refund_entries = (
            get_refund_summary(amount, status, metadata)
            if has_amount
            else (0.0, 0.0, list(metadata.get("refunds") or []))
        )
        refund_ids = [r.get("refund_id") for r in refund_entries if r.get("refund_id")]
        if not refund_ids and metadata.get("refund_id"):
            refund_ids = [metadata["refund_id"]]
        if refund_entries or metadata.get("total_refunded") is not None or metadata.get("refund_id"):
            verified_fields.append("metadata.refunds")

        if status == "COMPLETED":
            is_partial = bool(metadata.get("partial_refund")) or (has_amount and 0 < total_refunded < amount - 0.005)
            if is_partial and total_refunded > 0:
                headline = "Payment captured with partial refund recorded"
                summary = (
                    f"Transaction {pp_id} for {amount_phrase} is COMPLETED with "
                    f"{total_refunded:.2f} {currency} partially refunded "
                    f"({remaining:.2f} {currency} remaining refundable)."
                )
                causes = [
                    f"Capture status is COMPLETED; recorded refund(s) total {total_refunded:.2f} {currency}"
                    + (f" (refund ID(s): {', '.join(refund_ids)})." if refund_ids else ".")
                ]
                actions = [
                    f"Only up to {remaining:.2f} {currency} remains refundable on this capture.",
                    "Review timeline events for confirmed partial refund IDs before issuing another refund.",
                ]
            else:
                headline = "Payment captured and settled"
                summary = (
                    f"Transaction {pp_id} for {amount_phrase} is recorded as COMPLETED"
                    + (f" for order {order_id}." if order_id else ".")
                )
                causes = (
                    ["Stored transaction status is COMPLETED and payment timeline events are recorded."]
                    if timeline_events
                    else ["Stored transaction status is COMPLETED (no payment timeline events recorded yet)."]
                )
                actions = [
                    "No payment recovery needed unless buyer reports an issue.",
                    "Issue a refund from this screen if order cancellation or duplicate charge is confirmed.",
                ]
        elif status == "PENDING":
            headline = "Payment is pending settlement or release"
            pending_details = []
            if pending_reason:
                pending_details.append(f"pending_reason={pending_reason}")
            if expected_ship:
                pending_details.append(f"expected_ship={expected_ship}")
            detail_clause = f" ({', '.join(pending_details)})" if pending_details else ""
            summary = (
                f"Transaction {pp_id} for {amount_phrase} is currently PENDING{detail_clause}. "
                "Payment has not succeeded or settled yet."
            )
            causes = []
            if pending_reason:
                causes.append(f"Recorded pending reason: {pending_reason}.")
            if expected_ship:
                causes.append(f"Deferred fulfillment / expected_ship recorded: {expected_ship}.")
            if not pending_reason and not expected_ship:
                unavailable_fields.append("metadata.pending_reason")
                causes.append("Specific pending reason is unavailable (not recorded in metadata).")
            actions = [
                "Do not fulfill high-value goods until status transitions to COMPLETED.",
                "Monitor incoming PayPal PAYMENT.CAPTURE.COMPLETED webhooks or refresh capture status.",
            ]
        elif status == "FAILED":
            headline = f"Payment failed{f' ({error_code})' if error_code else ''}"
            summary = (
                f"Transaction {pp_id} for {amount_phrase} is marked FAILED"
                + (f" with recorded error_code={error_code}." if error_code else ".")
                + " No funds were settled for this attempt."
            )
            if error_code:
                causes = [f"Recorded processor error code: {error_code}."]
            else:
                unavailable_fields.append("metadata.error_code")
                causes = ["Specific processor failure reason is unavailable (no error_code recorded in metadata)."]
            actions = [
                "Ask the buyer to verify their funding method or retry checkout.",
                "Check if a subsequent COMPLETED transaction exists before asking the buyer to pay again.",
            ]
        elif status == "DISPUTED":
            if disputes:
                dispute_summaries = []
                any_open = any((d.get("status") or "").upper() == "OPEN" for d in disputes)
                headline = (
                    "Payment is on hold due to an open dispute"
                    if any_open
                    else "Payment has an active/recorded dispute"
                )
                for d in disputes:
                    d_ref = d.get("paypal_dispute_id") or d.get("id") or "unnumbered"
                    d_status = (d.get("status") or "UNKNOWN").upper()
                    d_reason = d.get("reason") or "UNSPECIFIED"
                    ev_text = "evidence submitted" if d.get("evidence_submitted") else "evidence not submitted"
                    res_text = f", outcome={d['resolution']}" if d.get("resolution") else ""
                    dispute_summaries.append(f"{d_ref} [{d_status}, {d_reason}, {ev_text}{res_text}]")
                joined = "; ".join(dispute_summaries)
                summary = (
                    f"Transaction {pp_id} for {amount_phrase} is DISPUTED "
                    f"({len(disputes)} linked dispute record(s): {joined})."
                )
                causes = [f"Linked dispute record(s): {joined}."]
            else:
                unavailable_fields.append("dispute_records")
                headline = "Payment is marked DISPUTED (dispute details unavailable)"
                summary = (
                    f"Transaction {pp_id} for {amount_phrase} is marked DISPUTED, "
                    "but linked dispute status, reason, and evidence records are unavailable in the local database."
                )
                causes = ["Transaction status is DISPUTED, while specific dispute details are unavailable locally."]
            actions = [
                "Review linked dispute status and evidence state before shipping or issuing an ad-hoc refund.",
                "Respond via the Disputes tab (submit tracking/evidence or accept claim).",
            ]
        elif status == "REFUNDED":
            headline = "Payment has been refunded"
            refund_desc = (
                f"{total_refunded:.2f} {currency} refunded; {remaining:.2f} {currency} remaining refundable"
                if has_amount
                else "refunded amount unavailable because original transaction amount is missing"
            )
            id_clause = f" (refund ID(s): {', '.join(refund_ids)})" if refund_ids else ""
            summary = f"Transaction {pp_id} for {amount_phrase} is REFUNDED ({refund_desc}){id_clause}."
            causes = []
            if refund_ids or any(
                (ev.get("event_type") or "").upper() == "REFUND_ISSUED" for ev in timeline_events
            ):
                causes.append(
                    f"Refund/reversal recorded for {total_refunded:.2f} {currency}"
                    + (f" under ID(s) {', '.join(refund_ids)}." if refund_ids else ".")
                )
            else:
                unavailable_fields.append("refund_reference")
                causes.append("Transaction is marked REFUNDED, though specific refund ID/timeline reference is unavailable.")
            actions = [
                "Provide the verified refund timeline and ID (if recorded) to the buyer if they ask about posting times.",
            ]
        else:
            headline = f"Recorded status: {status}"
            summary = f"Transaction {pp_id} has recorded status {status} for {amount_phrase}."
            causes = (
                ["Status recorded directly from database/API."]
                if status != "UNKNOWN"
                else ["Transaction status is unavailable/UNKNOWN in the local record."]
            )
            actions = ["Inspect raw metadata and PayPal lookup for additional details."]

        if stale_events:
            latest_stale = stale_events[-1].get("description") or stale_events[-1].get("title") or ""
            summary += (
                f" Note: {len(stale_events)} older/out-of-order webhook event(s) were ignored "
                f"to preserve the newer {status} status."
            )
            causes.append(
                f"{len(stale_events)} older/out-of-order webhook event(s) ignored"
                + (f" (latest: {latest_stale})" if latest_stale else ".")
            )

        if active_dups:
            summary += (
                f" Warning: {len(active_dups)} possible duplicate payment(s) detected for the same buyer and amount."
            )
            actions.insert(
                0,
                f"Review {len(active_dups)} possible duplicate charge(s) below before fulfilling or closing ticket.",
            )

        # Deduplicate unavailable_fields preserving order and explicitly disclose missing data
        dedup_unavailable = list(dict.fromkeys(unavailable_fields))
        if dedup_unavailable:
            summary += f" Unavailable record data: {', '.join(dedup_unavailable)}."
            if any(f in {"paypal_transaction_id", "amount", "status", "dispute_records", "timeline_events"} for f in dedup_unavailable):
                actions.append(
                    f"Verify missing data ({', '.join(dedup_unavailable)}) via PayPal API lookup before final action."
                )

        source_evidence = {
            "paypal_transaction_id": pp_id if raw_pp_id else None,
            "status": status,
            "amount": amount if has_amount else None,
            "currency": currency or None,
            "order_id": order_id,
            "pending_reason": pending_reason,
            "error_code": error_code,
            "buyer_email_recorded": bool(buyer_email),
            "total_refunded": total_refunded if (has_amount and (total_refunded > 0 or status == "REFUNDED")) else 0.0,
            "remaining_refundable": remaining if has_amount else None,
            "partial_refund": bool(
                status == "COMPLETED" and has_amount and 0 < total_refunded < amount - 0.005
            ),
            "refund_ids": refund_ids,
            "disputes": [
                {
                    "id": d.get("paypal_dispute_id") or d.get("id"),
                    "status": (d.get("status") or "UNKNOWN").upper(),
                    "reason": d.get("reason") or "UNSPECIFIED",
                    "evidence_submitted": bool(d.get("evidence_submitted")),
                    "resolution": d.get("resolution"),
                }
                for d in disputes
            ],
            "timeline_event_count": len(timeline_events),
            "stale_webhooks_ignored": len(stale_events),
            "possible_duplicate_count": len(active_dups),
            "unavailable_fields": dedup_unavailable,
            "verified_fields_used": verified_fields,
        }

        return {
            "transaction_id": txn_id,
            "paypal_transaction_id": pp_id,
            "status": status,
            "headline": headline,
            "plain_english_summary": summary,
            "likely_causes": causes,
            "recommended_actions": actions,
            "verified_fields_used": verified_fields,
            "source_evidence": source_evidence,
            "mode": "heuristic",
            "data_source": "database",
        }

    # ------------------------------------------------------------------
    # Secondary Dispute Priority & SLA Methods
    # ------------------------------------------------------------------

    def compute_dispute_priority(self, dispute, transaction) -> Priority:
        reason = (_get_attr(dispute, "reason", "") or "").upper()
        created_at = _parse_dt(_get_attr(dispute, "created_at"))
        dispute_status = (_get_attr(dispute, "status", "OPEN") or "OPEN").upper()
        amount = float(_get_attr(transaction, "amount", 0) or 0)

        if dispute_status in {"RESOLVED", "CLOSED"}:
            return Priority.LOW
        if reason in _HIGH_PRIORITY_REASONS:
            return Priority.CRITICAL

        age_hours = 0.0
        if created_at:
            age_hours = (datetime.utcnow() - created_at).total_seconds() / 3600

        amount_score = 4 if amount >= 1000 else 3 if amount >= 500 else 2 if amount >= 100 else 1
        age_score = 4 if age_hours >= 120 else 3 if age_hours >= 48 else 2 if age_hours >= 24 else 1
        reason_score = 4 if reason in _HIGH_PRIORITY_REASONS else 2 if reason in _MEDIUM_PRIORITY_REASONS else 1

        total = amount_score + age_score + reason_score
        if total >= 10:
            return Priority.CRITICAL
        if total >= 7:
            return Priority.HIGH
        if total >= 4:
            return Priority.MEDIUM
        return Priority.LOW

    def get_next_actions(self, dispute_status: str, resolution_case=None) -> list[str]:
        status = (dispute_status or "OPEN").upper()
        actions = list(_NEXT_ACTIONS.get(status, _NEXT_ACTIONS["OPEN"]))

        if resolution_case is not None:
            dispute = getattr(resolution_case, "dispute", None)
            if dispute and getattr(dispute, "evidence_submitted", False):
                actions = [a for a in actions if "evidence" not in a.lower()]

            breached, time_remaining = self.check_sla_breach(resolution_case)
            if breached:
                actions.insert(0, "SLA BREACHED - Immediate escalation required")
            elif time_remaining and "hours" in time_remaining:
                try:
                    hrs = float(time_remaining.split()[0])
                    if hrs < 4:
                        actions.insert(0, f"SLA breach imminent ({time_remaining} remaining)")
                except (ValueError, IndexError):
                    pass

        return actions

    def update_case_status(self, case, new_status: str, notes: str) -> dict:
        old_status = getattr(case, "status", None) or "UNKNOWN"
        now = datetime.utcnow()
        return {
            "timestamp": now.isoformat() + "Z",
            "action": "STATUS_CHANGE",
            "from_status": old_status,
            "to_status": new_status,
            "notes": notes,
            "agent": getattr(case, "assigned_agent", "system"),
        }

    def check_sla_breach(self, case) -> tuple[bool, str]:
        priority_str = (getattr(case, "priority", None) or "MEDIUM").upper()
        created_at = _parse_dt(getattr(case, "created_at", None))
        outcome = getattr(case, "outcome", None)

        try:
            priority = Priority(priority_str)
        except ValueError:
            priority = Priority.MEDIUM

        if outcome and outcome.upper() in {"RESOLVED", "CLOSED", "REFUNDED"}:
            return False, "Completed"

        if created_at is None:
            return False, "Unknown (no creation timestamp)"

        deadline = created_at + timedelta(hours=SLA_HOURS[priority])
        delta = deadline - datetime.utcnow()
        total_seconds = int(delta.total_seconds())

        if total_seconds < 0:
            overdue_seconds = abs(total_seconds)
            hours, remainder = divmod(overdue_seconds, 3600)
            minutes = remainder // 60
            return True, f"{hours} hours {minutes} minutes overdue"

        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return False, f"{hours} hours {minutes} minutes remaining"

    def get_sla_deadline(self, case) -> Optional[datetime]:
        priority_str = (getattr(case, "priority", None) or "MEDIUM").upper()
        created_at = _parse_dt(getattr(case, "created_at", None))
        try:
            priority = Priority(priority_str)
        except ValueError:
            priority = Priority.MEDIUM
        if created_at is None:
            return None
        return created_at + timedelta(hours=SLA_HOURS[priority])
