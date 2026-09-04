"""
Views for Accounts section: payment transactions list and account-level ledger.
"""
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.decorators import api_view
from django.core.paginator import Paginator
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.dateparse import parse_date


from shared.models import FinancialTransaction, DayBookManualEntry
from shared.services.ledger_service import get_ledger_entries
from shared.services import day_book_service
from master.permissions.permission_checker import admin_auth
from master.permissions.section_auth import (
    ACCOUNTS_DAILY_BOOK_READ_AUTH,
    ACCOUNTS_DAILY_BOOK_WRITE_AUTH,
)
from master.views.ledger_views import _ledger_entry_to_dict, _payment_map_for_ledger_entries


def _parse_book_date(request):
    date_str = request.GET.get('date') or request.data.get('date')
    if date_str:
        selected = parse_date(date_str)
        if selected:
            return selected
    return timezone.localdate()


@method_decorator(admin_auth("CRM_ACCOUNTS_COLLECTION_SUMMARY_VIEW"), name='get')
class AccountsTransactionListView(generics.ListAPIView):
    """
    GET /master/accounts/transactions/
    List all payment transactions (FinancialTransaction) for Accounts → Collections.
    Includes: customer name, scheme, type, amount, date, payment_mode, status.
    Query params: page, page_size, date_from, date_to, status, type (source_type), scheme_id, customer_id.
    """

    def get(self, request, *args, **kwargs):
        queryset = (
            FinancialTransaction.objects
            .select_related('customer', 'customer_scheme', 'customer_scheme__scheme')
            .all()
        )
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        status_filter = request.GET.get('status')
        type_filter = request.GET.get('type')  # source_type
        scheme_id = request.GET.get('scheme_id')
        customer_id = request.GET.get('customer_id')
        if date_from:
            try:
                from django.utils.dateparse import parse_date
                d = parse_date(date_from)
                if d:
                    queryset = queryset.filter(transaction_date__date__gte=d)
            except (TypeError, ValueError):
                pass
        if date_to:
            try:
                from django.utils.dateparse import parse_date
                d = parse_date(date_to)
                if d:
                    queryset = queryset.filter(transaction_date__date__lte=d)
            except (TypeError, ValueError):
                pass
        if status_filter:
            queryset = queryset.filter(status__iexact=status_filter)
        if type_filter:
            queryset = queryset.filter(source_type__iexact=type_filter)
        if scheme_id:
            try:
                queryset = queryset.filter(customer_scheme__scheme_id=int(scheme_id))
            except (TypeError, ValueError):
                pass
        if customer_id:
            try:
                queryset = queryset.filter(customer_id=int(customer_id))
            except (TypeError, ValueError):
                pass
        queryset = queryset.order_by('-transaction_date', '-id')
        page_number = int(request.GET.get('page', 1))
        page_size = min(int(request.GET.get('page_size', 20)), 100)
        paginator = Paginator(queryset, page_size)
        page = paginator.get_page(page_number)
        results = []
        for txn in page:
            scheme_name = None
            if txn.customer_scheme_id and getattr(txn.customer_scheme, 'scheme', None):
                scheme_name = txn.customer_scheme.scheme.scheme_name
            results.append({
                "id": txn.id,
                "customer_name": txn.customer.full_name if txn.customer_id else None,
                "customer_id": txn.customer_id,
                "scheme_id": txn.customer_scheme.scheme_id if txn.customer_scheme_id else None,
                "scheme_name": scheme_name,
                "type": txn.source_type,
                "direction": txn.direction,
                "amount": str(txn.amount) if txn.amount is not None else None,
                "transaction_date": txn.transaction_date.isoformat() if txn.transaction_date else None,
                "payment_mode": txn.payment_mode,
                "status": txn.status,
                "gateway_transaction_id": txn.gateway_transaction_id,
            })
        return Response({
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "results": results,
        })


@method_decorator(admin_auth("CRM_ACCOUNTS_DAILY_LEDGER_VIEW"), name='get')
class AccountsLedgerListView(generics.ListAPIView):
    """
    GET /master/accounts/ledger/
    Account-level (system-wide) ledger. Returns CustomerLedger entries with filters.
    Query params: customer_id, scheme_id, entry_type, date_from, date_to, ordering, page, page_size.
    """

    def get(self, request, *args, **kwargs):
        customer_id = request.GET.get('customer_id')
        scheme_id = request.GET.get('scheme_id')
        entry_type = request.GET.get('entry_type')
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        ordering = request.GET.get('ordering', '-entry_date')
        if customer_id:
            try:
                customer_id = int(customer_id)
            except (TypeError, ValueError):
                customer_id = None
        if scheme_id:
            try:
                scheme_id = int(scheme_id)
            except (TypeError, ValueError):
                scheme_id = None
        from django.utils.dateparse import parse_date
        date_from_parsed = parse_date(date_from) if date_from else None
        date_to_parsed = parse_date(date_to) if date_to else None
        queryset = get_ledger_entries(
            customer_id=customer_id,
            scheme_id=scheme_id,
            entry_type=entry_type or None,
            date_from=date_from_parsed,
            date_to=date_to_parsed,
            ordering=ordering,
        )
        page_number = int(request.GET.get('page', 1))
        page_size = min(int(request.GET.get('page_size', 20)), 100)
        paginator = Paginator(queryset, page_size)
        page = paginator.get_page(page_number)
        ledger_entries = list(page.object_list)
        payment_map = _payment_map_for_ledger_entries(ledger_entries)
        results = [_ledger_entry_to_dict(entry, payment_map) for entry in ledger_entries]
        return Response({
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "current_page": page.number,
            "results": results,
        })


