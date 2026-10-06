from datetime import datetime, timezone
import unittest

from permissions import history_cutoff, history_visible


class HistoryPermissionTest(unittest.TestCase):
    def test_six_calendar_months_uses_shanghai_local_time_and_includes_boundary(self):
        now = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)
        cutoff = history_cutoff(6, now=now)
        self.assertEqual(cutoff, datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc))
        self.assertTrue(history_visible(cutoff.isoformat(), cutoff))
        self.assertFalse(history_visible("2026-04-01T06:29:59+00:00", cutoff))

    def test_month_end_is_clamped_and_invalid_creation_time_is_hidden(self):
        cutoff = history_cutoff(6, now=datetime(2026, 8, 31, 8, tzinfo=timezone.utc))
        self.assertEqual(cutoff, datetime(2026, 2, 28, 8, tzinfo=timezone.utc))
        self.assertFalse(history_visible("invalid", cutoff))
        self.assertTrue(history_visible("invalid", None))
