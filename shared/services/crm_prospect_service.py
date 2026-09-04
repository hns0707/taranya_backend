"""CRM phone-diary prospect contacts + mobile suppression helpers."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from django.db.models import Q
from django.utils import timezone

from shared.models import CrmProspectContact, Customer


def normalize_mobile(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def find_customer_by_mobile(normalized: str) -> Optional[Customer]:
    if not normalized or len(normalized) < 10:
        return None
    return (
        Customer.objects.filter(
            Q(mobile__endswith=normalized) | Q(mobile=normalized)
        )
        .order_by("id")
        .first()
    )


def prior_contacts_for_mobile(normalized: str, limit: int = 10):
    if not normalized:
        return CrmProspectContact.objects.none()
    return (
        CrmProspectContact.objects.filter(mobile_normalized=normalized)
        .select_related("branch", "created_by", "matched_customer")
        .order_by("-contacted_at")[:limit]
    )


def serialize_prospect(row: CrmProspectContact) -> dict:
    handler = None
    if row.created_by_id:
        handler = getattr(row.created_by, "full_name", None) or getattr(
            row.created_by, "username", None
        )
    return {
        "id": row.id,
        "name": row.name,
        "mobile": row.mobile,
        "mobile_normalized": row.mobile_normalized,
        "branch_id": row.branch_id,
        "branch_name": row.branch.name if row.branch_id and row.branch else None,
        "campaign_name": row.campaign_name or "",
        "channel": row.channel,
        "outcome": row.outcome,
        "notes": row.notes or "",
        "contacted_at": row.contacted_at.isoformat() if row.contacted_at else None,
        "matched_customer_id": row.matched_customer_id,
        "matched_customer_name": (
            row.matched_customer.full_name if row.matched_customer_id and row.matched_customer else None
        ),
        "handled_by": handler,
    }


def create_prospect_contact(
    *,
    name: str,
    mobile: str,
    channel: str = CrmProspectContact.CHANNEL_CALL,
    outcome: str = CrmProspectContact.OUTCOME_OTHER,
    notes: str = "",
    campaign_name: str = "",
    branch_id: int | None = None,
    contacted_at: datetime | None = None,
    admin_user=None,
    allow_existing_customer: bool = False,
) -> tuple[CrmProspectContact | None, dict]:
    """
    Create a prospect log entry.
    Returns (row, meta) where meta may include suppression / customer match info.
    """
    normalized = normalize_mobile(mobile)
    if len(normalized) < 10:
        return None, {"error": "Enter a valid 10-digit mobile number."}

    name = (name or "").strip()
    if not name:
        return None, {"error": "Name is required."}

    matched = find_customer_by_mobile(normalized)
    prior = list(prior_contacts_for_mobile(normalized, limit=5))
    already_contacted = len(prior) > 0

    if matched and not allow_existing_customer:
        return None, {
            "error": "This mobile already belongs to an enrolled customer. Use Customer module instead.",
            "is_existing_customer": True,
            "matched_customer": {
                "id": matched.id,
                "full_name": matched.full_name,
                "customer_code": matched.customer_code,
                "mobile": matched.mobile,
            },
            "already_contacted": already_contacted,
            "prior_contacts": [serialize_prospect(p) for p in prior],
        }

    channel = (channel or CrmProspectContact.CHANNEL_CALL).upper()
    if channel not in {
        CrmProspectContact.CHANNEL_CALL,
        CrmProspectContact.CHANNEL_WHATSAPP,
        CrmProspectContact.CHANNEL_SMS,
    }:
        channel = CrmProspectContact.CHANNEL_CALL

    valid_outcomes = {c[0] for c in CrmProspectContact.OUTCOME_CHOICES}
    if outcome not in valid_outcomes:
        outcome = CrmProspectContact.OUTCOME_OTHER

    row = CrmProspectContact.objects.create(
        name=name,
        mobile=str(mobile).strip(),
        mobile_normalized=normalized,
        branch_id=branch_id,
        campaign_name=(campaign_name or "").strip()[:128],
        channel=channel,
        outcome=outcome,
        notes=(notes or "").strip(),
        contacted_at=contacted_at or timezone.now(),
        matched_customer=matched,
        created_by=admin_user,
        updated_by=admin_user,
    )
    return row, {
        "already_contacted": already_contacted,
        "is_existing_customer": bool(matched),
        "prior_contacts": [serialize_prospect(p) for p in prior],
        "warning": (
            "This number was contacted before — logged again for reference."
            if already_contacted
            else None
        ),
    }
