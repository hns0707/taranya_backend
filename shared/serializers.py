"""
Shared serializers for the project.
"""
from rest_framework import serializers
from django.db import models
from shared.models import SchemeMaster, CustomerScheme, CustomerKYC, SchemeInstalment, Payment, Lookup, LookupValue


class LookupCategoryListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for active lookup categories.
    """
    class Meta:
        model = Lookup
        fields = ['code', 'name', 'description']


class LookupDropdownValueSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for dropdown usage.
    """
    class Meta:
        model = LookupValue
        fields = ['code', 'label']


class BulkLookupRequestSerializer(serializers.Serializer):
    """
    Request serializer for bulk lookup dropdown endpoint.
    """
    codes = serializers.ListField(
        child=serializers.CharField(max_length=50),
        allow_empty=False
    )

    def validate_codes(self, value):
        normalized = [code.strip().upper() for code in value if code and code.strip()]
        if not normalized:
            raise serializers.ValidationError("At least one lookup code is required.")
        return list(dict.fromkeys(normalized))


class LookupSerializer(serializers.ModelSerializer):
    """
    Serializer for Lookup model.
    """
    class Meta:
        model = Lookup
        fields = ['code', 'name', 'description', 'is_active']


class LookupValueSerializer(serializers.ModelSerializer):
    """
    Serializer for LookupValue model.
    """
    class Meta:
        model = LookupValue
        fields = ['code', 'label', 'is_active', 'sort_order']


class SchemeMasterSerializer(serializers.ModelSerializer):
    """
    Serializer for SchemeMaster model.
    """
    total_scheme_duration = serializers.SerializerMethodField()
    benefits = serializers.SerializerMethodField()

    class Meta:
        model = SchemeMaster
        fields = ['id', 'scheme_code', 'scheme_name', 'tenure_months', 'gold_purity', 
                 'min_instalment', 'max_instalment', 'scheme_description', 
                 'marketing_banner_url', 'highlight_tags', 'is_active', 
                 'system_created_at', 'system_updated_at', 'total_scheme_duration', 'benefits']

    def get_total_scheme_duration(self, obj):
        """Calculate total scheme duration including bonus months."""
        bonus_months = obj.benefits.filter(
            benefit_type='BONUS_MONTHS'
        ).aggregate(
            total=models.Sum('benefit_months')
        )['total'] or 0

        return obj.tenure_months + bonus_months

    def get_benefits(self, obj):
        """Return scheme benefits as an array."""
        return [
            {
                "benefit_type": b.benefit_type,
                "benefit_value": b.benefit_value,
                "benefit_percentage": b.benefit_percentage,
                "benefit_months": b.benefit_months
            }
            for b in obj.benefits.all()
        ]


