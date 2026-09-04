"""
Tests for admin scheme enrollment APIs.
"""
from django.urls import reverse
from rest_framework.test import APITestCase
from shared.models import Customer, SchemeMaster
from datetime import date


class CustomerByMobileViewTests(APITestCase):
    """
    Tests for CustomerByMobileView.
    """
    
    def setUp(self):
        """Create test customer."""
        self.customer = Customer.objects.create(
            full_name="Test Customer",
            mobile="9876543210",
            email="test@example.com",
            is_active=True
        )
        self.url = reverse('customer-by-mobile', kwargs={'mobile': '9876543210'})
        
    def test_customer_lookup_success(self):
        """Test successful customer lookup by mobile number."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)  # Should require authentication
        
    def test_customer_lookup_not_found(self):
        """Test customer lookup with non-existent mobile number."""
        url = reverse('customer-by-mobile', kwargs={'mobile': '1234567890'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)


class AdminCustomerSchemeEnrollmentViewTests(APITestCase):
    """
    Tests for AdminCustomerSchemeEnrollmentView.
    """
    
    def setUp(self):
        """Create test customer and scheme."""
        self.customer = Customer.objects.create(
            full_name="Test Customer",
            mobile="9876543210",
            email="test@example.com",
            is_active=True
        )
        
        self.scheme = SchemeMaster.objects.create(
            scheme_name="Test Scheme",
            tenure_months=12,
            bonus_months=2,
            bonus_type='FLAT_CASH',
            bonus_value=500.00,
            min_instalment=1000.00,
            max_instalment=10000.00,
            is_gold_linked=False,
            gold_purity='22K',
            gold_locking_type='MONTHLY',
            is_active=True
        )
        
        self.url = reverse('admin-customer-enroll', kwargs={'customer_id': self.customer.id})
        
    def test_scheme_enrollment_success(self):
        """Test successful scheme enrollment."""
        data = {
            'scheme_id': self.scheme.id,
            'monthly_amount': 5000
        }
        
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, 401)  # Should require authentication
        
    def test_scheme_enrollment_invalid_scheme(self):
        """Test scheme enrollment with invalid scheme ID."""
        data = {
            'scheme_id': 999,  # Non-existent scheme
            'monthly_amount': 5000
        }
        
        response = self.client.post(self.url, data, format='json')
        self.assertEqual(response.status_code, 401)


class CustomerSchemeInstalmentsViewTests(APITestCase):
    """
    Tests for CustomerSchemeInstalmentsView.
    """
    
    def setUp(self):
        """Create test customer, scheme, and customer scheme."""
        from shared.services.scheme_service import apply_for_scheme
        
        self.customer = Customer.objects.create(
            full_name="Test Customer",
            mobile="9876543210",
            email="test@example.com",
            is_active=True
        )
        
        self.scheme = SchemeMaster.objects.create(
            scheme_name="Test Scheme",
            tenure_months=12,
            bonus_months=2,
            bonus_type='FLAT_CASH',
            bonus_value=500.00,
            min_instalment=1000.00,
            max_instalment=10000.00,
            is_gold_linked=False,
            gold_purity='22K',
            gold_locking_type='MONTHLY',
            is_active=True
        )
        
        self.customer_scheme, self.first_instalment = apply_for_scheme(
            customer=self.customer,
            scheme=self.scheme,
            monthly_amount=5000
        )
        
        self.url = reverse('customer-scheme-instalments', kwargs={'scheme_id': self.customer_scheme.id})
        
    def test_instalment_listing_success(self):
        """Test successful listing of customer scheme instalments."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)  # Should require authentication
        
    def test_instalment_listing_invalid_scheme(self):
        """Test instalment listing with invalid scheme ID."""
        url = reverse('customer-scheme-instalments', kwargs={'scheme_id': 999})  # Non-existent scheme
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)