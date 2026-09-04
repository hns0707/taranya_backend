from decimal import Decimal
from django.utils import timezone
from shared.models import CustomerLedger


def create_ledger_entry(
    customer,
    customer_scheme,
    entry_type,
    value_type,
    amount=Decimal('0.00'),
    gold_grams=Decimal('0.0000'),
    silver_grams=Decimal('0.0000'),
    reference_type=None,
    reference_id=None,
    description="",
):

    last = CustomerLedger.objects.filter(
        customer=customer,
        customer_scheme=customer_scheme
    ).order_by('-id').first()

    prev_cash = last.running_balance if last else Decimal('0.00')
    prev_gold = last.running_gold_balance if last else Decimal('0.0000')
    prev_silver = last.running_silver_balance if last else Decimal('0.0000')

    new_cash = prev_cash
    new_gold = prev_gold
    new_silver = prev_silver

    # ---------------- CASH ----------------
    if value_type == "CASH":
        if entry_type == "CREDIT":
            new_cash += amount
        else:
            new_cash -= amount

    # ---------------- GOLD ----------------
    elif value_type == "GOLD":
        if entry_type == "CREDIT":
            new_gold += gold_grams
        else:
            new_gold -= gold_grams

    # ---------------- SILVER ----------------
    elif value_type == "SILVER":
        if entry_type == "CREDIT":
            new_silver += silver_grams
        else:
            new_silver -= silver_grams

    return CustomerLedger.objects.create(
        customer=customer,
        customer_scheme=customer_scheme,

        entry_type=entry_type,
        value_type=value_type,

        amount=amount,
        gold_grams=gold_grams,
        silver_grams=silver_grams,

        running_balance=new_cash,
        running_gold_balance=new_gold,
        running_silver_balance=new_silver,

        reference_type=reference_type,
        reference_id=reference_id,

        entry_date=timezone.now(),
        description=description,
    )
