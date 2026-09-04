import re
import secrets
from datetime import datetime
from typing import Any, Optional, Tuple

from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction

from shared.models import Customer


MAX_CUSTOMER_CODE_RETRIES = 10


def normalize_mobile(mobile: Any) -> str:
    value = str(mobile or "").strip()
    digits = re.sub(r"\D", "", value)
    return digits or value


def _name_part(full_name: Optional[str]) -> str:
    if full_name:
        clean = re.sub(r"[^A-Za-z]", "", full_name)
        return clean[:3].upper().ljust(3, "X")
    return "CUS"


def _dob_part(date_of_birth: Any) -> str:
    if not date_of_birth:
        return "000000"
    try:
        if isinstance(date_of_birth, str):
            dob_obj = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
        else:
            dob_obj = date_of_birth
        return dob_obj.strftime("%d%m%y")
    except Exception:
        return "000000"


def generate_customer_code(full_name: Optional[str], date_of_birth: Any = None) -> str:
    prefix = f"{_name_part(full_name)}{_dob_part(date_of_birth)}"
    random_part = "".join(secrets.choice("0123456789") for _ in range(4))
    return f"{prefix}-{random_part}"


@transaction.atomic
def get_or_create_customer(
    mobile: Any,
    full_name: Optional[str] = None,
    optional_fields: Optional[dict] = None,
) -> Tuple[Customer, bool]:
    """
    Central customer creation flow.
    - Reuses customer by normalized mobile when present.
    - Creates new customer with generated customer_code when absent.
    - Uses transaction + row locking + retry for customer_code uniqueness races.
    """
    mobile_normalized = normalize_mobile(mobile)
    if not mobile_normalized:
        raise ValueError("mobile is required")

    fields = dict(optional_fields or {})
    resolved_name = (full_name or fields.pop("full_name", None) or "").strip()
    if not resolved_name:
        resolved_name = "Customer"

    # Lock candidates for this mobile to avoid parallel duplicate creation.
    existing_qs = Customer.objects.select_for_update().filter(mobile=mobile_normalized).order_by("id")
    existing = existing_qs.first()
    if existing:
        changed = False
        if resolved_name and (not existing.full_name or existing.full_name.strip() in ("", "Customer")):
            existing.full_name = resolved_name
            changed = True
        for key, value in fields.items():
            if value is None:
                continue
            current = getattr(existing, key, None)
            if current in (None, ""):
                setattr(existing, key, value)
                changed = True
        if not existing.customer_code:
            existing.customer_code = generate_customer_code(existing.full_name, existing.date_of_birth)
            changed = True
        if changed:
            existing.save()
        return existing, False

    payload = {
        "mobile": mobile_normalized,
        "full_name": resolved_name,
        "is_active": fields.pop("is_active", True),
    }
    payload.update(fields)

    # Retry on unique collisions (customer_code and/or mobile race).
    for _ in range(MAX_CUSTOMER_CODE_RETRIES):
        payload["customer_code"] = generate_customer_code(
            payload.get("full_name"),
            payload.get("date_of_birth"),
        )
        try:
            customer = Customer.objects.create(**payload)
            return customer, True
        except IntegrityError:
            existing_after_race = Customer.objects.select_for_update().filter(mobile=mobile_normalized).order_by("id").first()
            if existing_after_race:
                if not existing_after_race.customer_code:
                    existing_after_race.customer_code = generate_customer_code(
                        existing_after_race.full_name,
                        existing_after_race.date_of_birth,
                    )
                    existing_after_race.save(update_fields=["customer_code", "system_updated_at"])
                return existing_after_race, False

    raise IntegrityError("Unable to generate unique customer_code after retries")


def ensure_customer_password(customer: Customer, raw_password: str) -> None:
    customer.password_hash = make_password(raw_password)
    customer.save(update_fields=["password_hash", "system_updated_at"])
