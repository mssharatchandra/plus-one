import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from plus_one.timeparse import parse_reminder, parse_snooze_duration, quiet_hours_next

UTC = timezone.utc
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)

class ReminderTimeTests(unittest.TestCase):
    def test_tomorrow(self):
        proposal = parse_reminder('remind me to submit report tomorrow at 9am', 'Asia/Kolkata', NOW)
        self.assertEqual(proposal['title'], 'submit report')
        self.assertEqual(proposal['due_at'], '2026-11-01T03:30:00+00:00')
        self.assertIn('09:00', proposal['local_display'])
        self.assertIn('Asia/Kolkata', proposal['local_display'])

    def test_iso_date_and_24h(self):
        p = parse_reminder('remind me to call Sam on 2026-11-02 at 14:00', 'America/New_York', NOW)
        self.assertEqual(p['due_at'], '2026-11-02T19:00:00+00:00')
        self.assertEqual(p['timezone'], 'America/New_York')

    def test_relative_duration_is_elapsed_across_dst(self):
        before_fold = datetime(2026, 11, 1, 5, 45, tzinfo=UTC)
        p = parse_reminder('remind me to stretch in 30 minutes', 'America/New_York', before_fold)
        self.assertEqual(p['due_at'], '2026-11-01T06:15:00+00:00')
        self.assertIn('01:15', p['local_display'])
        self.assertIn('EST', p['local_display'])

    def test_today_uses_profile_local_date(self):
        now = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)
        p = parse_reminder('remind me to stretch today at 7am', 'Asia/Kolkata', now)
        self.assertEqual(p['due_at'], '2026-11-01T01:30:00+00:00')

    def test_noon_midnight_and_case(self):
        for clock, expected in [('12am', '00:00'), ('12 PM', '12:00'), ('00:15', '00:15')]:
            with self.subTest(clock=clock):
                p = parse_reminder(f'Remind Me To call tomorrow at {clock}', 'UTC', NOW)
                self.assertIn(expected, p['due_at'])

    def test_reject_past_or_equal(self):
        for clock in ['08:00', '09:00']:
            with self.assertRaisesRegex(ValueError, 'passed'):
                parse_reminder(f'remind me to report today at {clock}', 'UTC', NOW)

    def test_reject_ambiguous_clock(self):
        with self.assertRaisesRegex(ValueError, 'AM or PM'):
            parse_reminder('remind me to report tomorrow at 9', 'UTC', NOW)

    def test_reject_dst_fold(self):
        with self.assertRaisesRegex(ValueError, 'occurs twice'):
            parse_reminder('remind me to call on 2026-11-01 at 01:30', 'America/New_York', NOW)

    def test_reject_dst_gap(self):
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            parse_reminder('remind me to call on 2027-03-14 at 02:30', 'America/New_York', NOW)

    def test_bad_inputs(self):
        cases = [
            'remind me to call later', 'remind me to call Friday at 9am',
            'remind me to call on 2026-02-30 at 09:00',
            'remind me to call tomorrow at 24:00', 'remind me to call tomorrow at 13am',
            'remind me to call tomorrow at 10:99', 'remind me to call tomorrow at noon',
            'remind me to call in 0 minutes', 'remind me to call in 31 days',
            'remind me to call\ntomorrow at 9am', 'call tomorrow at 9am',
            'remind me to ' + 'a' * 501 + ' in 10 minutes',
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_reminder(text, 'UTC', NOW)

    def test_bad_timezone(self):
        for zone in ['Mars/Base', '', '../etc/passwd', None]:
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                parse_reminder('remind me to call in 5 minutes', zone, NOW)

    def test_naive_clock(self):
        with self.assertRaisesRegex(ValueError, 'aware'):
            parse_reminder('remind me to call in 5 minutes', 'UTC', datetime(2026, 10, 31))

class DurationTests(unittest.TestCase):
    def test_units(self):
        for text, expected in [('10m', 600), ('2 hours', 7200), ('1 min', 60), ('30 days', 2592000), ('1s', 1), (' 2 HRS ', 7200)]:
            with self.subTest(text=text):
                self.assertEqual(parse_snooze_duration(text), expected)

    def test_invalid(self):
        for text in ['', '-1m', '0m', '1.5h', '1h 30m', '31days', '999999999999m', 'later', '10', None]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_snooze_duration(text)

class QuietHoursTests(unittest.TestCase):
    def test_overnight(self):
        instant = datetime(2026, 10, 31, 23, 15, tzinfo=UTC)
        self.assertEqual(quiet_hours_next(instant, 'UTC', '22:00', '08:00'), datetime(2026, 11, 1, 8, tzinfo=UTC))
        early = datetime(2026, 10, 31, 1, 15, tzinfo=UTC)
        self.assertEqual(quiet_hours_next(early, 'UTC', '22:00', '08:00'), datetime(2026, 10, 31, 8, tzinfo=UTC))

    def test_boundaries_and_outside_unchanged(self):
        for hour, quiet in [(8, False), (12, False), (21, False), (22, True)]:
            instant = NOW.replace(hour=hour, minute=0)
            result = quiet_hours_next(instant, 'UTC', '22:00', '08:00')
            self.assertEqual(result != instant, quiet)

    def test_same_day(self):
        instant = NOW.replace(hour=12)
        self.assertEqual(quiet_hours_next(instant, 'UTC', '11:00', '13:00'), NOW.replace(hour=13))

    def test_timezone_and_date(self):
        instant = NOW.replace(hour=20)
        self.assertEqual(quiet_hours_next(instant, 'Asia/Kolkata', '22:00', '08:00'), datetime(2026, 11, 1, 2, 30, tzinfo=UTC))

    def test_fold_uses_later_end(self):
        instant = datetime(2026, 11, 1, 5, 10, tzinfo=UTC)
        self.assertEqual(quiet_hours_next(instant, 'America/New_York', '22:00', '01:30'), datetime(2026, 11, 1, 6, 30, tzinfo=UTC))

    def test_gap_end_advances_to_first_valid_minute(self):
        instant = datetime(2027, 3, 14, 6, 0, tzinfo=UTC)
        self.assertEqual(quiet_hours_next(instant, 'America/New_York', '22:00', '02:30'), datetime(2027, 3, 14, 7, 0, tzinfo=UTC))

    def test_gap_can_skip_whole_nonquiet_interval(self):
        instant = datetime(2027, 3, 14, 6, 0, tzinfo=UTC)
        self.assertEqual(quiet_hours_next(instant, 'America/New_York', '03:00', '02:30'), datetime(2027, 3, 15, 6, 30, tzinfo=UTC))

    def test_disabled(self):
        self.assertIs(quiet_hours_next(NOW, 'UTC', None, None), NOW)

    def test_invalid(self):
        for start, end in [('22:00', None), (None, '08:00'), ('22:00', '22:00'), ('24:00', '08:00'), ('9:00', '10:00'), ('22:00', '08:60')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                quiet_hours_next(NOW, 'UTC', start, end)
        with self.assertRaises(ValueError):
            quiet_hours_next(datetime(2026, 10, 31), 'UTC', None, None)

if __name__ == '__main__':
    unittest.main()
