"""
Views for OTP login & authentication in the customer app.
"""
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.db.models import Q
from django.contrib.auth.hashers import check_password
from customer.auth.customer_jwt import CustomerJWTAuthentication
from shared.models import Customer, CustomerOTP, CustomerKYC
from datetime import timedelta
import random
from django.utils import timezone
from shared.services.sms_service import send_login_otp


class RequestOTPView(generics.CreateAPIView):
    """
    API endpoint to request OTP for customer login.
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        mobile = request.data.get("phone")

        if not mobile:
            return Response(
                {"error": "Mobile number is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # OPTIONAL: Invalidate previous OTPs
        CustomerOTP.objects.filter(
            mobile=mobile,
            purpose="LOGIN",
            is_used=False
        ).update(is_used=True)

        # Generate OTP
        otp = self.generate_otp()

        # Save OTP
        expires_at = timezone.now() + timedelta(minutes=10)
        CustomerOTP.objects.create(
            mobile=mobile,
            otp_code=otp,
            purpose="LOGIN",
            expires_at=expires_at
        )

        # Send OTP via SMS
        ok, _detail = send_login_otp(mobile, otp)
        if not ok:
            return Response(
                {"error": "Unable to send OTP. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({"message": "OTP sent successfully"}, status=status.HTTP_200_OK)

    def generate_otp(self, length=4):
        """
        Generate numeric OTP
        """
        return f"{random.randint(0, (10**length) - 1):0{length}d}"


class VerifyOTPView(generics.CreateAPIView):
    """
    API endpoint to verify OTP and authenticate customer.
    """
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        """Verify OTP and return authentication token."""
        mobile = request.data.get('phone')
        otp_code = str(request.data.get('otp', '')).strip()
        
        if not mobile or not otp_code:
            return Response({"error": "Mobile and OTP are required"}, status=status.HTTP_400_BAD_REQUEST)

        if not otp_code.isdigit() or len(otp_code) != 4:
            return Response({"error": "OTP must be a 4-digit numeric code"}, status=status.HTTP_400_BAD_REQUEST)

        is_valid, otp_record, error_message = self.validate_otp(mobile, otp_code, 'LOGIN')

        if not is_valid:
            return Response({"error": error_message}, status=status.HTTP_400_BAD_REQUEST)

        # Mark OTP as used atomically to prevent reuse in concurrent requests.
        updated_count = CustomerOTP.objects.filter(id=otp_record.id, is_used=False).update(is_used=True)
        if updated_count == 0:
            return Response({"error": "OTP has already been used"}, status=status.HTTP_400_BAD_REQUEST)
        
        # Get or create customer
        customer, created = Customer.objects.get_or_create(mobile=mobile, defaults={'is_active': True})

        # Get KYC if exists
        kyc = CustomerKYC.objects.filter(customer=customer).first()

        # Generate JWT token
        token = CustomerJWTAuthentication.generate_customer_token(customer)

        # Build customer object
        customer_data = {
            "id": customer.id if not created else "",
            "full_name": customer.full_name or "",
            "mobile": customer.mobile,
            "email": customer.email or "",
            "gender": customer.gender or "",
            "date_of_birth": str(customer.date_of_birth) if customer.date_of_birth else "",
        }

        return Response({
            "token": token,
            "customer": customer_data
        }, status=status.HTTP_200_OK)
        
    
    def validate_otp(self, mobile, otp_code, purpose):
        """
        Validate an OTP code - Customer app specific logic.
        
        Args:
            mobile (str): Mobile number.
            otp_code (str): OTP code to validate.
            purpose (str): Purpose of the OTP (e.g., 'LOGIN', 'REGISTER').
        
        Returns:
            tuple: (is_valid, otp_record, error_message) where is_valid is a boolean,
            otp_record is the OTP record if valid, and error_message is set when invalid.
        """
        otp_record = CustomerOTP.objects.filter(
            mobile=mobile,
            otp_code=otp_code,
            purpose=purpose
        ).order_by('-system_created_at').first()

        if not otp_record:
            return False, None, "Invalid OTP"

        if otp_record.is_used:
            return False, None, "OTP has already been used"

        now = timezone.now()
        expires_at = otp_record.expires_at
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
        if expires_at <= now:
            return False, None, "OTP has expired"

        return True, otp_record, None

class LogoutView(generics.CreateAPIView):
    """
    API endpoint for customer logout.
    """
    permission_classes = [AllowAny]
    
    def perform_create(self, serializer):
        """Handle logout."""
        pass


class PasswordLoginView(generics.CreateAPIView):
    """
    API endpoint to authenticate customer using customer id/code + password.
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        customer_id = str(request.data.get("customer_id", "")).strip()
        password = str(request.data.get("password", ""))

        if not customer_id or not password:
            return Response(
                {"error": "customer_id and password are required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        qs = Customer.objects.filter(
            Q(customer_code__iexact=customer_id) |
            Q(mobile=customer_id)
        )
        if customer_id.isdigit():
            qs = qs | Customer.objects.filter(id=int(customer_id))

        customer = qs.first()
        if not customer:
            return Response({"error": "Customer not found"}, status=status.HTTP_404_NOT_FOUND)

        if not customer.is_active:
            return Response({"error": "Customer account is inactive"}, status=status.HTTP_403_FORBIDDEN)

        stored_hash = customer.password_hash or ""
        if not stored_hash:
            return Response({"error": "Password is not set for this customer"}, status=status.HTTP_400_BAD_REQUEST)

        # Support both hashed and legacy plain-text password storage.
        password_valid = False
        try:
            password_valid = check_password(password, stored_hash)
        except Exception:
            password_valid = False
        if not password_valid:
            password_valid = (password == stored_hash)

        if not password_valid:
            return Response({"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        customer.last_login_at = timezone.now()
        customer.save(update_fields=["last_login_at", "system_updated_at"])

        token = CustomerJWTAuthentication.generate_customer_token(customer)
        customer_data = {
            "id": customer.id,
            "customer_code": customer.customer_code or "",
            "full_name": customer.full_name or "",
            "mobile": customer.mobile or "",
            "email": customer.email or "",
            "gender": customer.gender or "",
            "date_of_birth": str(customer.date_of_birth) if customer.date_of_birth else "",
        }
        return Response({"token": token, "customer": customer_data}, status=status.HTTP_200_OK)
