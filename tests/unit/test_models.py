from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from wa_synergy.errors import (
    AuthenticationContractError,
    AuthenticationError,
    ConfigurationError,
    OtpError,
    OtpTimeoutError,
    PortalContractError,
    SynergyError,
    UsageValidationError,
)
from wa_synergy.models import SyncResult, UsageInterval, UsageQuery


class UsageQueryTests(unittest.TestCase):
    def test_range_is_exclusive_and_iso_strings_become_dates(self) -> None:
        query = UsageQuery(
            start="2026-07-01",
            end="2026-08-01",
            account_ids=["00123"],  # type: ignore[arg-type]
            service_point_ids=["0000456"],  # type: ignore[arg-type]
        )

        self.assertEqual(query.start, date(2026, 7, 1))
        self.assertEqual(query.end, date(2026, 8, 1))
        self.assertEqual(query.account_ids, ("00123",))
        self.assertEqual(query.service_point_ids, ("0000456",))
        self.assertEqual((query.end - query.start).days, 31)

    def test_empty_or_reversed_ranges_are_rejected(self) -> None:
        for start, end in (
            ("2026-07-01", "2026-07-01"),
            ("2026-07-02", "2026-07-01"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ConfigurationError):
                    UsageQuery(start=start, end=end)

    def test_only_exact_iso_calendar_dates_are_accepted(self) -> None:
        for value in ("20260701", "2026-7-1", datetime(2026, 7, 1)):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    UsageQuery(start=value, end="2026-08-01")  # type: ignore[arg-type]

    def test_filters_reject_ambiguous_or_duplicate_identifiers(self) -> None:
        for account_ids in ("123", ("123", "123"), (" 123",), ("",)):
            with self.subTest(account_ids=account_ids):
                with self.assertRaises(ConfigurationError):
                    UsageQuery(
                        start="2026-07-01",
                        end="2026-08-01",
                        account_ids=account_ids,  # type: ignore[arg-type]
                    )

    def test_query_is_immutable_and_slotted(self) -> None:
        query = UsageQuery(start="2026-07-01", end="2026-08-01")

        with self.assertRaises(FrozenInstanceError):
            query.start = date(2026, 6, 1)  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            query.unexpected = True  # type: ignore[attr-defined]


class UsageIntervalTests(unittest.TestCase):
    def make_interval(self, **changes: object) -> UsageInterval:
        values = {
            "account_id": "000123",
            "service_point_id": "a0123456789ABCDEF0",
            "meter_id": None,
            "channel": "PEAK",
            "interval_start": datetime(
                2026, 7, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))
            ),
            "interval_end": datetime(
                2026, 7, 1, 8, 30, tzinfo=timezone(timedelta(hours=8))
            ),
            "consumption_kwh": Decimal("1.230"),
            "quality": "Not yet billed",
        }
        values.update(changes)
        return UsageInterval(**values)  # type: ignore[arg-type]

    def test_normalizes_boundaries_to_utc_without_changing_decimal(self) -> None:
        interval = self.make_interval()

        self.assertEqual(
            interval.interval_start,
            datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            interval.interval_end,
            datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(interval.consumption_kwh.as_tuple().exponent, -3)
        self.assertEqual(interval.UNIT, "kWh")

    def test_identity_includes_account_service_meter_channel_and_bounds(self) -> None:
        interval = self.make_interval(meter_id="000METER", channel="VAL_SOLAR")

        self.assertEqual(
            interval.record_identity,
            (
                "000123",
                "a0123456789ABCDEF0",
                "000METER",
                "VAL_SOLAR",
                interval.interval_start,
                interval.interval_end,
            ),
        )

    def test_meter_attribution_may_be_absent(self) -> None:
        self.assertIsNone(self.make_interval().meter_id)

    def test_invalid_boundaries_are_rejected(self) -> None:
        for end in (
            datetime(2026, 7, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 1, 8, 30),
        ):
            with self.subTest(end=end):
                with self.assertRaises(UsageValidationError):
                    self.make_interval(interval_end=end)

    def test_quantity_requires_a_finite_decimal(self) -> None:
        for quantity in (1.23, Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(quantity=quantity):
                with self.assertRaises(UsageValidationError):
                    self.make_interval(consumption_kwh=quantity)

    def test_interval_is_immutable_and_slotted(self) -> None:
        interval = self.make_interval()

        with self.assertRaises(FrozenInstanceError):
            interval.channel = "OFF_PEAK"  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            interval.unexpected = True  # type: ignore[attr-defined]


class SyncResultTests(unittest.TestCase):
    def test_total_is_sum_of_persistence_outcomes(self) -> None:
        result = SyncResult(inserted=3, updated=2, unchanged=4)

        self.assertEqual(result.total, 9)

    def test_counts_are_non_negative_integers(self) -> None:
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SyncResult(inserted=value, updated=0, unchanged=0)  # type: ignore[arg-type]


class ExceptionHierarchyTests(unittest.TestCase):
    def test_contract_and_otp_errors_are_catchable_by_public_bases(self) -> None:
        self.assertTrue(issubclass(AuthenticationContractError, AuthenticationError))
        self.assertTrue(issubclass(AuthenticationContractError, PortalContractError))
        self.assertTrue(issubclass(OtpTimeoutError, OtpError))
        self.assertTrue(issubclass(OtpTimeoutError, TimeoutError))
        self.assertTrue(issubclass(ConfigurationError, SynergyError))


if __name__ == "__main__":
    unittest.main()
