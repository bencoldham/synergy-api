import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wa_synergy.errors import UsageValidationError
from wa_synergy.models import UsageQuery
from wa_synergy.usage import (
    _decode_usage_response,
    merge_usage_intervals,
    normalize_usage_response,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "usage" / "valid.json"
_ACCOUNT_ID = "0000000001"
_SERVICE_POINT_ID = "a00000000000000001"


def _fixture_envelope() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _nested_document(envelope: dict[str, Any]) -> dict[str, Any]:
    encoded = envelope["actions"][0]["returnValue"]["returnValue"]
    return json.loads(encoded)


def _encode_envelope(envelope: dict[str, Any], nested: dict[str, Any]) -> str:
    envelope["actions"][0]["returnValue"]["returnValue"] = json.dumps(nested)
    return json.dumps(envelope)


class UsageNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = UsageQuery(start="2026-07-01", end="2026-07-02")
        self.response_text = _FIXTURE.read_text(encoding="utf-8")

    def normalize(self, response_text: str | None = None, **changes: Any):
        arguments = {
            "response_text": self.response_text
            if response_text is None
            else response_text,
            "account_id": _ACCOUNT_ID,
            "service_point_id": _SERVICE_POINT_ID,
            "query": self.query,
        }
        arguments.update(changes)
        return normalize_usage_response(**arguments)

    def mutate_nested(self, mutation: Any) -> str:
        envelope = _fixture_envelope()
        nested = _nested_document(envelope)
        mutation(nested)
        return _encode_envelope(envelope, nested)

    def test_valid_response_normalizes_every_import_and_solar_quantity(self) -> None:
        intervals = self.normalize()

        self.assertEqual(len(intervals), 4)
        self.assertEqual(
            [(record.interval_start, record.channel) for record in intervals],
            [
                (datetime(2026, 6, 30, 16, 0, tzinfo=UTC), "PEAK"),
                (datetime(2026, 6, 30, 16, 0, tzinfo=UTC), "VAL_SOLAR"),
                (datetime(2026, 7, 1, 15, 30, tzinfo=UTC), "OFF_PEAK"),
                (datetime(2026, 7, 1, 15, 30, tzinfo=UTC), "VAL_SOLAR"),
            ],
        )
        self.assertEqual(
            [record.consumption_kwh for record in intervals],
            [Decimal("1.230"), Decimal("0.100"), Decimal("0.045"), Decimal("0.000")],
        )
        self.assertTrue(all(record.meter_id is None for record in intervals))
        self.assertTrue(all(record.UNIT == "kWh" for record in intervals))
        self.assertTrue(all(record.quality == "Not yet billed" for record in intervals))
        self.assertTrue(all(record.source_updated_at is None for record in intervals))

    def test_perth_day_boundaries_are_exposed_as_utc_intervals(self) -> None:
        intervals = self.normalize()

        self.assertEqual(
            intervals[0].interval_start,
            datetime(2026, 6, 30, 16, 0, tzinfo=UTC),
        )
        self.assertEqual(
            intervals[-1].interval_end,
            datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
        )

    def test_json_numbers_are_decoded_directly_as_decimal(self) -> None:
        decoded = _decode_usage_response(self.response_text)

        self.assertIsInstance(decoded["Total_Cost"], Decimal)
        self.assertEqual(decoded["Total_Cost"], Decimal("1.375"))

    def test_identifiers_remain_strings_with_leading_zeroes(self) -> None:
        interval = self.normalize()[0]

        self.assertEqual(interval.account_id, "0000000001")
        self.assertEqual(interval.service_point_id, "a00000000000000001")

    def test_additive_fields_at_every_observed_layer_are_ignored(self) -> None:
        self.assertEqual(len(self.normalize()), 4)

    def test_multiple_accounts_and_services_sort_deterministically(self) -> None:
        later = self.normalize(
            account_id="0000000002",
            service_point_id="b00000000000000001",
        )
        earlier = self.normalize(
            account_id="0000000001",
            service_point_id="a00000000000000001",
        )

        merged = merge_usage_intervals((*later, *reversed(earlier)))

        self.assertEqual(len(merged), 8)
        self.assertEqual(merged[0].account_id, "0000000001")
        self.assertEqual(merged[-1].account_id, "0000000002")
        self.assertEqual(
            merged,
            tuple(
                sorted(
                    merged,
                    key=lambda row: (
                        row.account_id,
                        row.service_point_id,
                        row.interval_start,
                        row.channel,
                    ),
                )
            ),
        )

    def test_exact_duplicate_rows_and_records_collapse(self) -> None:
        def duplicate_first_row(nested: dict[str, Any]) -> None:
            rows = nested["IPResult"]["ChartData"]["Response"]
            rows.append(deepcopy(rows[0]))

        intervals = self.normalize(self.mutate_nested(duplicate_first_row))

        self.assertEqual(len(intervals), 4)
        self.assertEqual(merge_usage_intervals((*intervals, *intervals)), intervals)

    def test_conflicting_duplicate_rows_are_rejected(self) -> None:
        def conflict(nested: dict[str, Any]) -> None:
            rows = nested["IPResult"]["ChartData"]["Response"]
            duplicate = deepcopy(rows[1])
            duplicate["PEAK"] = "9.999"
            rows.append(duplicate)

        with self.assertRaises(UsageValidationError):
            self.normalize(self.mutate_nested(conflict))

    def test_conflicting_normalized_records_are_rejected(self) -> None:
        interval = self.normalize()[0]
        conflicting = replace(interval, consumption_kwh=Decimal("9.999"))

        with self.assertRaises(UsageValidationError):
            merge_usage_intervals((interval, conflicting))

    def test_missing_required_fields_and_wrong_types_are_rejected(self) -> None:
        def missing_status(nested: dict[str, Any]) -> None:
            del nested["IPResult"]["ChartData"]["Response"][0]["BillingStatus"]

        def wrong_response_type(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"] = {}

        def numeric_quantity(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][1]["PEAK"] = 1.23

        for mutation in (missing_status, wrong_response_type, numeric_quantity):
            with (
                self.subTest(mutation=mutation.__name__),
                self.assertRaises(UsageValidationError),
            ):
                self.normalize(self.mutate_nested(mutation))

    def test_unknown_unit_and_unknown_quantity_channel_are_rejected(self) -> None:
        with self.assertRaises(UsageValidationError):
            self.normalize(provider_unit="WH")

        def future_tariff(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][0]["FUTURE_TARIFF"] = "0.500"

        with self.assertRaises(UsageValidationError):
            self.normalize(self.mutate_nested(future_tariff))

    def test_invalid_dates_times_and_out_of_range_rows_are_rejected(self) -> None:
        def invalid_date(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][0]["VAL_DAY"] = "2026-02-30"

        def invalid_time(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][0]["VAL_TIME"] = "2360"

        def outside_range(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][0]["VAL_DAY"] = "2026-07-02"

        for mutation in (invalid_date, invalid_time, outside_range):
            with (
                self.subTest(mutation=mutation.__name__),
                self.assertRaises(UsageValidationError),
            ):
                self.normalize(self.mutate_nested(mutation))

    def test_multiple_import_channels_are_rejected(self) -> None:
        def second_tariff(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["Response"][0]["PEAK"] = "0.100"

        with self.assertRaises(UsageValidationError):
            self.normalize(self.mutate_nested(second_tariff))

    def test_malformed_envelopes_nested_json_and_unsuccessful_chart_fail_safely(
        self,
    ) -> None:
        envelope = _fixture_envelope()
        malformed_nested = deepcopy(envelope)
        malformed_nested["actions"][0]["returnValue"]["returnValue"] = "not-json"

        def unsuccessful_chart(nested: dict[str, Any]) -> None:
            nested["IPResult"]["ChartData"]["errorCode"] = "INVOKE-500"

        candidates = (
            "not-json",
            json.dumps({"actions": []}),
            json.dumps(malformed_nested),
            self.mutate_nested(unsuccessful_chart),
        )
        for candidate in candidates:
            with (
                self.subTest(candidate=candidate[:20]),
                self.assertRaises(UsageValidationError) as raised,
            ):
                self.normalize(candidate)
            self.assertNotIn(candidate, str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_filter_context_must_match_the_query(self) -> None:
        account_query = UsageQuery(
            start="2026-07-01",
            end="2026-07-02",
            account_ids=("0000000099",),
        )

        with self.assertRaises(UsageValidationError):
            self.normalize(query=account_query)


if __name__ == "__main__":
    unittest.main()
