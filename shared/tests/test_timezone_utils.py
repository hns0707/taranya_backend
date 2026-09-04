"""
Validate timezone handling: UTC 20:30 should be IST next calendar day (17 Feb 2026).
"""
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone as tz


@override_settings(USE_TZ=True, TIME_ZONE='Asia/Kolkata')
class TimezoneUtilsTests(TestCase):
    """Test that business date logic uses India date correctly."""

    def test_utc_2030_is_ist_next_day(self):
        """UTC 2026-02-16 20:30 -> IST 2026-02-17 02:00 -> date = 17 Feb 2026."""
        utc_aware = datetime(2026, 2, 16, 20, 30, 0, tzinfo=dt_tz.utc)
        ist_date = tz.localtime(utc_aware).date()
        self.assertEqual(ist_date.year, 2026)
        self.assertEqual(ist_date.month, 2)
        self.assertEqual(ist_date.day, 17)

    def test_localdate_uses_settings_timezone(self):
        """timezone.localdate() returns today in Asia/Kolkata when now is UTC 20:30 16 Feb."""
        with patch.object(tz, 'now') as mock_now:
            mock_now.return_value = datetime(2026, 2, 16, 20, 30, 0, tzinfo=dt_tz.utc)
            today = tz.localdate()
            self.assertEqual(today.day, 17)
            self.assertEqual(today.month, 2)
            self.assertEqual(today.year, 2026)
