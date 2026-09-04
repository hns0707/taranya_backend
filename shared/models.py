"""
Shared models for the eCommerce Jewellery Savings Platform.
"""
from decimal import Decimal

from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class SystemBaseModel(models.Model):
    """
    Abstract base model for system audit fields.
    """
    system_created_at = models.DateTimeField(auto_now_add=True)
    system_updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_%(class)s_set'
    )

    updated_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_%(class)s_set'
    )

    class Meta:
        abstract = True


class Lookup(SystemBaseModel):
    """
    Represents a category/type of master data.
    """
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique uppercase code (e.g., PAYMENT_STATUS)"
    )
    name = models.CharField(
        max_length=100,
        help_text="Display name of the lookup category"
    )
    description = models.TextField(
        null=True,
        blank=True,
        help_text="Detailed description of the lookup category"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this lookup category is active"
    )

    class Meta:
        db_table = 'lookups'
        verbose_name_plural = 'Lookups'

    def __str__(self):
        return f"{self.code} - {self.name}"


class LookupValue(SystemBaseModel):
    """
    Represents actual values under a lookup.
    """
    lookup = models.ForeignKey(
        Lookup,
        on_delete=models.PROTECT,
        related_name='values',
        help_text="Parent lookup category"
    )
    code = models.CharField(
        max_length=50,
        help_text="Unique code per lookup (e.g., PENDING)"
    )
    label = models.CharField(
        max_length=100,
        help_text="Display label for the value"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this lookup value is active"
    )
    sort_order = models.IntegerField(
        default=0,
        help_text="Sort order for display purposes"
    )

    class Meta:
        db_table = 'lookup_values'
        verbose_name_plural = 'Lookup Values'
        unique_together = (('lookup', 'code'),)

    def __str__(self):
        return f"{self.lookup.code}: {self.code} ({self.label})"

class Customer(SystemBaseModel):
    """
    Model for customers.
    """
    GENDER_CHOICES = [
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
    ]

    full_name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=15)
    email = models.EmailField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    anniversary_date = models.DateField(null=True, blank=True)
    wedding_date = models.DateField(null=True, blank=True)
    family_group = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        help_text='Family / household label for CRM grouping.',
    )
    referred_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='referrals',
        help_text='Customer who referred this person (CRM referrals).',
    )
    referral_code = models.CharField(
        max_length=32,
        unique=True,
        null=True,
        blank=True,
        help_text='Optional shareable referral code.',
    )
    password_hash = models.CharField(max_length=255, null=True, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    customer_code = models.CharField(max_length=30, unique=True, null=True, blank=True)
    gst_number = models.CharField(max_length=20, null=True, blank=True)
    aadhaar_number = models.CharField(max_length=12, null=True, blank=True)

    class Meta:
        db_table = 'customers'

    def __str__(self):
        return self.full_name

    @property
    def is_authenticated(self):
        """
        Always returns True. This property is required by Django REST Framework's
        IsAuthenticated permission class to determine if a user is authenticated.
        Since CustomerAuthentication only returns authenticated customers, this
        will always be True for valid Customer instances.
        """
        return True

    @property
    def is_anonymous(self):
        """
        Always returns False. Required to distinguish from AnonymousUser in DRF.
        """
        return False

class CustomerOTP(SystemBaseModel):
    """
    Model for customer OTPs.
    """
    PURPOSE_CHOICES = [
        ('LOGIN', 'Login'),
        ('REGISTER', 'Register'),
        ('VERIFY', 'Verify'),
    ]

    mobile = models.CharField(max_length=15)
    otp_code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES)
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'customer_otp'

    def __str__(self):
        return f"OTP for {self.mobile}"

class OTPAttemptLog(SystemBaseModel):
    """
    Model for OTP attempt logs.
    """
    ATTEMPT_TYPE_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILURE', 'Failure'),
    ]

    mobile = models.CharField(max_length=15)
    attempt_type = models.CharField(max_length=10, choices=ATTEMPT_TYPE_CHOICES)
    ip_address = models.GenericIPAddressField()

    class Meta:
        db_table = 'otp_attempt_logs'

    def __str__(self):
        return f"{self.attempt_type} attempt for {self.mobile}"

class CustomerKYC(SystemBaseModel):
    """
    Model for customer KYC - simplified to PAN verification only.
    """
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE)
    address = models.ForeignKey('CustomerAddress', on_delete=models.SET_NULL, null=True, blank=True)
    pan_number = models.CharField(max_length=10)
    pan_document_url = models.TextField()
    status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='kyc_records'
    )
    verified_by = models.ForeignKey('AdminUser', on_delete=models.SET_NULL, null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'customer_kyc'

    def __str__(self):
        return f"KYC for {self.customer.full_name}"


class CustomerAddress(SystemBaseModel):
    """
    Model for customer addresses - supports multiple addresses per customer.
    """
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='addresses')
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    pincode = models.CharField(max_length=10)
    country = models.CharField(max_length=50, default='India')
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'customer_addresses'

    def __str__(self):
        return f"{self.customer.full_name} - {self.city}, {self.state}"


class SchemeMaster(SystemBaseModel):
    """
    Model for scheme configuration.
    """

    scheme_code = models.CharField(max_length=50, unique=True)
    scheme_name = models.CharField(max_length=150)
    tenure_months = models.IntegerField()
    
    gold_purity = models.CharField(max_length=5, choices=[('22K', '22K'), ('24K', '24K')], null=True, blank=True)
    # Instalment limits
    min_instalment = models.DecimalField(max_digits=10, decimal_places=2)
    max_instalment = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Marketing fields (for CP display)
    scheme_description = models.TextField(null=True, blank=True)
    marketing_banner_url = models.TextField(null=True, blank=True)
    highlight_tags = models.JSONField(null=True, blank=True)  # e.g., ["Gold Linked", "2 Month Bonus"]
    
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'scheme_master'

    def __str__(self):
        return self.scheme_name


class SchemeBenefit(SystemBaseModel):
    """
    Model for scheme benefits configuration.
    """
    BENEFIT_TYPE_CHOICES = [
        ('FLAT', 'Flat'),
        ('PERCENTAGE', 'Percentage'),
        ('FIXED_GRAM', 'Fixed Gram'),
        ('BONUS_MONTHS', 'Bonus Months'),
        ('DYNAMIC_LOCK', 'Dynamic Lock')
    ]

    scheme = models.ForeignKey(
        SchemeMaster,
        on_delete=models.CASCADE,
        related_name='benefits'
    )

    benefit_type = models.CharField(max_length=30, choices=BENEFIT_TYPE_CHOICES)

    benefit_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    benefit_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    benefit_months = models.IntegerField(default=0)

    class Meta:
        db_table = "scheme_benefits"

    def __str__(self):
        return f"{self.scheme.scheme_name} - {self.benefit_type}"


class CustomerSchemeBenefit(SystemBaseModel):
    """
    Snapshot model for customer scheme benefits at enrollment.
    """
    customer_scheme = models.ForeignKey(
        'CustomerScheme',
        on_delete=models.CASCADE,
        related_name='benefits'
    )

    benefit_type = models.CharField(max_length=30)

    benefit_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    benefit_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    benefit_months = models.IntegerField(default=0)

    class Meta:
        db_table = "customer_scheme_benefits"

    def __str__(self):
        return f"{self.customer_scheme.customer.full_name} - {self.benefit_type}"

class SchemeReferenceCounter(SystemBaseModel):
    """
    Counter to maintain last generated scheme reference number.
    Ensures atomic generation with no duplicates or skipping.
    """
    last_number = models.BigIntegerField(default=0)
    
    class Meta:
        db_table = 'scheme_reference_counters'
        verbose_name_plural = 'Scheme Reference Counters'
    
    @classmethod
    @transaction.atomic
    def get_next_number(cls):
        """
        Get next sequential number for scheme reference.
        Uses select_for_update() to ensure atomicity.
        """
        counter, created = cls.objects.select_for_update().get_or_create(id=1)
        counter.last_number += 1
        counter.save()
        return counter.last_number


class ReceiptCounter(SystemBaseModel):
    """
    Counter for sequential receipt number generation (RCPT000001, RCPT000002, etc.).
    Ensures atomic generation with no duplicates or skipping.
    """
    last_number = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'receipt_counters'
        verbose_name_plural = 'Receipt Counters'

    @classmethod
    @transaction.atomic
    def get_next_number(cls):
        """Get next sequential number for receipt. Uses select_for_update() for atomicity."""
        counter, created = cls.objects.select_for_update().get_or_create(id=1)
        counter.last_number += 1
        counter.save()
        return counter.last_number


class SaleInvoiceCounter(SystemBaseModel):
    """
    Counter for sequential invoice generation (INV-00001, INV-00002, ...).
    """
    last_number = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'sale_invoice_counters'
        verbose_name_plural = 'Sale Invoice Counters'

    @classmethod
    @transaction.atomic
    def get_next_number(cls):
        """
        Next sequence = max(active invoice numbers) + 1 so deleting the tip
        reuses that number; deleting a middle invoice does not fill the gap.
        """
        # Late resolve — SaleInvoice is declared below this counter model.
        SaleInvoice = cls._meta.apps.get_model('shared', 'SaleInvoice')
        max_seq = 0
        for num in (
            SaleInvoice.objects.filter(is_deleted=False)
            .values_list('invoice_number', flat=True)
            .iterator(chunk_size=500)
        ):
            raw = str(num or '').split('~', 1)[0]
            parts = raw.split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                seq = int(parts[1])
                if seq > max_seq:
                    max_seq = seq
        counter, _ = cls.objects.select_for_update().get_or_create(id=1)
        counter.last_number = max_seq + 1
        counter.save(update_fields=['last_number', 'system_updated_at'])
        return counter.last_number


class CustomerScheme(SystemBaseModel):
    """
    Customer scheme with two-level approval:
    1. CRM (KYC approval)
    2. Accounts (Finance approval)
    Tracks maturity values and accumulated gold.
    """

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    scheme = models.ForeignKey(SchemeMaster, on_delete=models.CASCADE)
    address = models.ForeignKey('CustomerAddress', on_delete=models.SET_NULL, null=True, blank=True)
    monthly_amount = models.DecimalField(max_digits=10, decimal_places=2)
    scheme_reference = models.CharField(max_length=20, unique=True, null=True, blank=True, 
                                       help_text="Unique scheme reference number (format: TS0001, TS0002, etc.)")

    # Snapshot fields at enrollment (reward configuration)
    tenure_months = models.IntegerField(null=True, blank=True)
    total_instalments = models.IntegerField(null=True, blank=True)
    total_payable_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # Financial fields
    total_paid = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    bonus_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    total_redeemable = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    # # Gold tracking
    # total_locked_gold = models.DecimalField(max_digits=10, decimal_places=4, default=0.0000)
    # accumulated_gold_grams = models.DecimalField(max_digits=10, decimal_places=4, default=0.0000)
    
    # Maturity values
    maturity_gold_grams = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    maturity_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    maturity_gold_rate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    # Lifecycle timestamps
    activated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When all tenure installments became PAID (COMPLETED).")
    processed_at = models.DateTimeField(null=True, blank=True, help_text="When scheduler applied bonus and set MATURED.")
    bonus_processed = models.BooleanField(default=False, help_text="True after monthly scheduler has applied bonus and gold lock.")

    # Force-abandon audit (admin-only; no bonus, maturity = total paid)
    abandoned_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='abandoned_schemes'
    )
    abandoned_reason = models.TextField(null=True, blank=True)
    abandoned_at = models.DateTimeField(null=True, blank=True)

    # -----------------------
    # Scheme Lifecycle
    # -----------------------
    scheme_status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='scheme_status_schemes',
        verbose_name='Scheme Status'
    )

    rejection_reason = models.CharField(max_length=255, null=True, blank=True)

    applied_at = models.DateTimeField(auto_now_add=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'customer_schemes'
        ordering = ['-system_created_at', '-system_updated_at']

    def __str__(self):
        return f"{self.customer.full_name} - {self.scheme.scheme_name}"


class CustomerNominee(SystemBaseModel):
    """
    Model for customer nominees - scheme-specific.
    """
    customer_scheme = models.ForeignKey(CustomerScheme, on_delete=models.CASCADE, related_name='nominees')
    full_name = models.CharField(max_length=150)
    relationship = models.CharField(max_length=50)
    mobile = models.CharField(max_length=15, null=True, blank=True)
    share_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=100.00)

    class Meta:
        db_table = 'customer_nominees'

    def __str__(self):
        return f"Nominee {self.full_name} for {self.customer_scheme.customer.full_name}"


