"""Tests for IBKR-specific timestamp → ISO-8601 conversion."""

import unittest

from .timestamps import bridge_to_iso, flex_date_to_iso, flex_to_iso


class TestFlexToIso(unittest.TestCase):

    def test_basic(self) -> None:
        assert flex_to_iso("20250403;153000") == "2025-04-03T15:30:00"

    def test_midnight(self) -> None:
        assert flex_to_iso("20250403;000000") == "2025-04-03T00:00:00"

    def test_wrong_separator_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("20250403-153000")

    def test_dash_not_semicolon_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("20250403-15:30:00")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("")

    def test_iso_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("2025-04-03T15:30:00")

    def test_invalid_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("20251345;153000")  # month 13

    def test_invalid_time_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_to_iso("20250403;253000")  # hour 25


class TestFlexDateToIso(unittest.TestCase):

    def test_basic(self) -> None:
        assert flex_date_to_iso("20260508") == "2026-05-08"

    def test_january_first(self) -> None:
        assert flex_date_to_iso("20260101") == "2026-01-01"

    def test_too_short_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026508")

    def test_too_long_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("202605081")

    def test_iso_input_passed_through(self) -> None:
        # Defensive forwarding: if IBKR ever flips the wire format from
        # YYYYMMDD to ISO, the helper keeps working without a code change.
        assert flex_date_to_iso("2026-05-08") == "2026-05-08"

    def test_invalid_iso_month_raises(self) -> None:
        # Forwarding does not mean trusting — malformed ISO must still fail.
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026-13-08")

    def test_invalid_iso_day_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026-02-30")  # Feb 30

    def test_iso_with_time_component_raises(self) -> None:
        # Only bare dates are forwarded; full ISO timestamps are rejected to
        # keep the contract crisp.
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026-05-08T00:00:00")

    def test_iso_unpadded_components_raises(self) -> None:
        # "2026-5-8" is technically representable but not strict ISO 8601.
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026-5-8")

    def test_invalid_month_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("20261308")

    def test_invalid_day_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("20260230")  # Feb 30

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("")

    def test_non_digit_raises(self) -> None:
        with self.assertRaises(ValueError):
            flex_date_to_iso("2026MAY8 ")


class TestBridgeToIso(unittest.TestCase):

    def test_naive_iso(self) -> None:
        assert bridge_to_iso("2026-04-22T15:31:28") == "2026-04-22T15:31:28"

    def test_utc_aware_iso(self) -> None:
        assert bridge_to_iso("2026-04-22T15:31:28+00:00") == "2026-04-22T15:31:28+00:00"

    def test_non_utc_offset_passed_through(self) -> None:
        assert bridge_to_iso("2026-04-22T15:31:28+05:30") == "2026-04-22T15:31:28+05:30"

    def test_z_suffix_passed_through(self) -> None:
        assert bridge_to_iso("2026-04-22T15:31:28Z") == "2026-04-22T15:31:28Z"

    def test_iso_midnight(self) -> None:
        assert bridge_to_iso("2026-04-11T00:00:00") == "2026-04-11T00:00:00"

    def test_iso_invalid_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            bridge_to_iso("2026-13-01T10:00:00")  # month 13

    def test_legacy_basic(self) -> None:
        assert bridge_to_iso("20260411-10:30:00") == "2026-04-11T10:30:00"

    def test_legacy_midnight(self) -> None:
        assert bridge_to_iso("20260411-00:00:00") == "2026-04-11T00:00:00"

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            bridge_to_iso("")

    def test_flex_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            bridge_to_iso("20260411;103000")

    def test_legacy_invalid_hour_raises(self) -> None:
        with self.assertRaises(ValueError):
            bridge_to_iso("20260411-25:30:00")

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            bridge_to_iso("not-a-timestamp")
