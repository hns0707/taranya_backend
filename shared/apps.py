"""
App configuration for the shared module.
"""
from django.apps import AppConfig

class SharedConfig(AppConfig):
    """
    Configuration for the shared app.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'shared'
    verbose_name = 'Shared'