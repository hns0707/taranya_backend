"""
Payment helper utilities: idempotency, locking, and update+process pattern.
No business logic changes; structural helpers only.
"""
from shared.models import Payment
from shared.services.payment_processor import resolve_payment


def is_payment_already_processed(payment):
    """
    Idempotency check: returns True if payment is already finalized.
    Caller may return early with appropriate response.
    """
    return payment.is_finalized


def get_locked_payment_by_transaction_id(txnid):
    """
    Fetch payment by transaction_id with select_for_update().
    Must be called inside transaction.atomic() by the caller.
    Raises Payment.DoesNotExist if not found.
    """
    return Payment.objects.select_for_update().get(transaction_id=txnid)


def update_status_and_process(payment, field_name, lookup_value):
    """
    Update a single status field, save with update_fields, then run
    """
    setattr(payment, field_name, lookup_value)
    payment.save(update_fields=[field_name])
    resolve_payment(payment)