class GoldLockingRecord(SystemBaseModel):
    """
    Tracks gold locking for each instalment payment.
    Detailed audit trail of gold grams locked per payment.
    """
    customer_scheme = models.ForeignKey(CustomerScheme, on_delete=models.CASCADE)
    instalment = models.ForeignKey('SchemeInstalment', on_delete=models.CASCADE)
    payment = models.ForeignKey('Payment', on_delete=models.CASCADE)
    gold_rate = models.DecimalField(max_digits=10, decimal_places=2)
    gold_grams = models.DecimalField(max_digits=10, decimal_places=4)
    locked_at = models.DateTimeField()
    payment_date = models.DateField()

    class Meta:
        db_table = 'gold_locking_records'
        unique_together = ('payment',)

    def __str__(self):
        return f"{self.gold_grams}g gold locked for {self.customer_scheme.customer.full_name}"


class SchemeInstalment(SystemBaseModel):
    """
    Model for scheme instalments.
    Tracks gold locking for gold-linked schemes.
    Bonus installments are company-funded (created_by_company=True, status=PAID).
    """
    customer_scheme = models.ForeignKey(CustomerScheme, on_delete=models.CASCADE)
    due_date = models.DateField()
    instalment_no = models.IntegerField()
    is_bonus = models.BooleanField(default=False)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    gold_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    gold_grams = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)  # Gold locked per instalment
    status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='instalments'
    )
    created_by_company = models.BooleanField(default=False)

    class Meta:
        db_table = 'scheme_instalments'
    
    def __str__(self):
        return f"Instalment for {self.customer_scheme.customer.full_name}"
    
    @property
    def is_gold_locked(self):
        return self.gold_grams is not None and self.gold_grams > 0

class Department(SystemBaseModel):
    """
    Model for departments (mapped to existing table).
    """
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'department'
        managed = False

    def __str__(self):
        return self.name


class Payment(SystemBaseModel):
    """
    Model for payments with multi-verification support.
    Ensures 1:1 relationship between Instalment and successful Payment.
    Multi-verification process:
    1. Webhook status updates from EssBazz
    2. Manual verification status from EssBazz API
    3. Payment status is confirmed as PAID if ANY of the above is SUCCESS

    Supports:
    - CP (Customer Portal): gateway payments, single mode in PaymentCollection
    - POS (Store Payment): single or split payment modes in PaymentCollection
    """
    instalment = models.ForeignKey(
        SchemeInstalment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Null for POS sale invoices. Set for scheme instalment payments.",
    )
    payment_mode = models.ForeignKey(
        LookupValue,
        db_column='payment_mode_id',
        on_delete=models.PROTECT,
        related_name='payments_by_mode',
        limit_choices_to={
            'lookup__code': 'PAYMENT_MODE',
            'is_active': True
        },
        null=True,
        blank=True,
        help_text="Primary mode for CP/single payments. POS split payments use PaymentCollection."
    )
    receipt_no = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        null=True,
        blank=True,
        help_text="Sequential receipt number (e.g. RCPT000001)"
    )
    payment_source = models.CharField(
        max_length=10,
        choices=[
            ('CP', 'Customer Portal'),
            ('POS', 'Store Payment')
        ],
        default='CP'
    )
    is_split_payment = models.BooleanField(
        default=False,
        help_text="True when payment has multiple PaymentCollection rows"
    )
    transaction_id = models.CharField(max_length=100, unique=True)
    gateway_transaction_id = models.CharField(max_length=100, null=True, blank=True)
    payment_status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='payments'
    )
    webhook_status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='webhook_payments',
        null=True,
        blank=True
    )
    esbuzz_verify_status = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        related_name='esbuzz_verify_payments',
        null=True,
        blank=True,
        help_text='Gateway STATUS/verify outcome (legacy column name; used by Orange PG dual-confirm).',
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    paid_at = models.DateTimeField(null=True, blank=True)
    is_finalized = models.BooleanField(default=False)
    reference_type = models.CharField(max_length=50, null=True, blank=True)
    reference_id = models.BigIntegerField(null=True, blank=True)
    payment_provider = models.CharField(
        max_length=20,
        choices=[
            ('ORANGE_PG', 'ICICI Orange PG'),
            ('ICICI_UPI', 'ICICI UPI Mandate'),
            ('EASEBUZZ', 'Easebuzz (legacy)'),
        ],
        default='ORANGE_PG',
        db_index=True,
    )
    upi_execution = models.ForeignKey(
        'UpiMandateExecution',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        help_text='Links CP ICICI mandate debit to this payment row.',
    )

    class Meta:
        db_table = 'payments'

    def __str__(self):
        if self.instalment_id:
            return f"Payment for {self.instalment.customer_scheme.customer.full_name}"
        if self.reference_type and self.reference_id:
            return f"Payment for {self.reference_type}:{self.reference_id}"
        return f"Payment {self.id}"


class PaymentCollection(SystemBaseModel):
    """
    Stores split payment mode details. One row per payment mode in a payment.
    CP payments: single row. POS split payments: multiple rows.
    """
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name='collections'
    )
    payment_mode = models.ForeignKey(
        LookupValue,
        on_delete=models.PROTECT,
        limit_choices_to={
            'lookup__code': 'PAYMENT_MODE',
            'is_active': True
        }
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference_number = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = 'payment_collections'

    def __str__(self):
        return f"Collection {self.payment_mode.code} {self.amount} for Payment {self.payment_id}"


class UpiMandate(SystemBaseModel):
    """
    ICICI UPI mandate — one row per enrolled customer scheme.
    Only one ACTIVE (APPROVED) mandate per customer_scheme at a time.
    """

    STATUS_PENDING = 'PENDING'
    STATUS_APPROVED = 'APPROVED'
    STATUS_REVOKED = 'REVOKED'
    STATUS_FAILED = 'FAILED'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REVOKED, 'Revoked'),
        (STATUS_FAILED, 'Failed'),
    ]

    customer_scheme = models.ForeignKey(
        CustomerScheme,
        on_delete=models.CASCADE,
        related_name='upi_mandates',
    )
    merchant_tran_id = models.CharField(max_length=100, unique=True, db_index=True)
    umn = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    payer_vpa = models.CharField(max_length=150, null=True, blank=True)
    payer_name = models.CharField(max_length=150, null=True, blank=True)
    payer_mobile = models.CharField(max_length=15, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    debit_day = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text='Day of month registered with ICICI for recurring debit (1–28).',
    )
    frequency = models.CharField(max_length=30, default='MONTHLY')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    bank_rrn = models.CharField(max_length=100, null=True, blank=True)
    qr_string = models.TextField(null=True, blank=True, help_text='ICICI MandateQR qrString — do not use local fallback')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    mandate_created_at = models.DateTimeField(null=True, blank=True)
    mandate_approved_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'upi_mandates'
        indexes = [
            models.Index(fields=['customer_scheme', 'status']),
        ]

    def __str__(self):
        return f"UPI Mandate {self.merchant_tran_id} ({self.status})"


class UpiMandateExecution(SystemBaseModel):
    """
    Tracks each ICICI mandate debit execution against a scheme instalment.
    """

    TXN_INITIATED = 'INITIATED'
    TXN_SUCCESS = 'SUCCESS'
    TXN_FAILED = 'FAILED'
    TXN_PENDING = 'PENDING'

    TXN_STATUS_CHOICES = [
        (TXN_INITIATED, 'Initiated'),
        (TXN_PENDING, 'Pending'),
        (TXN_SUCCESS, 'Success'),
        (TXN_FAILED, 'Failed'),
    ]

    upi_mandate = models.ForeignKey(
        UpiMandate,
        on_delete=models.CASCADE,
        related_name='executions',
    )
    scheme_instalment = models.ForeignKey(
        SchemeInstalment,
        on_delete=models.PROTECT,
        related_name='upi_executions',
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='upi_mandate_executions',
    )
    merchant_tran_id = models.CharField(max_length=100, unique=True, db_index=True)
    mandate_seq_no = models.CharField(max_length=50, null=True, blank=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    txn_status = models.CharField(max_length=20, choices=TXN_STATUS_CHOICES, default=TXN_PENDING, db_index=True)
    bank_rrn = models.CharField(max_length=100, null=True, blank=True)
    gateway_response = models.JSONField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'upi_mandate_executions'
        constraints = [
            models.UniqueConstraint(
                fields=['scheme_instalment'],
                name='uniq_upi_execution_per_instalment',
            ),
        ]

    def __str__(self):
        return f"UPI Execution {self.merchant_tran_id} ({self.txn_status})"


class UpiMandateNotification(SystemBaseModel):
    """
    ICICI Pre-Debit Notification (PDN) — one row per scheme instalment (instalment 2+).
    """

    STATUS_PENDING = 'PENDING'
    STATUS_SENT = 'SENT'
    STATUS_SUCCESS = 'SUCCESS'
    STATUS_FAILED = 'FAILED'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FAILED, 'Failed'),
    ]

    upi_mandate = models.ForeignKey(
        UpiMandate,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    scheme_instalment = models.ForeignKey(
        SchemeInstalment,
        on_delete=models.PROTECT,
        related_name='upi_mandate_notifications',
    )
    merchant_tran_id = models.CharField(max_length=100, unique=True, db_index=True)
    mandate_seq_no = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    debit_date = models.DateField(db_index=True)
    notification_date = models.DateField(db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    gateway_response = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'upi_mandate_notifications'
        constraints = [
            models.UniqueConstraint(
                fields=['scheme_instalment'],
                name='uniq_upi_notification_per_instalment',
            ),
        ]

    def __str__(self):
        return f"UPI PDN {self.merchant_tran_id} ({self.status})"


class Redemption(SystemBaseModel):
    """
    Model for redemption.
    """
    customer_scheme = models.ForeignKey(CustomerScheme, on_delete=models.CASCADE)
    jewellery_order_id = models.CharField(max_length=100)
    scheme_amount_used = models.DecimalField(max_digits=10, decimal_places=2)
    remaining_balance = models.DecimalField(max_digits=10, decimal_places=2)
    
    class Meta:
        db_table = 'redemptions'
    
    def __str__(self):
        return f"Redemption for {self.customer_scheme.customer.full_name}"

class Refund(SystemBaseModel):
    """
    Model for refunds.
    """
    REFUND_TYPE_CHOICES = [
        ('PARTIAL', 'Partial'),
        ('FULL', 'Full'),
    ]
    
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2)
    refund_type = models.CharField(max_length=10, choices=REFUND_TYPE_CHOICES)
    refund_reason = models.CharField(max_length=255)
    refunded_at = models.DateTimeField()
    
    class Meta:
        db_table = 'refunds'
    
    def __str__(self):
        if self.payment.instalment_id:
            return f"Refund for {self.payment.instalment.customer_scheme.customer.full_name}"
        return f"Refund for payment {self.payment_id}"


class SaleInvoice(SystemBaseModel):
    """
    POS/Future sales invoice header.
    Snapshot fields are mandatory; relational mapping is nullable for future flow.
    """
    STATUS_PAID = "PAID"
    STATUS_PARTIAL = "PARTIAL"
    STATUS_PENDING = "PENDING"
    STATUS_CHOICES = [
        (STATUS_PAID, "Paid"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_PENDING, "Pending"),
    ]

    invoice_number = models.CharField(max_length=64, unique=True, db_index=True)

    # Future mapping (nullable for now)
    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sale_invoices',
    )

    # Mandatory snapshot
    bill_to_name = models.CharField(max_length=150)
    bill_to_phone = models.CharField(max_length=15)
    bill_to_address = models.TextField()
    invoice_date = models.DateField(
        help_text="Commercial bill date printed on the tax invoice (may differ from system_created_at).",
    )

    # Financial snapshot
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pending_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)

    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "sale_invoices"
        ordering = ["-system_created_at"]

    def __str__(self):
        return self.invoice_number


class SaleItem(SystemBaseModel):
    """
    POS/Future sales item line.
    Keeps mandatory snapshot while allowing future FK mapping.
    """
    invoice = models.ForeignKey(
        'SaleInvoice',
        on_delete=models.CASCADE,
        related_name='items',
    )

    # Future mapping (nullable)
    product_item = models.ForeignKey(
        'ProductItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sale_items',
    )
    tag = models.ForeignKey(
        'ProductTag',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sale_items',
    )

    # Mandatory snapshot
    product_name = models.CharField(max_length=255)
    hsn = models.CharField(max_length=20, blank=True, default="")
    qty = models.DecimalField(max_digits=12, decimal_places=3)
    gross_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    net_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    purity = models.CharField(max_length=50)
    making_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    final_amount = models.DecimalField(max_digits=12, decimal_places=2)

    is_manual_entry = models.BooleanField(default=True)

    class Meta:
        db_table = "sale_items"
        ordering = ["id"]

    def __str__(self):
        return f"{self.invoice.invoice_number} - {self.product_name}"

