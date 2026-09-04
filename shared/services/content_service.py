"""
Shared service for content-related business logic.
"""
from shared.models import FAQ, CMSPage


def get_active_faqs():
    """
    Get all active FAQs.
    
    Returns:
        QuerySet: A queryset of all active FAQs.
    """
    return FAQ.objects.filter(is_active=True)


def get_cms_page(page_key):
    """
    Get a CMS page by its key.

    Args:
        page_key (str): The key of the CMS page.

    Returns:
        CMSPage: The CMS page, or None if not found.
    """
    try:
        return CMSPage.objects.get(page_key=page_key, is_active=True)
    except CMSPage.DoesNotExist:
        return None


def get_default_cms_page():
    """
    Get the default CMS page (first active CMS page).

    Returns:
        CMSPage: The first active CMS page, or None if none exist.
    """
    return CMSPage.objects.filter(is_active=True)