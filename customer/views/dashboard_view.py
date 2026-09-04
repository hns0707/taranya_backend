"""
Views for customer dashboard.
"""
from decimal import Decimal

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from django.db.models import Sum
from django.utils import timezone

from shared.models import CustomerScheme, SchemeInstalment, Payment, LookupValue, CustomerLedger, GoldLockingRecord
from customer.auth.customer_auth import CustomerAuthentication


def to_paise(value):
    """
    Safely convert Decimal to paise (int).
    """
    return int(Decimal(value) * 100) if value is not None else 0


@api_view(["GET"])
@authentication_classes([CustomerAuthentication])
@permission_classes([IsAuthenticated])
def customer_dashboard(request):
    """
    API endpoint to fetch complete customer dashboard data with all metrics
    and financial information.

    - All money values returned in paise
    - Fully Decimal-safe
    - Defensive against NULL values
    """
    customer = request.user
    today = timezone.localdate()

    # -----------------------------
    # Fetch active schemes
    # -----------------------------
    try:
        active_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code='ACTIVE')
        paid_inst_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PAID')
        pending_inst_status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code='PENDING')
    except LookupValue.DoesNotExist:
        return Response({"error": "Required status values not found"}, status=status.HTTP_404_NOT_FOUND)

    active_schemes = (
        CustomerScheme.objects
        .filter(customer=customer, scheme_status=active_status)
        .select_related("scheme")
    )

    # -----------------------------
    # Fetch instalments
    # -----------------------------
    all_instalments = (
        SchemeInstalment.objects
        .filter(customer_scheme__in=active_schemes)
        .select_related("customer_scheme__scheme", "status")
    )

    # -----------------------------
    # Summary metrics (from instalment status = PAID, not payment status)
    # -----------------------------
    paid_instalments = all_instalments.filter(status=paid_inst_status)
    paid_amount_agg = paid_instalments.aggregate(total=Sum('amount'))
    total_investment = to_paise(paid_amount_agg['total'] or 0)
    completed_payments = paid_instalments.count()

    pending_payments = all_instalments.filter(status=pending_inst_status).count()

    all_payments = Payment.objects.filter(instalment__in=all_instalments).select_related(
        'payment_status', 'instalment', 'instalment__status'
    )

    # -----------------------------
    # Next payment details
    # -----------------------------
    next_payment = None

    upcoming_instalments = (
        all_instalments
        .filter(status=pending_inst_status, due_date__gte=today)
        .order_by("due_date")
    )

    if upcoming_instalments.exists():
        instalment = upcoming_instalments.first()
        scheme = instalment.customer_scheme.scheme

        next_payment = {
            "instalmentId": instalment.id,
            "customerSchemeId": instalment.customer_scheme_id,
            "dueDate": instalment.due_date.isoformat(),
            "dueAmount": to_paise(instalment.amount),
            "daysUntilDue": (instalment.due_date - today).days,
            "schemeId": scheme.id,
            "schemeName": scheme.scheme_name,
            "installmentNo": instalment.instalment_no,
        }

    # -----------------------------
    # Investment goal calculation
    # -----------------------------
    investment_goal = None

    if active_schemes.exists():
        target_amount = 0

        for cs in active_schemes:
            # Priority 1: total payable amount
            if cs.total_payable_amount is not None:
                target_amount += to_paise(cs.total_payable_amount)

            # Priority 2: monthly * tenure
            elif cs.monthly_amount is not None and cs.tenure_months is not None:
                target_amount += int(
                    Decimal(cs.monthly_amount) *
                    Decimal(cs.tenure_months) *
                    100
                )

            # else: ignore incomplete scheme safely

        percentage_complete = (
            int((total_investment / target_amount) * 100)
            if target_amount > 0 else 0
        )

        investment_goal = {
            "currentAmount": total_investment,
            "targetAmount": target_amount,
            "percentageComplete": percentage_complete,
        }

    # -----------------------------
    # Recent transactions (last 10)
    # -----------------------------
    recent_transactions = []
    try:
        payment_paid_status = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='PAID')
        payment_success_status = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='SUCCESS')
        payment_SUCCESS = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='SUCCESS')
        payment_failed_status = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code='FAILED')
    except LookupValue.DoesNotExist:
        payment_paid_status = payment_success_status = payment_SUCCESS = payment_failed_status = None

    recent_payments = all_payments.order_by("-paid_at")[:10]

    for payment in recent_payments:
        # Success if payment is PAID/SUCCESS/SUCCESS or if linked instalment is PAID
        is_success = (
            payment.payment_status in (payment_paid_status, payment_success_status, payment_SUCCESS)
            or (payment.instalment and payment.instalment.status == paid_inst_status)
        )
        if is_success:
            status_str = "success"
        elif payment.payment_status == payment_failed_status:
            status_str = "failed"
        else:
            status_str = "pending"

        recent_transactions.append({
            "id": str(payment.transaction_id),
            "type": "payment",
            "amount": to_paise(payment.amount),
            "date": payment.paid_at.isoformat() if payment.paid_at else None,
            "schemeName": payment.instalment.customer_scheme.scheme.scheme_name,
            "status": status_str,
        })

    # -----------------------------
    # Ledger-derived investments + ledger timeline
    # -----------------------------
    customer_ledger_qs = CustomerLedger.objects.filter(customer=customer).order_by("-entry_date", "-id")

    total_cash_investment = to_paise(
        customer_ledger_qs.filter(value_type="CASH", entry_type="CREDIT").aggregate(total=Sum("amount"))["total"] or 0
    )
    total_gold_investment_grams = str(
        customer_ledger_qs.filter(value_type="GOLD", entry_type="CREDIT").aggregate(total=Sum("gold_grams"))["total"] or 0
    )

    ledger_transactions = []
    for entry in customer_ledger_qs[:20]:
        ledger_transactions.append({
            "id": str(entry.id),
            "date": entry.entry_date.isoformat() if entry.entry_date else None,
            "narration": entry.description or entry.reference_type or "Ledger entry",
            "entryType": entry.entry_type,
            "valueType": entry.value_type,
            "cashAmount": to_paise(entry.amount) if entry.amount is not None else 0,
            "goldGrams": str(entry.gold_grams or 0),
            "silverGrams": str(entry.silver_grams or 0),
        })

    # -----------------------------
    # Calculate gold from GoldLockingRecord (instead of CustomerScheme fields)
    # -----------------------------
    active_scheme_ids = list(active_schemes.values_list('id', flat=True))
    
    if active_scheme_ids:
        gold_lock_agg = GoldLockingRecord.objects.filter(
            customer_scheme_id__in=active_scheme_ids
        ).aggregate(total_gold=Sum('gold_grams'))
        total_locked_gold = gold_lock_agg['total_gold'] or 0
        accumulated_gold = total_locked_gold  # Gold from payments = accumulated gold
    else:
        total_locked_gold = 0
        accumulated_gold = 0

    # -----------------------------
    # Final response
    # -----------------------------
    response_data = {
        "totalInvestment": total_investment,
        "totalCashInvestment": total_cash_investment,
        "totalGoldInvestmentGrams": total_gold_investment_grams,
        "activeSchemes": active_schemes.count(),
        "pendingPayments": pending_payments,
        "completedPayments": completed_payments,
        "totalLockedGoldGrams": str(total_locked_gold),
        "accumulatedGoldGrams": str(accumulated_gold),
        "nextPayment": next_payment,
        "investmentGoal": investment_goal,
        "recentTransactions": recent_transactions,
        "ledgerTransactions": ledger_transactions,
    }

    return Response(response_data, status=status.HTTP_200_OK)