class Role(SystemBaseModel):
    """
    Model for roles.
    """
    name = models.CharField(max_length=50)
    description = models.TextField()
    is_active = models.BooleanField(default=True)
    departments = models.ManyToManyField(Department, related_name='roles', blank=True)
    max_discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10,
        help_text='Default max catalogue discount %% allowed for users with this role (can be overridden on the user).',
    )
    
    class Meta:
        db_table = 'roles'
    
    def __str__(self):
        return self.name

class AdminUser(SystemBaseModel):
    """
    Model for admin users.
    """
    username = models.CharField(max_length=50, unique=True)
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    password_hash = models.CharField(max_length=255)
    profile_image = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_super_admin = models.BooleanField(default=False)
    last_login_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    max_discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10,
        help_text='Max catalogue discount %% this user may apply without manager approval.',
    )

    class Meta:
        db_table = 'admin_users'

    def __str__(self):
        return self.full_name

class AuditLog(SystemBaseModel):
    """
    Model for audit logs.
    """
    admin = models.ForeignKey(AdminUser, on_delete=models.CASCADE)
    action = models.CharField(max_length=255)
    entity_type = models.CharField(max_length=100)
    entity_id = models.BigIntegerField()
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField()
    
    class Meta:
        db_table = 'audit_logs'
    
    def __str__(self):
        return f"{self.action} by {self.admin.full_name}"

class CMSPage(SystemBaseModel):
    """
    Model for CMS pages.
    """
    page_key = models.CharField(max_length=50, unique=True)
    title = models.CharField(max_length=150)
    content = models.TextField()
    version = models.IntegerField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'cms_pages'
    
    def __str__(self):
        return self.title

class FAQ(SystemBaseModel):
    """
    Model for FAQs.
    """
    question = models.TextField()
    answer = models.TextField()
    is_active = models.BooleanField(default=True)
    display_order = models.IntegerField()
    
    class Meta:
        db_table = 'faqs'
    
    def __str__(self):
        return self.question

class PushTemplate(SystemBaseModel):
    """
    Model for push templates.
    """
    template_key = models.CharField(max_length=50, unique=True)
    title = models.CharField(max_length=150)
    message = models.TextField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'push_templates'
    
    def __str__(self):
        return self.title

class CustomerPolicyAcceptance(SystemBaseModel):
    """
    Model for customer policy acceptance.
    """
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    page_key = models.CharField(max_length=50)
    version = models.IntegerField()
    accepted_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'customer_policy_acceptance'
    
    def __str__(self):
        return f"{self.customer.full_name} accepted {self.page_key}"
    

    # ==============================
# RULE ENGINE MODELS
# ==============================

class Rule(SystemBaseModel):
    """
    Master rule definition.
    """
    RULE_TYPE_CHOICES = [
        ('BONUS', 'Bonus'),
        ('PENALTY', 'Penalty'),
        ('ELIGIBILITY', 'Eligibility'),
    ]
    
    rule_key = models.CharField(max_length=100, unique=True)
    rule_type = models.CharField(max_length=20, choices=RULE_TYPE_CHOICES)
    description = models.TextField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'rules'
    
    def __str__(self):
        return self.rule_key


class RuleCondition(SystemBaseModel):
    """
    Conditions under which a rule applies.
    """
    OPERATOR_CHOICES = [
        ('EQ', 'Equals'),
        ('GT', 'Greater Than'),
        ('LT', 'Less Than'),
        ('GTE', 'Greater Than Equal'),
        ('LTE', 'Less Than Equal'),
    ]
    
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name='conditions')
    field_name = models.CharField(max_length=100)  # e.g. paid_instalments
    operator = models.CharField(max_length=10, choices=OPERATOR_CHOICES)
    value = models.CharField(max_length=100)
    
    class Meta:
        db_table = 'rule_conditions'
    
    def __str__(self):
        return f"{self.rule.rule_key} condition"




class RuleAction(SystemBaseModel):
    """
    Action to perform when rule conditions match.
    """
    ACTION_TYPE_CHOICES = [
        ('CREDIT', 'Credit'),
        ('DEBIT', 'Debit'),
        ('STATUS_CHANGE', 'Status Change'),
    ]
    
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name='actions')
    action_type = models.CharField(max_length=20, choices=ACTION_TYPE_CHOICES)
    action_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    meta_data = models.JSONField(null=True, blank=True)
    
    class Meta:
        db_table = 'rule_actions'
    
    def __str__(self):
        return f"{self.rule.rule_key} action"


# ==============================
# SCHEME – RULE MAPPING
# ==============================

class SchemeRule(SystemBaseModel):
    """
    Maps rules to schemes.
    """
    scheme = models.ForeignKey(SchemeMaster, on_delete=models.CASCADE)
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE)
    priority = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'scheme_rules'
        unique_together = ('scheme', 'rule')
    
    def __str__(self):
        return f"{self.scheme.scheme_name} -> {self.rule.rule_key}"


# ==============================
# FINANCIAL ARCHITECTURE (managed=False; tables exist in DB)
# ==============================

class CustomerLedger(models.Model):

    VALUE_TYPE_CHOICES = (
        ('CASH', 'Cash'),
        ('GOLD', 'Gold'),
        ('SILVER', 'Silver'),
    )

    ENTRY_TYPE_CHOICES = (
        ('CREDIT', 'Credit'),
        ('DEBIT', 'Debit'),
        ('BONUS', 'Bonus'),
    )

    customer = models.ForeignKey('Customer', on_delete=models.CASCADE)
    customer_scheme = models.ForeignKey('CustomerScheme', on_delete=models.CASCADE)

    entry_type = models.CharField(max_length=10, choices=ENTRY_TYPE_CHOICES)
    value_type = models.CharField(max_length=10, choices=VALUE_TYPE_CHOICES)

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gold_grams = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    silver_grams = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    running_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    running_gold_balance = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    running_silver_balance = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    reference_type = models.CharField(max_length=50, null=True, blank=True)
    reference_id = models.IntegerField(null=True, blank=True)
    invoice = models.CharField(max_length=50, null=True, blank=True, help_text="Receipt number for this ledger entry")
    source = models.CharField(max_length=50, null=True, blank=True, help_text="Payment mode/source (CASH, UPI, CARD, NETBANKING, etc.)")

    entry_date = models.DateTimeField()
    description = models.TextField(null=True, blank=True)
    admin_remark = models.TextField(null=True, blank=True)


    class Meta:
        db_table = 'customer_ledger'
        managed = False

    def __str__(self):
        return f"{self.customer_id} - {self.value_type} - {self.entry_type}"



class AccountingLedger(models.Model):
    """Double-entry accounting ledger. Table: accounting_ledger."""
    id = models.BigAutoField(primary_key=True)
    account_code = models.CharField(max_length=50)
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    reference_type = models.CharField(max_length=50)
    reference_id = models.BigIntegerField()
    description = models.CharField(max_length=255, null=True, blank=True)
    entry_date = models.DateTimeField()

    class Meta:
        db_table = 'accounting_ledger'
        managed = False


class FinancialTransaction(models.Model):
    """Financial transaction log. Table: financial_transactions."""
    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey('Customer', on_delete=models.DO_NOTHING, null=True)
    customer_scheme = models.ForeignKey('CustomerScheme', on_delete=models.DO_NOTHING, null=True)
    source_type = models.CharField(max_length=30)
    source_id = models.BigIntegerField()
    direction = models.CharField(max_length=10)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_mode = models.CharField(max_length=30, null=True, blank=True)
    status = models.CharField(max_length=20)
    transaction_date = models.DateTimeField()
    gateway_transaction_id = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = 'financial_transactions'
        managed = False


# ==============================
# PENALTY TRACKING
# ==============================

class Penalty(SystemBaseModel):
    """
    Penalty applied for late or failed instalments.
    """
    customer_scheme = models.ForeignKey(CustomerScheme, on_delete=models.CASCADE)
    instalment = models.ForeignKey(SchemeInstalment, on_delete=models.CASCADE)
    rule = models.ForeignKey(Rule, on_delete=models.SET_NULL, null=True, blank=True)
    penalty_amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=255)
    is_waived = models.BooleanField(default=False)
    
    class Meta:
        db_table = 'penalties'
    
    def __str__(self):
        return f"Penalty {self.penalty_amount} for {self.customer_scheme.customer.full_name}"



class Module(models.Model):
    """
    Model for modules.
    """
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'modules'

    def __str__(self):
        return self.name


class SubModule(models.Model):
    """
    Model for sub modules (optional).
    """
    id = models.BigAutoField(primary_key=True)
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='sub_modules')
    parent = models.ForeignKey('self', on_delete=models.CASCADE, related_name='sub_modules', null=True, blank=True)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'sub_modules'

    def __str__(self):
        if self.parent:
            return f"{self.module.name} - {self.parent.name} - {self.name}"
        return f"{self.module.name} - {self.name}"


class Section(models.Model):
    """
    Model for sections. A section belongs to a module or a submodule.
    """
    id = models.BigAutoField(primary_key=True)
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='sections')
    sub_module = models.ForeignKey(SubModule, on_delete=models.CASCADE, related_name='sections', null=True, blank=True)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'sections'

    def __str__(self):
        if self.sub_module:
            return f"{self.sub_module} - {self.name}"
        return f"{self.module.name} - {self.name}"


class Action(models.Model):
    """
    Model for actions (VIEW, CREATE, UPDATE, DELETE, etc.).
    """
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50, unique=True)

    class Meta:
        db_table = 'actions'

    def __str__(self):
        return self.name


class Permission(models.Model):
    """
    Model for permissions - one permission = one action on one section.
    """
    id = models.BigAutoField(primary_key=True)
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name='permissions')
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name='permissions')
    code = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'permissions'

    def __str__(self):
        return self.name


class RolePermission(SystemBaseModel):
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        db_table = 'role_permissions'
        unique_together = ('role', 'permission')


class AdminUserRole(SystemBaseModel):
    admin_user = models.ForeignKey(AdminUser, on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        db_table = 'admin_user_roles'
        unique_together = ('admin_user', 'role')


class PaymentAuditLog(models.Model):

    txnid = models.CharField(max_length=100, db_index=True)

    type = models.CharField(
        max_length=100
    )

    status = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    response_json = models.JSONField(
        null=True,
        blank=True
    )

    request_payload = models.JSONField(
        null=True,
        blank=True
    )

    class Meta:
        db_table = "payment_audit_log"

    def __str__(self):
        return f"{self.txnid} - {self.type}"

class AdminNotification(SystemBaseModel):
    """
    Model for admin notifications.
    """

    title = models.CharField(max_length=255)
    section_code = models.CharField(max_length=255)
    type = models.CharField(max_length=255)

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='notifications',
        db_column='customer_id'
    )

    installment = models.ForeignKey(
        SchemeInstalment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications',
        db_column='installment_id'
    )

    message = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'admin_notification'
        indexes = [
            models.Index(fields=['customer']),
            models.Index(fields=['installment']),
            models.Index(fields=['type']),
        ]

    def __str__(self):
        return f"{self.title} - {self.section_code} - {self.customer_id}"
    
class AdminUserNotification(SystemBaseModel):
    """
    Model to track which admin user received which notification.
    """
    admin_user = models.ForeignKey(
        AdminUser,
        on_delete=models.CASCADE,
        related_name='user_notifications'
    )

    notification = models.ForeignKey(
        AdminNotification,
        on_delete=models.CASCADE,
        related_name='notification_users'
    )

    isView = models.BooleanField(default=False)

    class Meta:
        db_table = 'admin_user_notification'
        unique_together = ('admin_user', 'notification')

    def __str__(self):
        return f"{self.admin_user.full_name} - {self.notification.id}"

from django.db import models


class Branch(SystemBaseModel):
    """Branch (HO / location). Used for branch-wise metal purity & rate override."""
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "branch"

    def __str__(self):
        return self.name or self.code or str(self.id)



class HSNMaster(SystemBaseModel):
    """
    HSN code master for GST/tax configuration (e.g. 7108 - Gold @ 3%).
    Table: hsn_master
    """
    hsn_code = models.CharField(max_length=20, unique=True)
    description = models.TextField(null=True, blank=True)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    category = models.CharField(max_length=100, null=True, blank=True)
    gst_type = models.CharField(max_length=20, default="GST")
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    making_charge_taxable = models.BooleanField(default=True)
    stone_tax_applicable = models.BooleanField(default=False)
    cess_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    remarks = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "hsn_master"
        ordering = ["hsn_code"]

    def __str__(self):
        return f"{self.hsn_code} - {self.description or ''}".strip()




