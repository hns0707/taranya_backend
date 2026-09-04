"""
otpmsg.in DLT SMS — Taranya Jewels.

One public helper per approved DLT template. All low-level HTTP details stay
inside send_dlt_sms / _encode_otpmsg_query so callers only pass text + template-id.
"""
from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Optional, Tuple
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

OTPMSG_SEND_URL = "https://otpmsg.in//api/mt/SendSMS"
OTPMSG_DELIVERY_URL = "https://otpmsg.in//api/mt/GetDelivery"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_inr(amount) -> str:
    """Indian comma-formatted number (e.g. 1,23,456)."""
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _format_dlt_num(amount) -> str:
    """Plain digits for DLT {#num#} slots — no thousand separators."""
    value = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value:.2f}"


def _format_date(d=None) -> str:
    dt = d or timezone.localdate()
    return dt.strftime("%d-%b-%Y") if hasattr(dt, "strftime") else str(dt)


def normalise_phone(phone: str) -> str:
    """Return 91-prefixed 12-digit mobile number."""
    p = (phone or "").strip().lstrip("+").lstrip("0")
    return ("91" + p) if (len(p) == 10 and p.isdigit()) else p


def _msisdn(phone: str) -> str:
    """otpmsg expects 10-digit numbers (no country code)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


# ---------------------------------------------------------------------------
# otpmsg HTTP helpers
# ---------------------------------------------------------------------------

def _credentials() -> tuple[str, str, str]:
    api_key  = (getattr(settings, "OTPMSG_API_KEY",  None) or "").strip()
    user     = (getattr(settings, "OTPMSG_USER",     None) or "").strip()
    password = (getattr(settings, "OTPMSG_PASSWORD", None) or "").strip()
    return api_key, user, password


def _build_pairs(*, number: str, text: str, dlt_template_id: str) -> list[tuple[str, str]]:
    """Build ordered query pairs matching the working otpmsg demo URL."""
    api_key, user, password = _credentials()
    sender   = (getattr(settings, "OTPMSG_SENDER_ID", None) or "").strip()
    route    = str(getattr(settings, "OTPMSG_ROUTE", 1))
    entity   = (getattr(settings, "OTPMSG_ENTITY_ID", None) or "").strip()

    pairs: list[tuple[str, str]] = []
    if api_key:
        pairs.append(("apikey", api_key))
    else:
        pairs += [("user", user), ("password", password)]

    pairs += [
        ("senderid",      sender),
        ("channel",       "Trans"),
        ("DCS",           "8"),
        ("flashsms",      "0"),
        ("number",        number),
        ("text",          text),
        ("route",         route),
        ("DLTTemplateId", dlt_template_id),
    ]
    if entity:
        pairs.append(("EntityId", entity))

    return [(k, v) for k, v in pairs if v not in (None, "")]


def _encode(pairs: list[tuple[str, str]]) -> str:
    """URL-encode pairs, keeping DLT-safe chars (: / ' etc.) unencoded in `text`."""
    parts = []
    for key, value in pairs:
        safe = " :/'-,._" if key == "text" else ""
        parts.append(f"{key}={quote(str(value), safe=safe)}")
    return "&".join(parts)


def _is_ok(response: requests.Response) -> bool:
    if response.status_code != 200:
        return False
    body = (response.text or "").strip()
    if not body:
        return True
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        code = str(data.get("ErrorCode") or data.get("errorcode") or "").strip()
        if code in {"000", "0"}:
            return True
        if code:
            return False
        if data.get("JobId") or data.get("MessageData"):
            return True
    return bool(re.search(r'"errorcode"\s*:\s*"0+"', body.lower()))


def _delivery_status(job_id: str) -> str:
    api_key, user, password = _credentials()
    pairs = (
        [("apikey", api_key), ("jobid", job_id)] if api_key else
        [("user", user), ("password", password), ("jobid", job_id)]
    )
    url = f"{OTPMSG_DELIVERY_URL}?{_encode(pairs)}"
    print('url',url)
    try:
        return (requests.get(url, timeout=15).text or "")[:400]
    except Exception as exc:
        return f"dlr_error:{exc}"


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

def send_dlt_sms(*, number: str, text: str, dlt_template_id: str) -> Tuple[bool, str]:
    """Send one DLT SMS. Returns (success, raw_api_response)."""
    if not number or not text:
        return False, "missing number or text"
    if not dlt_template_id:
        return False, "DLT template id not configured"

    api_key, user, password = _credentials()
    if not api_key and not (user and password):
        logger.error("otpmsg credentials missing — set OTPMSG_API_KEY in .env")
        return False, "otpmsg credentials missing"

    phone = _msisdn(number)
    if len(phone) < 10:
        return False, "invalid mobile number"

    url = f"{OTPMSG_SEND_URL}?{_encode(_build_pairs(number=phone, text=text, dlt_template_id=dlt_template_id))}"
    logger.info("SMS send last4=%s template=%s len=%s", phone[-4:], dlt_template_id, len(text))
    try:
        print('url', url)
        resp = requests.get(url, timeout=15)
        print('resp', resp)
        ok   = _is_ok(resp)
        detail = (resp.text or "")[:500]
        if ok:
            job_id = ""
            try:
                job_id = str((resp.json() or {}).get("JobId") or "")
            except Exception:
                pass
            if job_id:
                time.sleep(2)
                dlr = _delivery_status(job_id)
                detail = f"{detail} | DLR={dlr}"
                if any(t in dlr.lower() for t in ('"failed"', "expired", "ndnc", "rejected", "undeliver")):
                    ok = False
                    logger.warning("SMS not delivered last4=%s dlr=%s", phone[-4:], dlr[:200])
        else:
            logger.warning("SMS rejected last4=%s resp=%s", phone[-4:], detail[:200])
        return ok, detail
    except Exception as exc:
        logger.exception("SMS send error last4=%s", phone[-4:])
        return False, str(exc)


# ---------------------------------------------------------------------------
# One function per DLT template
# ---------------------------------------------------------------------------

def send_login_otp(mobile: str, otp: str) -> Tuple[bool, str]:
    text = (
        f"Use OTP {otp} to securely log in to your Taranya Jewels account. "
        f"This OTP is valid for 10 minutes. Please do not share it with anyone. "
        f"Visit: https://oneashish.in/home"
    )
    return send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_LOGIN_OTP)


