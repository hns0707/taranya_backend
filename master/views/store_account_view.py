"""
Customer store account (JAMA advance / UDHAR outstanding) for catalogue POS.
"""

from decimal import Decimal

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from master.permissions.permission_checker import admin_auth
from master.permissions.section_auth import STORE_POS_READ_AUTH, STORE_POS_WRITE_AUTH
from shared.models import Customer
from shared.services.customer_store_account_service import (
    get_customer_store_balance,
    record_store_advance,
)
from shared.services.customer_scheme_redeem_service import get_customer_scheme_redeem_options
from shared.services.sms_service import send_advance_receipt_sms


@api_view(['GET'])
@admin_auth(*STORE_POS_READ_AUTH)
def customer_store_balance(request, pk: int):
    """
    GET /master/customer/<pk>/store-balance/
  Positive signed_balance = JAMA (advance available).
  Negative signed_balance = UDHAR (customer owes).
    """
    if not Customer.objects.filter(pk=pk).exists():
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)
    return Response(get_customer_store_balance(pk))


@api_view(['POST'])
@admin_auth(*STORE_POS_WRITE_AUTH)
def customer_store_advance(request, pk: int):
    """
    POST /master/customer/<pk>/store-advance/
    Body: { amount, mode?, remark? }
    """
    if not Customer.objects.filter(pk=pk).exists():
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)

    data = request.data or {}
    try:
        amount = Decimal(str(data.get('amount', 0)))
    except Exception:
        return Response({'error': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        result = record_store_advance(
            pk,
            amount,
            mode_code=str(data.get('mode') or data.get('mode_code') or 'CASH'),
            remark=str(data.get('remark') or data.get('notes') or ''),
        )
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    send_sms = data.get('send_sms', True)
    if send_sms is not False and send_sms != 'false':
        customer = Customer.objects.get(pk=pk)
        if customer.mobile:
            receipt_no = f"ADV-{result.get('ledger_id', '')}"
            try:
                send_advance_receipt_sms(
                    mobile=customer.mobile,
                    customer_name=customer.full_name or 'Customer',
                    amount=amount,
                    receipt_number=receipt_no,
                )
                result['sms_sent'] = True
            except Exception:
                result['sms_sent'] = False
        else:
            result['sms_sent'] = False

    return Response(result, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@admin_auth(*STORE_POS_READ_AUTH)
def customer_scheme_redeem_options(request, pk: int):
    """
    GET /master/customer/<pk>/scheme-redeem-options/
    Active savings schemes with cash available for kitty redeem on catalogue bills.
    """
    if not Customer.objects.filter(pk=pk).exists():
        return Response({'error': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)
    return Response(get_customer_scheme_redeem_options(pk))