class Metal(SystemBaseModel):
    """Single global metal table. One row per metal type (e.g. Gold, Silver). Availability per branch via BranchMetal."""
    metal_name = models.CharField(max_length=255, unique=True)
    hsn = models.ForeignKey(
        "HSNMaster",
        on_delete=models.PROTECT,
        related_name="metals",
        db_column="hsn_id",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "metal"

    def __str__(self):
        return self.metal_name


class BranchMetal(SystemBaseModel):
    """
    Branch–metal mapping: controls which metals are available per branch.
    JOIN branch_metal when fetching metals for a branch; do not duplicate metal definitions.
    """
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="branch_metals",
        db_column="branch_id",
    )
    metal = models.ForeignKey(
        Metal,
        on_delete=models.CASCADE,
        related_name="branch_metals",
        db_column="metal_id",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "branch_metal"
        unique_together = (("branch_id", "metal_id"),)

    def __str__(self):
        return f"{self.branch} / {self.metal}"


class MetalMasterRule(SystemBaseModel):
    """
    Master (HO) purity configuration per metal.
    Table kept as metal_rule (treat as master rule); rename to metal_master_rule only if safe via migration.
    """
    metal = models.ForeignKey(
        Metal,
        on_delete=models.CASCADE,
        related_name="rules",
        db_column="metal_id"
    )
    purity_name = models.CharField(max_length=30, null=True, blank=True)
    purity_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Purity as percentage (0-100), e.g. 99.9 for 24K"
    )
    description = models.TextField(null=True, blank=True)
    type = models.CharField(max_length=50, null=True, blank=True)
    is_base = models.BooleanField(default=False)

    class Meta:
        db_table = "metal_master_rule"

    def __str__(self):
        return f"{self.metal.metal_name} - {self.purity_name or self.type}"

    def save(self, *args, **kwargs):
        if self.is_base and self.metal_id:
            MetalMasterRule.objects.filter(
                metal_id=self.metal_id,
                is_base=True,
            ).exclude(pk=self.pk).update(is_base=False)
        super().save(*args, **kwargs)


# Backward compatibility: alias so existing code using MetalRule still works until all refs updated
MetalRule = MetalMasterRule


class MetalBranchRule(SystemBaseModel):
    """Branch override of purity config. When is_current=1, used instead of master rule."""
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name="metal_branch_rules",
        db_column="branch_id"
    )
    metal = models.ForeignKey(
        Metal,
        on_delete=models.CASCADE,
        related_name="branch_rules",
        db_column="metal_id"
    )
    purity_name = models.CharField(max_length=30, null=True, blank=True)
    purity_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Purity as percentage (0-100)"
    )
    description = models.TextField(null=True, blank=True)
    type = models.CharField(max_length=50, null=True, blank=True)
    is_base = models.BooleanField(default=False)
    is_current = models.BooleanField(default=True, db_column="is_current")

    class Meta:
        db_table = "metal_branch_rule"
        unique_together = (("branch_id", "metal_id", "purity_name"),)

    def __str__(self):
        return f"{self.branch} / {self.metal} - {self.purity_name or self.type}"


class MetalMasterRate(SystemBaseModel):
    """
    Master (HO) rate per metal, purity, date.
    Unique: (metal_id, purity_name, effective_date).
    """
    metal = models.ForeignKey(
        Metal,
        on_delete=models.CASCADE,
        related_name='master_rates',
        db_column='metal_id'
    )
    purity_name = models.CharField(max_length=30, null=True, blank=True, db_column='purity_name')
    sell_price = models.DecimalField(max_digits=12, decimal_places=2, db_column='sell_price')
    buyback_price = models.DecimalField(max_digits=12, decimal_places=2, db_column='buyback_price')
    effective_date = models.DateField(db_column='effective_date')
    is_active = models.BooleanField(default=True, db_column='is_active')

    class Meta:
        db_table = 'metal_master_rate'
        unique_together = (('metal_id', 'purity_name', 'effective_date'),)
        ordering = ['-effective_date', 'metal_id']

    def __str__(self):
        return f"Master rate {self.metal_id} {self.purity_name} {self.effective_date}"

    @property
    def rate_value(self):
        return self.sell_price


class MetalBranchRate(SystemBaseModel):
    """
    Branch override rate per branch, metal, purity, date.
    Unique: (branch_id, metal_id, purity_name, effective_date).
    """
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name='metal_branch_rates',
        db_column='branch_id'
    )
    metal = models.ForeignKey(
        Metal,
        on_delete=models.CASCADE,
        related_name='branch_rates',
        db_column='metal_id'
    )
    purity_name = models.CharField(max_length=30, null=True, blank=True, db_column='purity_name')
    sell_price = models.DecimalField(max_digits=12, decimal_places=2, db_column='sell_price')
    buyback_price = models.DecimalField(max_digits=12, decimal_places=2, db_column='buyback_price')
    effective_date = models.DateField(db_column='effective_date')
    is_current = models.BooleanField(default=True, db_column='is_current')
    is_active = models.BooleanField(default=True, db_column='is_active')

    class Meta:
        db_table = 'metal_branch_rate'
        unique_together = (('branch_id', 'metal_id', 'purity_name', 'effective_date'),)
        ordering = ['-effective_date', 'branch_id', 'metal_id']

    def __str__(self):
        return f"Branch rate {self.branch_id} {self.metal_id} {self.purity_name} {self.effective_date}"

    @property
    def rate_value(self):
        return self.sell_price
    
from django.db import models


class Vendor(models.Model):
    vendor_code = models.CharField(max_length=50, unique=True)
    vendor_name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, null=True, blank=True)
    email = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=20, null=True, blank=True)
    gst_number = models.CharField(max_length=20, null=True, blank=True)
    pan_number = models.CharField(max_length=20, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    system_created_at = models.DateTimeField(auto_now_add=True)
    system_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor"


class VendorBankDetails(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    account_holder_name = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50)
    bank_name = models.CharField(max_length=255)
    ifsc_code = models.CharField(max_length=20)
    branch_name = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "vendor_bank_details"


class VendorAddress(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    address_line1 = models.CharField(max_length=255)
    address_line2 = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    pincode = models.CharField(max_length=20)

    class Meta:
        db_table = "vendor_address"


class Stone(models.Model):
    # Hyphenated auto-SKUs can be long (colour + CLR + CUT + size + rank + shape + HSN); DB uses VARCHAR(255).
    stone_code = models.CharField(max_length=255, unique=True)
    stone_name = models.TextField()

    stone_type = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        related_name="stone_types"
    )

    stone_category = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        related_name="stone_categories"
    )

    color = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        related_name="stone_colors"
    )

    shape = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        related_name="stone_shapes"
    )

    rank = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        related_name="stone_ranks"
    )

    stone_group = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stone_groups",
        help_text="e.g. Diamond / Ruby / Neelam — optional until lookup STONE_GROUP is populated.",
    )

    clarity = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stone_clarities",
        help_text="Clarity / grade (e.g. VVS, VS); often same lookup family as former variant quality.",
    )

    cut = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stone_cuts",
    )

    stone_size = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Physical size (e.g. mm); optional.",
    )

    size_unit = models.ForeignKey(
        "LookupValue",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stones_size_unit",
    )

    default_rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Default purchase/sell reference rate when not priced elsewhere.",
    )

    hsn = models.ForeignKey(
        "HSNMaster",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stones",
        help_text="Optional HSN master row for GST / reporting.",
    )

    is_active = models.BooleanField(default=True)

    system_created_at = models.DateTimeField(auto_now_add=True)
    system_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "stones"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.stone_name} ({self.stone_code})"


class Category(SystemBaseModel):
    """
    Product/category master used for grouping items (e.g. Rings, Earrings).
    """

    name = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=160, unique=True)
    description = models.TextField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "categories"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Subcategory(SystemBaseModel):
    """
    Subcategory under a Category (e.g. Engagement Rings, Diamond Rings).
    """

    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="subcategories",
        db_column="category_id",
    )
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160)
    description = models.TextField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "subcategories"
        ordering = ["category_id", "sort_order", "name"]
        unique_together = ("category", "slug")

    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
class ProductDraft(SystemBaseModel):
    """
    Stores temporary product creation data from the 5-step wizard.
    Table: product_drafts
    """
    created_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.CASCADE,
        related_name='product_drafts_created'
    )
    
    updated_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_drafts_updated'
    )

    current_step = models.IntegerField(default=1)

    draft_data = models.JSONField(default=dict)

    class Meta:
        db_table = "product_drafts"

    def __str__(self):
        return f"Draft #{self.id} by {self.created_by.username}"


class ProductGroup(SystemBaseModel):
    """
    Stores style/category information for products.
    Table: product_groups
    """
    style_name = models.CharField(max_length=255)

    category = models.ForeignKey(
        'Category',
        on_delete=models.RESTRICT,
        related_name='product_groups'
    )

    subcategory = models.ForeignKey(
        'Subcategory',
        on_delete=models.RESTRICT,
        related_name='product_groups'
    )

    gender = models.ForeignKey(
        'LookupValue',
        on_delete=models.RESTRICT,
        related_name='product_groups'
    )

    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "product_groups"
        unique_together = (('style_name', 'category', 'subcategory', 'gender'),)

    def __str__(self):
        return self.style_name


class ProductSKU(SystemBaseModel):
    """
    Stores configuration for product groups.
    Table: product_skus
    """
    product_group = models.ForeignKey(
        ProductGroup,
        on_delete=models.CASCADE,
        related_name='skus'
    )

    pattern_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Pattern identifier (e.g. MCRK-1001); part of SKU identity.",
    )

    color = models.ForeignKey(
        'LookupValue',
        on_delete=models.PROTECT,
        related_name='product_skus'
    )

    hsn = models.ForeignKey(
        'HSNMaster',
        on_delete=models.PROTECT,
        related_name='product_skus'
    )

    product_code = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Base product code copied from the first item published under this SKU.",
    )

    sku_code = models.CharField(max_length=128, null=True, blank=True)

    style_code = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = "product_skus"
        constraints = [
            # product_code and pattern_code may be shared by multiple SKUs.
            models.UniqueConstraint(
                fields=('sku_code',),
                condition=Q(sku_code__isnull=False) & ~Q(sku_code=''),
                name='uniq_product_sku_sku_code_non_empty',
            ),
        ]

    def __str__(self):
        return f"{self.product_group.style_name} - {self.color.label if self.color else 'No Color'}"


class ProductItem(SystemBaseModel):
    """
    Sellable inventory line: one row per (SKU, structured size). Current quantity on hand: qty.
    Table: product_items
    """
    sku = models.ForeignKey(
        ProductSKU,
        on_delete=models.CASCADE,
        related_name='product_items'
    )

    size_number = models.IntegerField(
        null=True,
        blank=True,
        help_text="Optional ring-style size on this stock line (not on SKU).",
    )
    size_mm = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Optional diameter (mm) on this stock line.",
    )
    height_mm = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Optional height (mm) when using H×W size on this line.",
    )
    width_mm = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Optional width (mm) when using H×W size on this line.",
    )

    qty = models.IntegerField(
        default=0,
        help_text="Current stock for this SKU + size line (maintained via StockTransaction).",
    )

    store_variant_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Store-facing variant label from product wizard step 1.",
    )
    customer_variant_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Customer-facing variant label from product wizard step 1.",
    )

    is_parent_product = models.BooleanField(
        default=False,
        help_text="True on the canonical parent row (virtual style); child variants link via parent_product_item.",
    )
    parent_product_item = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="variant_children",
        help_text="When set, this item is a metal/colour variant of the parent product item.",
    )

    net_weight = models.DecimalField(max_digits=10, decimal_places=3)

    gross_weight = models.DecimalField(max_digits=10, decimal_places=3)

    charge_apply = models.CharField(max_length=20, null=True, blank=True)

    geometrical_shape = models.ForeignKey(
        'LookupValue',
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name='product_items_geometrical_shape',
        help_text='Product geometrical shape (Lookup GEOMETRICAL_SHAPE).',
    )

    @property
    def less_weight(self):
        """Derived: gross − net. Not stored on product_items."""
        g = self.gross_weight if self.gross_weight is not None else Decimal("0")
        n = self.net_weight if self.net_weight is not None else Decimal("0")
        diff = g - n
        return diff if diff > 0 else Decimal("0")

    class Meta:
        db_table = "product_items"
        constraints = [
            models.UniqueConstraint(
                fields=("sku", "size_number"),
                condition=Q(size_number__isnull=False),
                name="uniq_productitem_sku_size_number",
            ),
            models.UniqueConstraint(
                fields=("sku", "size_mm"),
                condition=Q(size_mm__isnull=False),
                name="uniq_productitem_sku_size_mm",
            ),
            models.UniqueConstraint(
                fields=("sku", "height_mm", "width_mm"),
                condition=Q(height_mm__isnull=False) & Q(width_mm__isnull=False),
                name="uniq_productitem_sku_hw_mm",
            ),
        ]

    def __str__(self):
        from shared.product_item_size import format_product_item_size_display

        code = self.sku.product_code if self.sku_id and self.sku.product_code else f"id{self.pk}"
        return f"{code} [{format_product_item_size_display(self)}]"

    _SIZE_VALIDATE_FIELDS = frozenset(
        {"sku_id", "sku", "size_number", "size_mm", "height_mm", "width_mm"}
    )

    def _should_validate_size_on_save(self, update_fields) -> bool:
        if getattr(self, "_skip_size_validation", False) or not self.sku_id:
            return False
        if update_fields is None:
            return True
        return bool(self._SIZE_VALIDATE_FIELDS.intersection(update_fields))

    def save(self, *args, **kwargs):
        from shared.product_item_size import (
            normalize_product_item_size_fields,
            validate_product_item_size_fields,
        )

        update_fields = kwargs.get("update_fields")
        if self._should_validate_size_on_save(update_fields):
            sku = self.sku if getattr(self, "sku", None) and self.sku.pk else None
            if sku is None:
                sku = ProductSKU.objects.filter(pk=self.sku_id).first()
            if sku:
                normalize_product_item_size_fields(self)
                validate_product_item_size_fields(
                    size_number=self.size_number,
                    size_mm=self.size_mm,
                    height_mm=self.height_mm,
                    width_mm=self.width_mm,
                )
        super().save(*args, **kwargs)