@api_view(['GET'])
@admin_auth(*ACCOUNTS_DAILY_BOOK_READ_AUTH)
def accounts_daily_book(request):
    """
    GET /master/accounts/daily-book/
    Daily Book — cash ledger for a date with Money In / Money Out columns.
    Query params: date (YYYY-MM-DD). Defaults to today.
    """
    selected_date = _parse_book_date(request)
    data = day_book_service.build_day_book(selected_date)
    return Response(data)


@api_view(['PUT', 'PATCH'])
@admin_auth(*ACCOUNTS_DAILY_BOOK_WRITE_AUTH)
def accounts_daily_book_opening(request):
    """
    PUT /master/accounts/daily-book/opening/
    Set manual opening balance for a date.
    Body: { "date": "YYYY-MM-DD", "opening_balance": "12345.67" }
    """
    selected_date = _parse_book_date(request)
    raw_balance = request.data.get('opening_balance')
    if raw_balance is None:
        return Response({'error': 'opening_balance is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        day_book_service.set_opening_balance(
            selected_date,
            raw_balance,
            user=request.user,
        )
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    data = day_book_service.build_day_book(selected_date)
    return Response(data)


@api_view(['POST'])
@admin_auth(*ACCOUNTS_DAILY_BOOK_WRITE_AUTH)
def accounts_daily_book_entry_create(request):
    """
    POST /master/accounts/daily-book/entries/
    Create a manual day book entry.
  """
    entry_date = parse_date(request.data.get('entry_date') or request.data.get('date') or '')
    if not entry_date:
        return Response({'error': 'entry_date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        entry = day_book_service.create_manual_entry(
            entry_date=entry_date,
            direction=str(request.data.get('direction', '')).upper(),
            amount=request.data.get('amount'),
            transaction_mode=str(
                request.data.get('entry_type')
                or request.data.get('transaction_mode', '')
            ).upper(),
            narration=request.data.get('narration', ''),
            payment_mode=str(request.data.get('payment_mode', 'CASH')),
            payment_collections=request.data.get('payment_collections'),
            user=request.user,
        )
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    payments = [
        {'payment_mode': p.payment_mode, 'amount': str(p.amount)}
        for p in entry.payments.all()
    ]
    return Response({
        'id': entry.id,
        'entry_date': entry.entry_date.isoformat(),
        'direction': entry.direction,
        'amount': str(entry.amount),
        'transaction_mode': entry.transaction_mode,
        'entry_type': entry.transaction_mode,
        'payment_mode': entry.payment_mode,
        'payment_collections': payments,
        'narration': entry.narration,
    }, status=status.HTTP_201_CREATED)


@api_view(['PUT', 'PATCH', 'DELETE'])
@admin_auth(*ACCOUNTS_DAILY_BOOK_WRITE_AUTH)
def accounts_daily_book_entry_detail(request, pk: int):
    """
    PUT/PATCH/DELETE /master/accounts/daily-book/entries/<id>/
    Update or soft-delete a manual entry.
    """
    if request.method == 'DELETE':
        try:
            day_book_service.delete_manual_entry(pk, user=request.user)
        except DayBookManualEntry.DoesNotExist:
            return Response({'error': 'Entry not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)

    fields = {}
    if 'amount' in request.data:
        fields['amount'] = request.data['amount']
    if 'direction' in request.data:
        fields['direction'] = str(request.data['direction']).upper()
    if 'transaction_mode' in request.data or 'entry_type' in request.data:
        fields['transaction_mode'] = str(
            request.data.get('entry_type') or request.data.get('transaction_mode', '')
        ).upper()
    if 'payment_mode' in request.data:
        fields['payment_mode'] = str(request.data['payment_mode'])
    if 'payment_collections' in request.data:
        fields['payment_collections'] = request.data['payment_collections']
    if 'narration' in request.data:
        fields['narration'] = request.data['narration']
    if 'entry_date' in request.data:
        parsed = parse_date(request.data['entry_date'])
        if not parsed:
            return Response({'error': 'Invalid entry_date.'}, status=status.HTTP_400_BAD_REQUEST)
        fields['entry_date'] = parsed

    try:
        entry = day_book_service.update_manual_entry(pk, user=request.user, **fields)
    except DayBookManualEntry.DoesNotExist:
        return Response({'error': 'Entry not found.'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    payments = [
        {'payment_mode': p.payment_mode, 'amount': str(p.amount)}
        for p in entry.payments.all()
    ]
    return Response({
        'id': entry.id,
        'entry_date': entry.entry_date.isoformat(),
        'direction': entry.direction,
        'amount': str(entry.amount),
        'transaction_mode': entry.transaction_mode,
        'entry_type': entry.transaction_mode,
        'payment_mode': entry.payment_mode,
        'payment_collections': payments,
        'narration': entry.narration,
    })


@api_view(['GET'])
@admin_auth(*ACCOUNTS_DAILY_BOOK_READ_AUTH)
def accounts_daily_book_print(request):
    """
    GET /master/accounts/daily-book/print/
    Printable payload for one day or a date range (batch).
    Query params: date OR date_from + date_to
    """
    date_from_str = request.GET.get('date_from') or request.GET.get('date')
    date_to_str = request.GET.get('date_to') or request.GET.get('date')

    if not date_from_str:
        return Response({'error': 'date or date_from is required.'}, status=status.HTTP_400_BAD_REQUEST)

    date_from = parse_date(date_from_str)
    if not date_from:
        return Response({'error': 'Invalid date.'}, status=status.HTTP_400_BAD_REQUEST)

    if date_to_str and date_to_str != date_from_str:
        date_to = parse_date(date_to_str)
        if not date_to:
            return Response({'error': 'Invalid date_to.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pages = day_book_service.build_day_book_batch_print(date_from, date_to)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'pages': pages, 'count': len(pages)})

    return Response(day_book_service.build_day_book_print_payload(date_from))