def send_scheme_due_reminder_sms(*, mobile: str, customer_name: str, amount, due_date) -> Tuple[bool, str]:
    text = (
        f"Dear {customer_name}, This is a reminder that your jewellery savings scheme "
        f"installment of Rs {_format_inr(amount)} is due on{_format_date(due_date)}. "
        f"Please ignore this message if you have already made the payment. "
        f"Thank you, TARANYA JEWELS"
    )
    return send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_SCHEME_DUE_REMINDER)


def send_udhar_payment_reminder_sms(*, mobile: str, balance_amount) -> Tuple[bool, str]:
    text = (
        f"A balance of Rs{_format_inr(balance_amount)} is pending in your account. "
        f"Kindly visit our store or contact us for payment. "
        f"Please ignore this message if payment has already been made. "
        f"Thank you,TARANYA JEWELS"
    )
    return send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_UDHAR_REMINDER)


def send_outstanding_balance_payment_sms(
    *, mobile: str, payment_amount, receipt_number: str, remaining_balance
) -> Tuple[bool, str]:
    text = (
        f"We have received your payment of Rs {_format_inr(payment_amount)} against your outstanding balance. "
        f"Receipt Number: {receipt_number} "
        f"Remaining Balance: Rs{_format_inr(remaining_balance)} "
        f"Thank you. TARANYA JEWELS"
    )
    return send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_OUTSTANDING_BALANCE)


def send_advance_receipt_sms(
    *, mobile: str, customer_name: str, amount, receipt_number: str, payment_date=None
) -> Tuple[bool, str]:
    text = (
        f"Dear {customer_name}, We have received your advance payment of Rs {_format_inr(amount)}. "
        f"Receipt Number: {receipt_number} Date: {_format_date(payment_date)} "
        f"Thank you for your trust. TARANYA JEWELS"
    )
    return send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_ADVANCE_RECEIPT)


# ---------------------------------------------------------------------------
# Gold / silver rate helpers
# ---------------------------------------------------------------------------

def get_todays_gold_silver_rates_for_sms(branch_id=None) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    from shared.services.metal_rate_service import (
        get_default_gold_metal_id,
        get_default_silver_metal_id,
        get_metal_master_rate_simple,
        get_metal_rate_by_date,
    )
    today    = timezone.localdate()
    gold_id  = get_default_gold_metal_id()
    silver_id = get_default_silver_metal_id()

    gold_24 = gold_22 = silver = None
    if gold_id:
        r24 = get_metal_rate_by_date(gold_id, today, "24K", branch_id)
        r22 = get_metal_rate_by_date(gold_id, today, "22K", branch_id)
        if r24 and r24.sell_price is not None:
            gold_24 = Decimal(str(r24.sell_price))
        if r22 and r22.sell_price is not None:
            gold_22 = Decimal(str(r22.sell_price))
    if silver_id:
        rs = get_metal_master_rate_simple(silver_id, today)
        if rs and rs.sell_price is not None:
            silver = Decimal(str(rs.sell_price))

    return gold_24, gold_22, silver


def send_gold_rate_sms(
    *, mobile: str, gold_24k_per_g=None, gold_22k_per_g=None, silver_s925_per_g=None, branch_id=None,
) -> Tuple[bool, str, str]:
    if any(v is None for v in (gold_24k_per_g, gold_22k_per_g, silver_s925_per_g)):
        g24, g22, sil = get_todays_gold_silver_rates_for_sms(branch_id=branch_id)
        gold_24k_per_g   = gold_24k_per_g   if gold_24k_per_g   is not None else g24
        gold_22k_per_g   = gold_22k_per_g   if gold_22k_per_g   is not None else g22
        silver_s925_per_g = silver_s925_per_g if silver_s925_per_g is not None else sil

    if any(v is None for v in (gold_24k_per_g, gold_22k_per_g, silver_s925_per_g)):
        return False, "Gold/silver rates not available for today", ""

    # Must match the DLT template exactly (plain apostrophe).
    text = (
        f"Today's Gold Rate: 24K: Rs{_format_dlt_num(gold_24k_per_g)} / gram "
        f"22K: Rs{_format_dlt_num(gold_22k_per_g)} / gram "
        f"Today's Silver Rate: S925 - Rs{_format_dlt_num(silver_s925_per_g)}/ gram "
        f"Thank you, TARANYA JEWELS"
    )
    logger.info("Gold rate SMS text last4=%s: %s", _msisdn(mobile)[-4:], text)
    ok, api_text = send_dlt_sms(number=mobile, text=text, dlt_template_id=settings.SMS_DLT_GOLD_RATE)
    return ok, api_text, text