class ProductItemLinkedVendor(SystemBaseModel):
    """M2M map: which vendors can supply/manufacture this product item (parent or child)."""

    product_item = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name="linked_vendors",
    )
    vendor = models.ForeignKey(
        "Vendor",
        on_delete=models.PROTECT,
        related_name="linked_product_items",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    vendor_variant_name = models.CharField(max_length=255, blank=True, default='')
    delivery_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Days this vendor needs to deliver this product.",
    )
    validity = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Date/time until this vendor can manufacture or supply this product.",
    )

    class Meta:
        db_table = "product_item_linked_vendors"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("product_item", "vendor"),
                name="uniq_productitem_linked_vendor",
            ),
        ]


class ProductStone(SystemBaseModel):
    """
    Stores stone information for product items.
    Table: product_stones
    """
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='stones'
    )

    stone = models.ForeignKey(
        'Stone',
        on_delete=models.RESTRICT,
        related_name='product_stones'
    )

    quantity = models.IntegerField()

    weight = models.DecimalField(max_digits=10, decimal_places=3)

    class Meta:
        db_table = "product_stones"

    def __str__(self):
        return f"{self.stone.stone_name} ({self.quantity})"


class ProductImage(SystemBaseModel):
    """
    Stores product images.
    Table: product_images
    """
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='images'
    )

    image_url = models.TextField()

    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "product_images"

    def __str__(self):
        return f"Image for item {self.product_id}"


class ProductBOM(SystemBaseModel):
    """
    Bill of Materials for product items - stores metal/stone material details.
    Table: product_material_bom
    
    Validation (see clean()): METAL requires metal+purity; STONE requires master stone;
    metal fields must be NULL on STONE rows.
    """
    MATERIAL_TYPE_CHOICES = [
        ('METAL', 'Metal'),
        ('STONE', 'Stone'),
    ]
    
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='bom_items'
    )

    material_type = models.CharField(max_length=10, choices=MATERIAL_TYPE_CHOICES)

    metal = models.ForeignKey(
        'Metal',
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name='product_boms'
    )

    purity = models.ForeignKey(
        'MetalMasterRule',
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name='product_boms'
    )

    stone = models.ForeignKey(
        'Stone',
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name='product_boms'
    )

    weight = models.DecimalField(max_digits=10, decimal_places=3)

    quantity = models.IntegerField(default=1)

    class Meta:
        db_table = "product_material_bom"

    def __str__(self):
        return f"{self.material_type} - item {self.product_id}"

    def clean(self):
        """Validate material fields based on material_type CHECK constraint."""
        from django.core.exceptions import ValidationError
        if self.material_type == 'METAL':
            if not self.metal or not self.purity:
                raise ValidationError('For METAL material_type, metal and purity are required.')
            if self.stone:
                raise ValidationError('For METAL material_type, stone must be NULL.')
        elif self.material_type == 'STONE':
            if not self.stone:
                raise ValidationError('For STONE material_type, stone is required.')
            if self.metal or self.purity:
                raise ValidationError('For STONE material_type, metal and purity must be NULL.')


class ProductOccasion(SystemBaseModel):
    """
    Stores occasion information for product items (from LookupValue).
    Table: product_occasions
    """
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='occasions'
    )

    occasion = models.ForeignKey(
        'LookupValue',
        on_delete=models.RESTRICT,
        related_name='product_occasions'
    )

    class Meta:
        db_table = "product_occasions"
        unique_together = (('product', 'occasion'),)

    def __str__(self):
        return f"{self.occasion.label} for item {self.product_id}"


class ProductAttribute(SystemBaseModel):
    """
    Stores product attribute details (making_category, crafting_process, method, nature, finishing).
    Table: product_attributes
    """
    product_bom = models.ForeignKey(
        ProductBOM,
        on_delete=models.CASCADE,
        related_name='attributes'
    )

    making_category = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_making_category',
        db_column='making_category',
    )

    crafting_process = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_crafting_process',
        db_column='crafting_process',
    )

    method = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_method',
        db_column='method',
    )

    nature = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_nature',
        db_column='nature',
    )

    finishing = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_finishing',
        db_column='finishing',
    )

    special_charge = models.CharField(max_length=50, null=True, blank=True)

    charge_type = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_attributes_charge_type',
        db_column='charge_type',
    )

    detail_number = models.IntegerField(default=1)

    class Meta:
        db_table = "product_attributes"

    def __str__(self):
        return f"Attribute for BOM {self.product_bom_id}"


class ProductOperationCharge(SystemBaseModel):
    """
    Stores operation charges for product items.
    Table: product_operation_charges
    """
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='operation_charges'
    )

    component_name = models.CharField(max_length=255)

    charge_value = models.CharField(max_length=50, null=True, blank=True)

    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "product_operation_charges"

    def __str__(self):
        return f"{self.component_name} for item {self.product_id}"


class ProductPattern(SystemBaseModel):
    """
    Stores pattern information for product items.
    Table: product_patterns
    """
    product = models.ForeignKey(
        ProductItem,
        on_delete=models.CASCADE,
        related_name='patterns'
    )

    pattern_name = models.CharField(max_length=255)

    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "product_patterns"
        unique_together = (('product', 'pattern_name'),)

    def __str__(self):
        return f"{self.pattern_name} for item {self.product_id}"


# ---------------------------------------------------------------------------
# Purchase Order (procurement — before GRN receipt)
# ---------------------------------------------------------------------------

class PurchaseOrder(SystemBaseModel):
  """
  Purchase order header — commercial order to vendor before physical GRN.
  """
  STATUS_CHOICES = [
      ('Draft', 'Draft'),
      ('Approved', 'Approved'),
      ('Sent', 'Sent'),
      ('Partially_Received', 'Partially Received'),
      ('Received', 'Received'),
      ('Closed', 'Closed'),
      ('Cancelled', 'Cancelled'),
  ]

  po_no = models.CharField(max_length=64, unique=True, db_index=True)
  po_date = models.DateField()
  po_category = models.CharField(max_length=128, blank=True, default='')
  item_type = models.CharField(max_length=256, blank=True, default='')
  vendor = models.ForeignKey(
      'Vendor',
      on_delete=models.PROTECT,
      null=True,
      blank=True,
      related_name='purchase_orders',
  )
  vendor_name = models.CharField(max_length=256, blank=True, default='')
  currency = models.CharField(max_length=8, default='INR')
  terms = models.CharField(max_length=256, blank=True, default='')
  remarks = models.TextField(blank=True, default='')
  require_date = models.DateField(null=True, blank=True)
  contact_person = models.CharField(max_length=256, blank=True, default='')
  validity_date = models.DateField(null=True, blank=True)
  validity_days = models.CharField(max_length=16, blank=True, default='')
  expected_delivery_date = models.DateField(null=True, blank=True)
  buyer_name = models.CharField(max_length=256, blank=True, default='')
  ship_to_address = models.TextField(blank=True, default='')
  internal_notes = models.TextField(blank=True, default='')
  status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='Draft', db_index=True)
  total_ordered_pcs = models.IntegerField(default=0)
  total_ordered_g_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

  class Meta:
      db_table = 'purchase_orders'
      ordering = ['-system_created_at']

  def __str__(self):
      return f"{self.po_no} ({self.po_date})"


class PurchaseOrderLine(SystemBaseModel):
  """One ordered line — linked to virtual product; metal/purity editable on PO."""
  LINE_STATUS_CHOICES = [
      ('Open', 'Open'),
      ('Partial', 'Partial'),
      ('Closed', 'Closed'),
      ('Cancelled', 'Cancelled'),
  ]

  purchase_order = models.ForeignKey(
      PurchaseOrder,
      on_delete=models.CASCADE,
      related_name='lines',
  )
  line_no = models.PositiveIntegerField()
  product_item = models.ForeignKey(
      'ProductItem',
      on_delete=models.SET_NULL,
      null=True,
      blank=True,
      related_name='purchase_order_lines',
  )
  product_sku = models.ForeignKey(
      'ProductSKU',
      on_delete=models.SET_NULL,
      null=True,
      blank=True,
      related_name='purchase_order_lines',
  )
  product_code = models.CharField(max_length=100, blank=True, default='')
  sku_code = models.CharField(max_length=128, blank=True, default='')
  style_name = models.CharField(max_length=255, blank=True, default='')
  item_group = models.CharField(max_length=150, blank=True, default='')
  item_type = models.CharField(max_length=150, blank=True, default='')
  category = models.ForeignKey(
      'Category',
      on_delete=models.SET_NULL,
      null=True,
      blank=True,
      related_name='purchase_order_lines',
  )
  subcategory = models.ForeignKey(
      'Subcategory',
      on_delete=models.SET_NULL,
      null=True,
      blank=True,
      related_name='purchase_order_lines',
  )
  vendor_variant_name = models.CharField(max_length=255, blank=True, default='')
  metal = models.CharField(max_length=64, blank=True, default='')
  purity = models.CharField(max_length=64, blank=True, default='')
  colour = models.CharField(max_length=64, blank=True, default='')
  stone_name = models.CharField(max_length=255, blank=True, default='')
  size_display = models.CharField(max_length=64, blank=True, default='')
  ordered_pcs = models.IntegerField(default=1)
  ordered_g_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
  ordered_net_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
  making_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
  stone_purchase_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
  line_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
  expected_delivery_date = models.DateField(null=True, blank=True)
  remarks = models.TextField(blank=True, default='')
  line_status = models.CharField(max_length=16, choices=LINE_STATUS_CHOICES, default='Open')
  received_pcs = models.IntegerField(default=0)
  received_g_wt = models.DecimalField(max_digits=18, decimal_places=4, default=0)

  class Meta:
      db_table = 'purchase_order_lines'
      ordering = ['line_no']
      constraints = [
          models.UniqueConstraint(
              fields=['purchase_order', 'line_no'],
              name='uniq_po_line_no',
          ),
      ]

  def __str__(self):
      return f"PO#{self.purchase_order_id} L{self.line_no}"


class GrnBatch(SystemBaseModel):
    """
    GRN (Goods Receipt Note) batch header — field names aligned with
    jewel-admin-suite GrnBatchManagement.tsx BatchRow / GRN-Batch.docx.
    """
    # Batch number — mandatory in API; NOT unique (same number may repeat).
    doc_no = models.CharField(max_length=64, blank=True, default="")
    date = models.DateField()
    category = models.CharField(max_length=128)
    product_type = models.CharField(max_length=512, blank=True, default="")
    vendor = models.CharField(max_length=256, blank=True, default="")
    # UI: T & C (optional)
    terms = models.CharField(max_length=256, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    contact_person = models.CharField(max_length=256, blank=True, default="")
    validity = models.DateField(null=True, blank=True)
    reorder_deliver_days = models.CharField(max_length=32, blank=True, default="")
    # Comma-separated metal names from masters (multi-select on batch creation).
    metal = models.CharField(max_length=512, blank=True, default="")
    quantity = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    pcs = models.IntegerField(null=True, blank=True)
    # UI: Gross weight (mandatory)
    g_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    # UI: Less weight (doc) — deduction / stone weight, up to 4 decimals
    stone_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    stone_wt_unit = models.CharField(max_length=16, blank=True, default="grams")
    stone_rate_basis = models.CharField(max_length=16, blank=True, default="per_gram")
    stone_purchase_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    stone_sell_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    stone_exchange_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    quality_check = models.CharField(max_length=128, blank=True, default="")
    bom = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=32, default="Open")

    class Meta:
        db_table = "grn_batches"
        ordering = ["-system_created_at"]

    def __str__(self):
        return f"{self.doc_no or self.id} ({self.date})"


