"""
Shared utility functions for the eCommerce Jewellery Savings Platform.
"""
import re

from django.utils import timezone

def validate_email(email: str) -> bool:
    """
    Validate an email address using a simple regex pattern.
    
    Args:
        email (str): The email address to validate.
    
    Returns:
        bool: True if the email is valid, False otherwise.
    """
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None

def validate_phone_number(phone_number: str) -> bool:
    """
    Validate a phone number (10 digits).
    
    Args:
        phone_number (str): The phone number to validate.
    
    Returns:
        bool: True if the phone number is valid, False otherwise.
    """
    pattern = r"^[0-9]{10}$"
    return re.match(pattern, phone_number) is not None

def get_current_timestamp() -> str:
    """
    Get the current timestamp in a formatted string (timezone-aware UTC).
    Returns:
        str: Current timestamp in the format "YYYY-MM-DD HH:MM:SS".
    """
    return timezone.now().strftime("%Y-%m-%d %H:%M:%S")