class CustomerSchemeSerializer(serializers.ModelSerializer):
    """
    Serializer for CustomerScheme model.
    """
    # Status field - read as code, write as code
    scheme_status = serializers.CharField(source='scheme_status.code', read_only=True)
    
    # Write fields
    scheme_status_code = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = CustomerScheme
        fields = '__all__'
        extra_fields = ['scheme_status_code']
    
    def to_representation(self, instance):
        """Include scheme_reference in the serialized output."""
        representation = super().to_representation(instance)
        representation['scheme_reference'] = instance.scheme_reference
        return representation

    def create(self, validated_data):
        # Handle status codes
        scheme_status_code = validated_data.pop('scheme_status_code', 'PENDING')

        # Get or create lookup values
        try:
            scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code=scheme_status_code)
        except LookupValue.DoesNotExist:
            raise serializers.ValidationError("Invalid status code")

        validated_data['scheme_status'] = scheme_status

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle status codes
        if 'scheme_status_code' in validated_data:
            scheme_status_code = validated_data.pop('scheme_status_code')
            try:
                instance.scheme_status = LookupValue.objects.get(lookup__code='SCHEME_STATUS', code=scheme_status_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid scheme status code")

        return super().update(instance, validated_data)


class CustomerKYCSerializer(serializers.ModelSerializer):
    """
    Serializer for CustomerKYC model.
    """
    # Status field - read as code, write as code
    status = serializers.CharField(source='status.code', read_only=True)
    status_code = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = CustomerKYC
        fields = '__all__'
        extra_fields = ['status_code']

    def create(self, validated_data):
        # Handle status code
        status_code = validated_data.pop('status_code', 'PENDING')
        try:
            validated_data['status'] = LookupValue.objects.get(lookup__code='KYC_STATUS', code=status_code)
        except LookupValue.DoesNotExist:
            raise serializers.ValidationError("Invalid status code")

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle status code
        if 'status_code' in validated_data:
            status_code = validated_data.pop('status_code')
            try:
                instance.status = LookupValue.objects.get(lookup__code='KYC_STATUS', code=status_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid status code")

        return super().update(instance, validated_data)


class SchemeInstalmentSerializer(serializers.ModelSerializer):
    """
    Serializer for SchemeInstalment model.
    """
    # Status field - read as code, write as code
    status = serializers.CharField(source='status.code', read_only=True)
    status_code = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = SchemeInstalment
        fields = '__all__'
        extra_fields = ['status_code']

    def create(self, validated_data):
        # Handle status code
        status_code = validated_data.pop('status_code', 'PENDING')
        try:
            validated_data['status'] = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code=status_code)
        except LookupValue.DoesNotExist:
            raise serializers.ValidationError("Invalid status code")

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle status code
        if 'status_code' in validated_data:
            status_code = validated_data.pop('status_code')
            try:
                instance.status = LookupValue.objects.get(lookup__code='INSTALLMENT_STATUS', code=status_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid status code")

        return super().update(instance, validated_data)


class PaymentSerializer(serializers.ModelSerializer):
    """
    Serializer for Payment model. Supports optional payment_mode and PaymentCollection.
    """
    # Status field - read as code, write as code
    payment_status = serializers.CharField(source='payment_status.code', read_only=True)
    payment_status_code = serializers.CharField(write_only=True, required=False)

    # Payment mode - from payment_mode or first collection; allow_null for split/legacy
    payment_mode = serializers.SerializerMethodField(read_only=True)
    payment_mode_code = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Payment
        fields = '__all__'
        extra_fields = ['payment_status_code', 'payment_mode_code']

    def get_payment_mode(self, obj):
        from shared.helper import get_payment_mode_display
        return get_payment_mode_display(obj)

    def validate_payment_mode_code(self, value):
        """Validate that payment_mode exists in PAYMENT_MODE lookup."""
        exists = LookupValue.objects.filter(
            lookup__code='PAYMENT_MODE',
            code=value,
            is_active=True
        ).exists()
        if not exists:
            raise serializers.ValidationError(f"Invalid payment mode: {value}")
        return value

    def create(self, validated_data):
        # Handle status code
        payment_status_code = validated_data.pop('payment_status_code', 'INITIATED')
        try:
            validated_data['payment_status'] = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code=payment_status_code)
        except LookupValue.DoesNotExist:
            raise serializers.ValidationError("Invalid payment status code")
            
        # Handle payment mode
        payment_mode_code = validated_data.pop('payment_mode_code', None)
        if payment_mode_code:
            try:
                validated_data['payment_mode'] = LookupValue.objects.get(lookup__code='PAYMENT_MODE', code=payment_mode_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid payment mode code")

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle status code
        if 'payment_status_code' in validated_data:
            payment_status_code = validated_data.pop('payment_status_code')
            try:
                instance.payment_status = LookupValue.objects.get(lookup__code='PAYMENT_STATUS', code=payment_status_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid payment status code")
                
        # Handle payment mode
        if 'payment_mode_code' in validated_data:
            payment_mode_code = validated_data.pop('payment_mode_code')
            try:
                instance.payment_mode = LookupValue.objects.get(lookup__code='PAYMENT_MODE', code=payment_mode_code)
            except LookupValue.DoesNotExist:
                raise serializers.ValidationError("Invalid payment mode code")

        return super().update(instance, validated_data)