class GrnLot(SystemBaseModel):
    """
    GRN lot line — field names aligned with grn-lot-types.ts LotListingRow
    and Make lot popup (GrnBatchManagement).
    """
    lot_no = models.CharField(max_length=32)
    batch = models.ForeignKey(
        "GrnBatch",
        on_delete=models.PROTECT,
        related_name="lots",
        null=True,
        blank=True,
        help_text="Real FK to the parent batch. Replaces the legacy batch_doc_no string match.",
    )
    # Legacy display string kept in sync with batch.doc_no for read compatibility.
    # New code should read lot.batch.doc_no instead.
    batch_doc_no = models.CharField(max_length=64, db_index=True)
    # Category FK → UI Item group; Subcategory FK → UI Item type.
    category = models.ForeignKey(
        "Category",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="grn_lots",
    )
    subcategory = models.ForeignKey(
        "Subcategory",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="grn_lots",
    )
    vendor_variant_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Vendor variant name from product master; used to filter catalog SKUs.",
    )
    # Lot identity is pattern/product scoped. Metal / purity / stone are segregated on Make Bag.
    product_code = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="Product code selected/matched during Lot Creation.",
    )
    pattern_code = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Pattern code from registry / product SKU; filtered by item group + item type.",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    pcs = models.IntegerField(null=True, blank=True)
    g_wt = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    # Type-1 barcode flow — Lot-1 QC / less weight / commercial attributes
    less_wt = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Less weight for this lot (gross − less = net).",
    )
    quality_check = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Approve / Pending / Reject (lot-level QC).",
    )
    is_exclusive = models.BooleanField(default=False)
    is_rare_find = models.BooleanField(default=False)
    is_limited = models.BooleanField(default=False)
    is_bestseller = models.BooleanField(default=False)
    line_of_business = models.CharField(max_length=128, blank=True, default="")
    sub_line_of_business = models.CharField(max_length=128, blank=True, default="")
    brand = models.CharField(max_length=128, blank=True, default="")
    occasion = models.CharField(max_length=128, blank=True, default="")
    bom = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(max_length=32, default="Open")

    class Meta:
        db_table = "grn_lots"
        ordering = ["-system_created_at"]

    def __str__(self):
        return f"{self.lot_no} ({self.batch_doc_no})"


class GrnBag(SystemBaseModel):
    """
    Physical bag line mapped from a GRN lot to a catalog ProductItem (SKU line).
    """
    lot = models.ForeignKey(
        'GrnLot',
        on_delete=models.CASCADE,
        related_name='bags',
    )
    bag_no = models.CharField(max_length=64)
    product_item = models.ForeignKey(
        'ProductItem',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='grn_bags',
    )
    remark = models.TextField(blank=True, default='')
    # Bag-level receive snapshot (Make Bag). Multiple bags can share one product_item;
    # barcode / stock UI must use THESE fields, not aggregated product_item qty/weights.
    quantity = models.IntegerField(
        null=True,
        blank=True,
        help_text="Pieces received into this bag (item_qty / stock-in).",
    )
    pcs = models.IntegerField(
        null=True,
        blank=True,
        help_text="PCS deducted from the lot for this bag (defaults to quantity).",
    )
    g_wt = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Gross weight received into this bag.",
    )
    stone_wt = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Stone weight received into this bag.",
    )
    net_wt = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Optional net weight snapshot for this bag.",
    )

    class Meta:
        db_table = 'grn_bags'
        ordering = ['-system_created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['lot', 'bag_no'],
                name='uniq_grn_bag_lot_bag_no',
            ),
        ]

    def __str__(self):
        return f"{self.bag_no} (lot {self.lot_id})"


# ---------------------------------------------------------------------------
# Stock & Inventory
# ---------------------------------------------------------------------------

class StockTransaction(SystemBaseModel):
    """
    Every stock movement is one row — auditable, reversible, branch-aware.
    Table: product_stock_transactions
    """
    TXN_TYPE_CHOICES = [
        ('bag_in', 'Bag received into stock'),
        ('bag_out', 'Bag reversed out of stock'),
        ('transfer_in', 'Transfer from another branch'),
        ('transfer_out', 'Transfer to another branch'),
        ('sale', 'Sold to customer'),
        ('return', 'Customer return'),
        ('adjustment', 'Manual stock correction'),
        ('exchange_in', 'Old gold exchange received'),
    ]

    product_item = models.ForeignKey(
        'ProductItem',
        on_delete=models.PROTECT,
        related_name='stock_transactions',
    )
    branch = models.ForeignKey(
        'Branch',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='stock_transactions',
        help_text="Optional. Omit for single-location setups.",
    )
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE_CHOICES, db_index=True)
    quantity = models.IntegerField(help_text="Positive for in, negative for out.")
    bag = models.ForeignKey(
        'GrnBag',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_transactions',
    )
    reference = models.CharField(max_length=128, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    txn_date = models.DateTimeField(auto_now_add=True)
    performed_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stock_transactions_performed',
    )

    class Meta:
        db_table = 'product_stock_transactions'
        ordering = ['-txn_date']
        indexes = [
            models.Index(fields=['bag', 'txn_type'], name='idx_stxn_bag_type'),
            models.Index(fields=['product_item', 'txn_type'], name='idx_stxn_item_type'),
        ]

    def __str__(self):
        return f"{self.txn_type} {self.quantity:+d} — {self.product_item_id}"


# ---------------------------------------------------------------------------
# Tags / Barcodes
# ---------------------------------------------------------------------------

class ProductCodePrefix(SystemBaseModel):
    """
    Registry of product-code prefixes (LR, NP, PRG, …) with an independent
    numeric sequence for barcode/tag values: LR-1001, LR-1002, …
    Table: product_code_prefixes
    """
    category = models.ForeignKey(
        'Category',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='product_code_prefixes',
    )
    subcategory = models.ForeignKey(
        'Subcategory',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='product_code_prefixes',
    )
    prefix = models.CharField(max_length=32, unique=True, db_index=True)
    start_sequence = models.PositiveIntegerField(
        default=1001,
        help_text="Configured starting number when the prefix was created.",
    )
    next_sequence = models.PositiveIntegerField(
        default=1001,
        help_text="Next numeric suffix to assign (e.g. 1001 → tag LR-1001).",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "product_code_prefixes"
        ordering = ["prefix"]
        constraints = [
            models.UniqueConstraint(
                fields=("category_id", "subcategory_id"),
                condition=models.Q(
                    category_id__isnull=False,
                    subcategory_id__isnull=False,
                ),
                name="uniq_product_code_prefix_category_subcategory",
            ),
        ]

    def __str__(self):
        return f"{self.prefix} (next {self.next_sequence})"


class PatternCodeRegistry(SystemBaseModel):
    """
    Pattern code registry scoped to item type (subcategory).
    One item type → many pattern codes; one pattern code → one item type.
    Also keeps 1:1 mapping pattern_code ↔ store_variant_name.
    Table: pattern_code_registry
    """
    category = models.ForeignKey(
        'Category',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='pattern_code_rows',
    )
    subcategory = models.ForeignKey(
        'Subcategory',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='pattern_code_rows',
        help_text='Item type that owns this pattern code.',
    )
    pattern_code = models.CharField(max_length=64, unique=True, db_index=True)
    store_variant_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Store-facing variant label linked to this pattern code.",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "pattern_code_registry"
        ordering = ["pattern_code"]
        constraints = [
            models.UniqueConstraint(
                fields=("store_variant_name",),
                condition=~models.Q(store_variant_name=""),
                name="uniq_pattern_registry_store_variant_name_nonempty",
            ),
        ]

    def __str__(self):
        name = (self.store_variant_name or "").strip()
        return f"{self.pattern_code}" + (f" → {name}" if name else "")


class ProductTag(SystemBaseModel):
    """
    Physical tag (barcode / QR / RFID) for a product item.
    Table: product_tags
    """
    TAG_TYPE_CHOICES = [
        ('barcode', 'Barcode'),
        ('qr', 'QR Code'),
        ('rfid', 'RFID Tag'),
    ]

    product_item = models.ForeignKey(
        'ProductItem',
        on_delete=models.PROTECT,
        related_name='tags',
    )
    tag_type = models.CharField(max_length=10, choices=TAG_TYPE_CHOICES, default='barcode')
    tag_value = models.CharField(max_length=128, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    branch = models.ForeignKey(
        'Branch',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='product_tags',
    )
    printed_at = models.DateTimeField(null=True, blank=True)
    printed_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tags_printed',
    )

    # Snapshot fields — what gets printed on the physical label
    display_name = models.CharField(max_length=255, blank=True, default='')
    metal_info = models.CharField(max_length=128, blank=True, default='')
    weight_info = models.CharField(max_length=64, blank=True, default='')
    # Per-piece weights (entered by operator during tag generation)
    gross_weight = models.CharField(max_length=32, blank=True, default='')
    net_weight = models.CharField(max_length=32, blank=True, default='')
    less_weight = models.CharField(max_length=32, blank=True, default='')
    price_info = models.CharField(max_length=64, blank=True, default='')
    price_type = models.CharField(
        max_length=20, blank=True, default='',
        help_text="MRP, Fix Making, or Normal",
    )
    huid = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Hallmark Unique ID — required when hallmark is printed on the tag.",
    )
    sku_code = models.CharField(max_length=128, blank=True, default='')
    branch_name = models.CharField(max_length=128, blank=True, default='')
    remark = models.CharField(max_length=255, blank=True, default='')

    grn_bag = models.ForeignKey(
        'GrnBag',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_tags',
        help_text='GRN bag this tag was generated from (traceability).',
    )
    MAPPING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('complete', 'Complete'),
    ]
    mapping_status = models.CharField(
        max_length=16,
        choices=MAPPING_STATUS_CHOICES,
        default='pending',
        db_index=True,
    )
    attributes_mapped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'product_tags'
        ordering = ['-system_created_at']

    def __str__(self):
        return f"{self.tag_type}:{self.tag_value} → {self.product_item_id}"


class ProductTagMetal(SystemBaseModel):
    """
    Extra metal + purity + weight lines on a generated tag (multi-metal pieces).
    Primary/total gross stays on ProductTag.gross_weight.
    Table: product_tag_metals
    """
    product_tag = models.ForeignKey(
        'ProductTag',
        on_delete=models.CASCADE,
        related_name='tag_metals',
    )
    metal = models.ForeignKey(
        'Metal',
        on_delete=models.PROTECT,
        related_name='tag_metals',
    )
    purity = models.ForeignKey(
        'MetalMasterRule',
        on_delete=models.PROTECT,
        related_name='tag_metals',
    )
    weight = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'product_tag_metals'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"Tag {self.product_tag_id} · metal {self.metal_id}"


class ProductTagPhoto(SystemBaseModel):
    """
    Photo(s) of the physical printed tag on a piece (audit / reference).
    Table: product_tag_photos
    """
    product_tag = models.ForeignKey(
        'ProductTag',
        on_delete=models.CASCADE,
        related_name='photos',
    )
    image_url = models.TextField()
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'product_tag_photos'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"TagPhoto {self.id} → tag {self.product_tag_id}"


class TagAttributeDefinition(SystemBaseModel):
    """
    Configurable attribute type for post-tag commercial / merchandising mapping.
    Table: tag_attribute_definitions
    """
    DATA_TYPE_CHOICES = [
        ('text', 'Text'),
        ('number', 'Number'),
        ('lookup', 'Lookup'),
        ('boolean', 'Boolean'),
    ]

    code = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=128)
    data_type = models.CharField(max_length=16, choices=DATA_TYPE_CHOICES, default='text')
    lookup = models.ForeignKey(
        'Lookup',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tag_attribute_definitions',
        help_text='When data_type is lookup, options come from this lookup category.',
    )
    required = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    help_text = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'tag_attribute_definitions'
        ordering = ['sort_order', 'label']

    def __str__(self):
        return f"{self.code} — {self.label}"


