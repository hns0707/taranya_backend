"""
Mart2Meta WhatsApp template messaging service.

Usage:
    from shared.services.whatsapp_service import send_whatsapp_template, format_template_parameters
    send_whatsapp_template(
        phone="919876543210",
        template_name="testtemplate",
        parameters=["INV/00001", "Rs.5000"],
        media_type="document",
        media_url="https://...",
    )
"""
import logging
from typing import Optional, Sequence, Union

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

MART2META_URL = "https://login.mart2meta.com/api/{uid}/contact/send-template"


class WhatsAppAPIError(Exception):
    """Raised when Mart2Meta returns a non-2xx response."""

    def __init__(self, status_code: int, response_text: str, payload: Optional[dict] = None):
        self.status_code = status_code
        self.response_text = response_text
        self.payload = payload or {}
        super().__init__(f"WhatsApp API {status_code}: {response_text[:500]}")


def format_template_parameters(values: Sequence[str]) -> str:
    """
    Mart2Meta expects body variables as: {value1},{value2}

    Docs example: "parameters": "{1},{2}"
    Meta error 132000 occurs when the BSP only receives 1 localizable_param.
    """
    parts = []
    for raw in values:
        text = str(raw).strip()
        # Avoid commas inside a value — Mart2Meta splits on "},{" boundaries after braces
        text = text.replace(",", " ")
        # Strip accidental wrapping braces
        if text.startswith("{") and text.endswith("}"):
            text = text[1:-1]
        parts.append(f"{{{text}}}")
    return ",".join(parts)


def send_whatsapp_template(
    phone: str,
    template_name: Optional[str] = None,
    template_language: Optional[str] = None,
    parameters: Union[str, Sequence[str], None] = None,
    media_type: Optional[str] = None,
    media_url: Optional[str] = None,
) -> dict:
    """
    Send a WhatsApp template message via Mart2Meta.

    Args:
        phone:             Recipient number with country code, no + or 0 prefix.
        template_name:     WhatsApp template name (defaults to WHATSAPP_TEMPLATE_INVOICE).
        template_language: Template language code (defaults to en_US).
        parameters:        List of body vars OR already-formatted "{a},{b}" string.
        media_type:        "image" | "document" | "video" (required for media-header templates).
        media_url:         Publicly accessible URL for the media.

    Returns:
        dict from Mart2Meta API response.

    Raises:
        WhatsAppAPIError if the API returns a non-2xx status.
    """
    uid = getattr(settings, "WHATSAPP_UID", None)
    phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", None)
    api_token = getattr(settings, "WHATSAPP_API_TOKEN", None)

    if not all([uid, phone_number_id, api_token]):
        logger.warning("WhatsApp settings not configured (WHATSAPP_UID/PHONE_NUMBER_ID/API_TOKEN).")
        return {"status": "skipped", "reason": "not_configured"}

    template_name = template_name or getattr(settings, "WHATSAPP_TEMPLATE_INVOICE", "testtemplate")
    template_language = template_language or getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en_US")

    # Normalise phone: strip + and leading 0; pad India country code for 10-digit mobiles
    phone = str(phone).strip().lstrip("+").lstrip("0")
    if len(phone) == 10:
        phone = "91" + phone

    if isinstance(parameters, (list, tuple)):
        parameters_str = format_template_parameters(parameters)
    else:
        parameters_str = (parameters or "").strip()
        # If caller passed plain "a,b" without braces, wrap each segment
        if parameters_str and "{" not in parameters_str:
            parameters_str = format_template_parameters(
                [p.strip() for p in parameters_str.split(",") if p.strip()]
            )

    payload = {
        "from_phone_number_id": str(phone_number_id),
        "phone_number": phone,
        "template_name": template_name,
        "template_language": template_language,
    }
    if media_type and media_url:
        payload["template_media_type"] = media_type
        payload["url"] = media_url
    if parameters_str:
        payload["parameters"] = parameters_str

    url = MART2META_URL.format(uid=uid)
    logger.info(
        "WhatsApp send-template → phone=%s template=%s lang=%s media=%s params=%s",
        phone,
        template_name,
        template_language,
        media_type or "-",
        parameters_str,
    )
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if not resp.ok:
            logger.error(
                "WhatsApp API HTTP error %s: %s | payload=%s",
                resp.status_code,
                resp.text[:800],
                payload,
            )
            raise WhatsAppAPIError(resp.status_code, resp.text, payload)
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok", "raw": resp.text}
    except WhatsAppAPIError:
        raise
    except Exception as exc:
        logger.error("WhatsApp send failed: %s", exc)
        raise
