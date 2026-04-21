"""Tests for the shared timestamp normalization helper."""

import unittest
from zoneinfo import ZoneInfo

from .time_format import normalize_timestamp, parse_timezone, to_epoch


class TestNormalizeTimestamp(unittest.TestCase):

    # ── Canonical form (idempotent) ──

    def test_canonical_form_idempotent(self) -> None:
        assert normalize_timestamp("2026-04-19T15:30:00") == "2026-04-19T15:30:00"

    # ── ISO-8601 inputs ──

    def test_iso_with_z_suffix(self) -> None:
        assert normalize_timestamp("2026-04-19T15:30:00Z") == "2026-04-19T15:30:00"

    def test_iso_with_plus_offset(self) -> None:
        # +02:00 → UTC = 13:30
        assert normalize_timestamp("2026-04-19T15:30:00+02:00") == "2026-04-19T13:30:00"

    def test_iso_with_minus_offset(self) -> None:
        # -05:00 → UTC = 20:30
        assert normalize_timestamp("2026-04-19T15:30:00-05:00") == "2026-04-19T20:30:00"

    def test_iso_with_fractional_seconds(self) -> None:
        assert normalize_timestamp("2026-04-19T15:30:00.123456Z") == "2026-04-19T15:30:00"

    def test_iso_date_boundary_crossing(self) -> None:
        # 23:30 EST + 5h = 04:30 UTC next day
        assert normalize_timestamp("2026-04-19T23:30:00-05:00") == "2026-04-20T04:30:00"

    # ── Naive input + assume_tz ──

    def test_naive_iso_assumes_utc_by_default(self) -> None:
        assert normalize_timestamp("2026-04-19T15:30:00") == "2026-04-19T15:30:00"

    def test_naive_iso_with_assume_tz(self) -> None:
        ny = ZoneInfo("America/New_York")
        # 15:30 NY in April (EDT, -04:00) → UTC 19:30
        assert normalize_timestamp(
            "2026-04-19T15:30:00", assume_tz=ny,
        ) == "2026-04-19T19:30:00"

    def test_tz_aware_input_ignores_assume_tz(self) -> None:
        # Input carries +02:00; assume_tz=America/New_York should be ignored.
        ny = ZoneInfo("America/New_York")
        assert normalize_timestamp(
            "2026-04-19T15:30:00+02:00", assume_tz=ny,
        ) == "2026-04-19T13:30:00"

    # ── Failure cases ──

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_timestamp("")

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_timestamp("not a timestamp at all")

    def test_letters_in_date_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_timestamp("YYYY-MM-DDTHH:MM:SS")

    def test_slashes_raise(self) -> None:
        with self.assertRaises(ValueError):
            normalize_timestamp("2026/04/11 10:30:00")


class TestToEpoch(unittest.TestCase):

    def test_canonical_form_utc(self) -> None:
        # 2025-04-03T12:00:00 UTC == 1743681600
        assert to_epoch("2025-04-03T12:00:00") == 1743681600

    def test_empty_returns_zero(self) -> None:
        assert to_epoch("") == 0

    def test_monotonic(self) -> None:
        assert to_epoch("2025-04-03T12:00:00") < to_epoch("2025-04-03T12:00:01")

    def test_unparseable_raises(self) -> None:
        with self.assertRaises(ValueError):
            to_epoch("not a timestamp")

    # ── Strictness: only the exact canonical form is accepted. ──
    # Every Fill.timestamp passes through normalize_timestamp() upstream,
    # so anything else reaching to_epoch() is a contract violation and
    # should fail loudly rather than be silently accepted.

    def test_tz_aware_z_suffix_rejected(self) -> None:
        with self.assertRaises(ValueError) as cm:
            to_epoch("2025-04-03T12:00:00Z")
        assert "canonical" in str(cm.exception)

    def test_tz_aware_offset_rejected(self) -> None:
        with self.assertRaises(ValueError):
            to_epoch("2025-04-03T12:00:00+02:00")

    def test_fractional_seconds_rejected(self) -> None:
        with self.assertRaises(ValueError):
            to_epoch("2025-04-03T12:00:00.123456")

    def test_ibkr_flex_form_rejected(self) -> None:
        with self.assertRaises(ValueError):
            to_epoch("20250403;120000")

    def test_date_only_rejected(self) -> None:
        with self.assertRaises(ValueError):
            to_epoch("2025-04-03")


class TestParseTimezone(unittest.TestCase):

    def test_valid_utc(self) -> None:
        tz = parse_timezone("UTC")
        assert tz.key == "UTC"

    def test_valid_regional(self) -> None:
        tz = parse_timezone("America/New_York")
        assert tz.key == "America/New_York"

    def test_invalid_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_timezone("Not/AValid_Zone")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_timezone("")