class ProductTagAttributeValue(SystemBaseModel):
    """
    Per-tag attribute value (Attrib Type → Attrib Value grid).
    Table: product_tag_attribute_values
    """
    product_tag = models.ForeignKey(
        'ProductTag',
        on_delete=models.CASCADE,
        related_name='attribute_values',
    )
    attribute_definition = models.ForeignKey(
        'TagAttributeDefinition',
        on_delete=models.PROTECT,
        related_name='tag_values',
    )
    value_text = models.TextField(blank=True, default='')
    value_number = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    lookup_value = models.ForeignKey(
        'LookupValue',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='product_tag_attribute_values',
    )

    class Meta:
        db_table = 'product_tag_attribute_values'
        constraints = [
            models.UniqueConstraint(
                fields=['product_tag', 'attribute_definition'],
                name='uniq_product_tag_attribute_definition',
            ),
        ]

    def __str__(self):
        return f"Tag {self.product_tag_id} · {self.attribute_definition_id}"


# ---------------------------------------------------------------------------
# Store assisted selling — catalogue quotations
# ---------------------------------------------------------------------------

class CatalogueQuote(SystemBaseModel):
    """
    In-store quotation / order / booking from the assisted-selling catalogue.
    """

    STATUS_DRAFT = 'draft'
    STATUS_ORDER = 'order'
    STATUS_BOOKING = 'booking'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'

    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_ORDER, 'Order'),
        (STATUS_BOOKING, 'Booking'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    quote_number = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.PROTECT,
        related_name='catalogue_quotes',
    )
    contact_mobile = models.CharField(max_length=15, blank=True, default='')
    customer_name_snapshot = models.CharField(max_length=150)
    customer_email_snapshot = models.EmailField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    delivery_address = models.ForeignKey(
        'CustomerAddress',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_quotes',
    )
    delivery_address_snapshot = models.JSONField(default=dict, blank=True)
    expected_delivery_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Promised / expected delivery date for order or booking (CRM pending deliveries).',
    )

    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    gst_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    settle_from_jama = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text='Amount applied from customer JAMA (advance) on this bill.',
    )
    settle_from_scheme = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text='Total applied from savings-scheme kitty on this bill.',
    )
    scheme_settlements = models.JSONField(
        default=list,
        blank=True,
        help_text='Per-scheme kitty: [{customer_scheme_id, amount}, ...]',
    )
    account_balance_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Customer store balance at time of save (JAMA/UDHAR).',
    )
    sale_invoice = models.ForeignKey(
        'SaleInvoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_quotes',
        help_text='POS-style tax invoice when quote is order/booking.',
    )

    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    pricing_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When negotiated rates/discounts revert to baseline (3h from first apply).',
    )

    version = models.PositiveIntegerField(
        default=1,
        help_text='Optimistic lock — increment on each line/total mutation.',
    )
    cart_pricing_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text='Cart-wide discount meta (adjustment ledger snapshot).',
    )
    sales_credit_snapshot = models.JSONField(
        default=list,
        blank=True,
        help_text='Contributor share snapshot at order/booking time.',
    )

    class Meta:
        db_table = 'catalogue_quotes'
        ordering = ['-system_created_at']

    def __str__(self):
        return self.quote_number

    @property
    def pending_amount(self):
        from decimal import Decimal
        jama = self.settle_from_jama or Decimal('0')
        scheme = self.settle_from_scheme or Decimal('0')
        return max(Decimal('0'), self.grand_total - self.paid_amount - jama - scheme)


class CatalogueQuoteLine(SystemBaseModel):
    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    line_no = models.PositiveIntegerField(default=1)
    product_id = models.CharField(max_length=64)
    product_name = models.CharField(max_length=255)
    design_code = models.CharField(max_length=64, blank=True, default='')
    image = models.TextField(blank=True, default='')
    variant_label = models.CharField(max_length=255, blank=True, default='')
    variant_key = models.CharField(max_length=255, blank=True, default='')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    breakdown = models.JSONField(default=dict, blank=True)
    pricing_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text='Per-line discount ledger / baseline snapshots from assisted selling UI.',
    )
    added_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_quote_lines_added',
    )
    is_removed = models.BooleanField(default=False, db_index=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_quote_lines_removed',
    )

    class Meta:
        db_table = 'catalogue_quote_lines'
        ordering = ['line_no', 'id']


class CatalogueQuoteVisit(SystemBaseModel):
    """Open in-store visit tying one customer to one active draft quotation."""

    STATUS_OPEN = 'open'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_CLOSED, 'Closed'),
    ]

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.PROTECT,
        related_name='catalogue_quote_visits',
    )
    quote = models.OneToOneField(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='visit',
    )
    primary_sales_user = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='primary_catalogue_visits',
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'catalogue_quote_visits'
        ordering = ['-system_created_at']


class CatalogueQuoteContributor(SystemBaseModel):
    """Sales credit split between staff working the same quotation."""

    ROLE_PRIMARY = 'primary'
    ROLE_ASSISTANT = 'assistant'
    ROLE_CHOICES = [
        (ROLE_PRIMARY, 'Primary'),
        (ROLE_ASSISTANT, 'Assistant'),
    ]

    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='contributors',
    )
    admin_user = models.ForeignKey(
        'AdminUser',
        on_delete=models.CASCADE,
        related_name='catalogue_quote_contributions',
    )
    share_percent = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_ASSISTANT)

    class Meta:
        db_table = 'catalogue_quote_contributors'
        unique_together = ('quote', 'admin_user')
        ordering = ['role', 'id']


class CatalogueQuoteChangeLog(models.Model):
    """Audit trail for multi-user quotation edits."""

    ACTION_LINE_ADDED = 'line_added'
    ACTION_LINE_REMOVED = 'line_removed'
    ACTION_LINE_UPDATED = 'line_updated'
    ACTION_DISCOUNT_APPLIED = 'discount_applied'
    ACTION_CART_DISCOUNT = 'cart_discount'
    ACTION_CONTRIBUTOR_JOINED = 'contributor_joined'
    ACTION_SHARE_UPDATED = 'share_updated'
    ACTION_QUOTE_CREATED = 'quote_created'
    ACTION_QUOTE_UPDATED = 'quote_updated'
    ACTION_STATUS_CHANGED = 'status_changed'

    ACTION_CHOICES = [
        (ACTION_LINE_ADDED, 'Line added'),
        (ACTION_LINE_REMOVED, 'Line removed'),
        (ACTION_LINE_UPDATED, 'Line updated'),
        (ACTION_DISCOUNT_APPLIED, 'Discount applied'),
        (ACTION_CART_DISCOUNT, 'Cart discount'),
        (ACTION_CONTRIBUTOR_JOINED, 'Contributor joined'),
        (ACTION_SHARE_UPDATED, 'Share updated'),
        (ACTION_QUOTE_CREATED, 'Quote created'),
        (ACTION_QUOTE_UPDATED, 'Quote updated'),
        (ACTION_STATUS_CHANGED, 'Status changed'),
    ]

    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='change_logs',
    )
    actor = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_quote_changes',
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    line = models.ForeignKey(
        CatalogueQuoteLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='change_logs',
    )
    summary = models.CharField(max_length=512, default='')
    payload = models.JSONField(default=dict, blank=True)
    reason = models.CharField(
        max_length=512,
        blank=True,
        default='',
        help_text='Optional staff reason for the manual change.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'catalogue_quote_change_logs'
        ordering = ['-created_at', '-id']


class CatalogueQuoteDiscountApproval(SystemBaseModel):
    """Manager/coordinator approval when negotiated discount exceeds threshold (default 10%)."""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='discount_approvals',
    )
    change_log = models.ForeignKey(
        CatalogueQuoteChangeLog,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='discount_approvals',
    )
    line = models.ForeignKey(
        CatalogueQuoteLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='discount_approvals',
    )
    requested_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_discount_approvals_requested',
    )
    reviewed_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_discount_approvals_reviewed',
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    discount_percent = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    before_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    after_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    threshold_percent = models.DecimalField(max_digits=8, decimal_places=2, default=10)
    request_notes = models.TextField(blank=True, default='')
    review_notes = models.TextField(blank=True, default='')
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'catalogue_quote_discount_approvals'
        ordering = ['-system_created_at']


class CatalogueQuoteLineRemovalRequest(SystemBaseModel):
    """Approval required when one salesperson removes another's line."""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='line_removal_requests',
    )
    line = models.ForeignKey(
        CatalogueQuoteLine,
        on_delete=models.CASCADE,
        related_name='removal_requests',
    )
    requested_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.CASCADE,
        related_name='catalogue_line_removal_requests_made',
    )
    owner_sales_user = models.ForeignKey(
        'AdminUser',
        on_delete=models.CASCADE,
        related_name='catalogue_line_removal_requests_owned',
        help_text='Salesperson who originally added the line (must approve removal).',
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewed_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='catalogue_line_removal_reviews',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    request_notes = models.TextField(blank=True, default='')
    review_notes = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'catalogue_quote_line_removal_requests'
        ordering = ['-system_created_at']


class CatalogueQuotePayment(SystemBaseModel):
    quote = models.ForeignKey(
        CatalogueQuote,
        on_delete=models.CASCADE,
        related_name='payments',
    )
    mode_code = models.CharField(max_length=32)
    mode_name = models.CharField(max_length=64, blank=True, default='')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference_no = models.CharField(max_length=128, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'catalogue_quote_payments'
        ordering = ['id']


# ---------------------------------------------------------------------------
# Accounts — Day Book (daily cash ledger)
# ---------------------------------------------------------------------------

class DayBookDay(SystemBaseModel):
    """Per-day opening balance override for the store cash day book."""

    book_date = models.DateField(unique=True, db_index=True)
    opening_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Manual opening cash. Null = use previous day closing.',
    )
    is_opening_manual = models.BooleanField(default=False)
    closing_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Cached closing cash after entries; speeds up next-day opening.',
    )

    class Meta:
        db_table = 'day_book_days'
        ordering = ['-book_date']

    def __str__(self):
        return f'DayBook {self.book_date}'


class DayBookManualEntry(SystemBaseModel):
    """Manual money-in / money-out lines (expenses, borrowings, repairs, etc.)."""

    DIRECTION_IN = 'IN'
    DIRECTION_OUT = 'OUT'
    DIRECTION_CHOICES = [
        (DIRECTION_IN, 'Money In'),
        (DIRECTION_OUT, 'Money Out'),
    ]

    MODE_ADVANCE = 'ADVANCE'
    MODE_BORROWING = 'BORROWING'
    MODE_UDHAR = 'UDHAR'
    MODE_LENDING = 'LENDING'
    MODE_MISC = 'MISC'
    MODE_HUF = 'HUF'
    MODE_HUF_I = 'HUF_I'
    # Kept for reference; new groups come from Lookup DAY_BOOK_GROUP.
    TRANSACTION_MODE_CHOICES = [
        (MODE_ADVANCE, 'Advance'),
        (MODE_BORROWING, 'Borrowing'),
        (MODE_UDHAR, 'Udhar'),
        (MODE_LENDING, 'Lending'),
        (MODE_MISC, 'Misc.'),
        (MODE_HUF, 'HUF'),
        (MODE_HUF_I, 'HUF I'),
    ]

    entry_date = models.DateField(db_index=True)
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    transaction_mode = models.CharField(
        max_length=50,
        help_text='Day Book group code (built-in or LookupValue under DAY_BOOK_GROUP).',
    )
    # Primary / legacy single mode. When split, set to SPLIT (details in payments).
    payment_mode = models.CharField(max_length=32, blank=True, default='CASH')
    narration = models.TextField(blank=True, default='')
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = 'day_book_manual_entries'
        ordering = ['entry_date', 'id']

    def __str__(self):
        return f'{self.entry_date} {self.direction} {self.amount}'


class DayBookManualPayment(SystemBaseModel):
    """
    Split payment modes for a manual Day Book entry.
    Example: ₹10,000 = ₹5,000 CASH + ₹5,000 CARD.
    """

    entry = models.ForeignKey(
        DayBookManualEntry,
        on_delete=models.CASCADE,
        related_name='payments',
    )
    payment_mode = models.CharField(max_length=32)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'day_book_manual_payments'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.payment_mode} {self.amount} for entry {self.entry_id}'


# ---------------------------------------------------------------------------
# CRM Communication Log
# ---------------------------------------------------------------------------

class CommunicationLog(models.Model):
    """
    Audit trail for every WhatsApp / SMS / call reminder sent from the CRM.
    """
    CHANNEL_WHATSAPP = "WHATSAPP"
    CHANNEL_SMS = "SMS"
    CHANNEL_CALL = "CALL"
    CHANNEL_CHOICES = [
        (CHANNEL_WHATSAPP, "WhatsApp"),
        (CHANNEL_SMS, "SMS"),
        (CHANNEL_CALL, "Call"),
    ]

    TYPE_SCHEME_REMINDER = "SCHEME_REMINDER"
    TYPE_UDHAR_REMINDER = "UDHAR_REMINDER"
    TYPE_INVOICE = "INVOICE"
    TYPE_OFFER = "OFFER"
    TYPE_CUSTOM = "CUSTOM"
    TYPE_CHOICES = [
        (TYPE_SCHEME_REMINDER, "Scheme Payment Reminder"),
        (TYPE_UDHAR_REMINDER, "Udhar/Booking Reminder"),
        (TYPE_INVOICE, "Invoice"),
        (TYPE_OFFER, "Offer / Promotion"),
        (TYPE_CUSTOM, "Custom"),
    ]

    STATUS_SENT = "SENT"
    STATUS_FAILED = "FAILED"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_CHOICES = [
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, db_index=True)
    message_type = models.CharField(max_length=32, choices=TYPE_CHOICES, db_index=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SENT, db_index=True)

    # Recipient
    customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='communication_logs',
    )
    phone = models.CharField(max_length=20)

    # Content reference
    template_name = models.CharField(max_length=128, blank=True, default="")
    parameters = models.TextField(blank=True, default="")
    message_body = models.TextField(blank=True, default="")

    # Related object (optional linkage)
    ref_invoice_id = models.IntegerField(null=True, blank=True, db_index=True)
    ref_instalment_id = models.IntegerField(null=True, blank=True, db_index=True)

    # API response / error
    api_response = models.TextField(blank=True, default="")
    error_detail = models.TextField(blank=True, default="")

    # Campaign grouping (optional)
    campaign_name = models.CharField(max_length=128, blank=True, default="", db_index=True)

    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_by = models.ForeignKey(
        'AdminUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_communication_logs',
    )

    class Meta:
        db_table = 'crm_communication_logs'
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.channel} {self.message_type} → {self.phone} [{self.status}]"


class CrmCustomerVisit(SystemBaseModel):
    """
    CRM walk-in / product-enquiry visit.
    Created when catalogue barcode enquiry / quotation visit opens.
    Lost = visit date with no SaleInvoice for that customer (same local day).
    """

    SOURCE_CATALOGUE = 'catalogue_enquiry'
    SOURCE_BARCODE = 'barcode_scan'
    SOURCE_INVOICE = 'invoice_import'
    SOURCE_CHOICES = [
        (SOURCE_CATALOGUE, 'Catalogue enquiry'),
        (SOURCE_BARCODE, 'Barcode scan'),
        (SOURCE_INVOICE, 'Invoice import'),
    ]

    customer = models.ForeignKey(
        'Customer',
        on_delete=models.PROTECT,
        related_name='crm_visits',
    )
    branch = models.ForeignKey(
        'Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_visits',
        db_column='branch_id',
    )
    quote = models.ForeignKey(
        'CatalogueQuote',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_visits',
    )
    catalogue_visit = models.OneToOneField(
        'CatalogueQuoteVisit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_visit',
    )
    visited_at = models.DateTimeField(db_index=True)
    source = models.CharField(
        max_length=32,
        choices=SOURCE_CHOICES,
        default=SOURCE_CATALOGUE,
        db_index=True,
    )
    buy_next_time = models.BooleanField(
        default=False,
        help_text='Customer said they will buy next time (wishlist / unconverted intent).',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'crm_customer_visits'
        ordering = ['-visited_at']
        indexes = [
            models.Index(fields=['visited_at', 'branch']),
            models.Index(fields=['customer', 'visited_at']),
        ]

    def __str__(self):
        return f"CRM visit {self.customer_id} @ {self.visited_at}"


class CrmProspectContact(SystemBaseModel):
    """
    Phone-diary / cold-call prospect log (not a Customer record).
    Used for campaign suppression so the same number is not called repeatedly.
    """

    CHANNEL_CALL = 'CALL'
    CHANNEL_WHATSAPP = 'WHATSAPP'
    CHANNEL_SMS = 'SMS'
    CHANNEL_CHOICES = [
        (CHANNEL_CALL, 'Call'),
        (CHANNEL_WHATSAPP, 'WhatsApp'),
        (CHANNEL_SMS, 'SMS'),
    ]

    OUTCOME_NO_ANSWER = 'no_answer'
    OUTCOME_INTERESTED = 'interested'
    OUTCOME_NOT_INTERESTED = 'not_interested'
    OUTCOME_CALLBACK = 'callback'
    OUTCOME_WRONG_NUMBER = 'wrong_number'
    OUTCOME_OTHER = 'other'
    OUTCOME_CHOICES = [
        (OUTCOME_NO_ANSWER, 'No answer'),
        (OUTCOME_INTERESTED, 'Interested'),
        (OUTCOME_NOT_INTERESTED, 'Not interested'),
        (OUTCOME_CALLBACK, 'Callback later'),
        (OUTCOME_WRONG_NUMBER, 'Wrong number'),
        (OUTCOME_OTHER, 'Other'),
    ]

    name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20)
    mobile_normalized = models.CharField(
        max_length=15,
        db_index=True,
        help_text='Last 10 digits for suppression matching.',
    )
    branch = models.ForeignKey(
        'Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='crm_prospect_contacts',
        db_column='branch_id',
    )
    campaign_name = models.CharField(max_length=128, blank=True, default='', db_index=True)
    channel = models.CharField(
        max_length=16,
        choices=CHANNEL_CHOICES,
        default=CHANNEL_CALL,
        db_index=True,
    )
    outcome = models.CharField(
        max_length=32,
        choices=OUTCOME_CHOICES,
        default=OUTCOME_OTHER,
        db_index=True,
    )
    notes = models.TextField(blank=True, default='')
    contacted_at = models.DateTimeField(db_index=True)
    matched_customer = models.ForeignKey(
        'Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prospect_contact_matches',
        help_text='Set when mobile already belongs to an enrolled customer.',
    )

    class Meta:
        db_table = 'crm_prospect_contacts'
        ordering = ['-contacted_at']
        indexes = [
            models.Index(fields=['mobile_normalized', 'contacted_at']),
            models.Index(fields=['campaign_name', 'contacted_at']),
        ]

    def __str__(self):
        return f"Prospect {self.name} {self.mobile_normalized} @ {self.contacted_at}"


class CrmScheduledReminder(SystemBaseModel):
    """
    Scheduled CRM reminder (WhatsApp / SMS / Call) for a date+time.
    Processed by management command or POST /crm/reminders/process-scheduled/.
    """

    CHANNEL_WHATSAPP = "WHATSAPP"
    CHANNEL_SMS = "SMS"
    CHANNEL_CALL = "CALL"
    CHANNEL_CHOICES = [
        (CHANNEL_WHATSAPP, "WhatsApp"),
        (CHANNEL_SMS, "SMS"),
        (CHANNEL_CALL, "Call"),
    ]

    TYPE_SCHEME_REMINDER = "SCHEME_REMINDER"
    TYPE_UDHAR_REMINDER = "UDHAR_REMINDER"
    TYPE_OFFER = "OFFER"
    TYPE_CUSTOM = "CUSTOM"
    TYPE_CHOICES = [
        (TYPE_SCHEME_REMINDER, "Scheme Payment Reminder"),
        (TYPE_UDHAR_REMINDER, "Udhar/Booking Reminder"),
        (TYPE_OFFER, "Offer / Promotion"),
        (TYPE_CUSTOM, "Custom"),
    ]

    STATUS_PENDING = "PENDING"
    STATUS_SENT = "SENT"
    STATUS_FAILED = "FAILED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_SKIPPED, "Skipped"),
    ]

    customer = models.ForeignKey(
        "Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_reminders",
    )
    phone = models.CharField(max_length=20)
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, db_index=True)
    message_type = models.CharField(
        max_length=32,
        choices=TYPE_CHOICES,
        default=TYPE_CUSTOM,
        db_index=True,
    )
    scheduled_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    template_name = models.CharField(max_length=128, blank=True, default="")
    parameters = models.TextField(blank=True, default="")
    message_body = models.TextField(blank=True, default="")
    campaign_name = models.CharField(max_length=128, blank=True, default="", db_index=True)
    ref_instalment_id = models.IntegerField(null=True, blank=True, db_index=True)
    ref_invoice_id = models.IntegerField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)
    error_detail = models.TextField(blank=True, default="")
    communication_log = models.ForeignKey(
        "CommunicationLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_reminders",
    )

    class Meta:
        db_table = "crm_scheduled_reminders"
        ordering = ["scheduled_at"]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["channel", "status"]),
        ]

    def __str__(self):
        return f"{self.channel} {self.message_type} @ {self.scheduled_at} [{self.status}]"


class CrmServiceTicket(SystemBaseModel):
    """
    Lightweight CRM ticket for repairs, exchanges, and returns.
    Used for Customer List segments and Customer 360 listings.
    """

    TYPE_REPAIR = "REPAIR"
    TYPE_EXCHANGE = "EXCHANGE"
    TYPE_RETURN = "RETURN"
    TYPE_CHOICES = [
        (TYPE_REPAIR, "Repair"),
        (TYPE_EXCHANGE, "Exchange"),
        (TYPE_RETURN, "Return"),
    ]

    STATUS_OPEN = "OPEN"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_READY = "READY"
    STATUS_CLOSED = "CLOSED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_READY, "Ready"),
        (STATUS_CLOSED, "Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    customer = models.ForeignKey(
        "Customer",
        on_delete=models.CASCADE,
        related_name="crm_service_tickets",
    )
    ticket_type = models.CharField(max_length=16, choices=TYPE_CHOICES, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
        db_index=True,
    )
    title = models.CharField(max_length=200)
    item_description = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    opened_at = models.DateTimeField(db_index=True)
    expected_ready_date = models.DateField(null=True, blank=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    branch = models.ForeignKey(
        "Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_service_tickets",
        db_column="branch_id",
    )
    ref_invoice_id = models.IntegerField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "crm_service_tickets"
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["ticket_type", "status"]),
            models.Index(fields=["customer", "ticket_type"]),
        ]

    def __str__(self):
        return f"{self.ticket_type} {self.title} [{self.status}]"


class CrmStoreContact(SystemBaseModel):
    """
    Store-initiated customer contact log.
    Client requirement: always know the customer was contacted from the store,
    with conversation remarks and reason of contact.
    """

    CHANNEL_IN_STORE = "IN_STORE"
    CHANNEL_CALL = "CALL"
    CHANNEL_WHATSAPP = "WHATSAPP"
    CHANNEL_CHOICES = [
        (CHANNEL_IN_STORE, "In store"),
        (CHANNEL_CALL, "Call"),
        (CHANNEL_WHATSAPP, "WhatsApp"),
    ]

    REASON_PRODUCT_ENQUIRY = "PRODUCT_ENQUIRY"
    REASON_SCHEME = "SCHEME"
    REASON_REPAIR = "REPAIR"
    REASON_EXCHANGE = "EXCHANGE"
    REASON_FOLLOW_UP = "FOLLOW_UP"
    REASON_OFFER = "OFFER"
    REASON_BOOKING = "BOOKING"
    REASON_COMPLAINT = "COMPLAINT"
    REASON_OTHER = "OTHER"
    REASON_CHOICES = [
        (REASON_PRODUCT_ENQUIRY, "Product enquiry"),
        (REASON_SCHEME, "Scheme / savings"),
        (REASON_REPAIR, "Repair"),
        (REASON_EXCHANGE, "Exchange / return"),
        (REASON_FOLLOW_UP, "Follow-up"),
        (REASON_OFFER, "Offer / promotion"),
        (REASON_BOOKING, "Booking / order"),
        (REASON_COMPLAINT, "Complaint / feedback"),
        (REASON_OTHER, "Other"),
    ]

    customer = models.ForeignKey(
        "Customer",
        on_delete=models.CASCADE,
        related_name="crm_store_contacts",
    )
    branch = models.ForeignKey(
        "Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="crm_store_contacts",
        db_column="branch_id",
    )
    channel = models.CharField(
        max_length=16,
        choices=CHANNEL_CHOICES,
        default=CHANNEL_IN_STORE,
        db_index=True,
    )
    contact_reason = models.CharField(
        max_length=32,
        choices=REASON_CHOICES,
        db_index=True,
    )
    remarks = models.TextField(
        help_text="Conversation remarks — what was discussed with the customer.",
    )
    contacted_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "crm_store_contacts"
        ordering = ["-contacted_at"]
        indexes = [
            models.Index(fields=["customer", "contacted_at"]),
            models.Index(fields=["channel", "contact_reason"]),
        ]

    def __str__(self):
        return f"{self.channel} {self.contact_reason} → customer {self.customer_id}"
