from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from wa_synergy.errors import ConfigurationError, StorageError
from wa_synergy.models import UsageInterval
from wa_synergy.storage import SCHEMA_VERSION, upsert_usage_intervals


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.db_path = Path(self.temporary_directory.name) / "nested" / "usage.sqlite3"
        self.fetched_at = datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC)

    def make_interval(self, **changes: object) -> UsageInterval:
        values = {
            "account_id": "000123",
            "service_point_id": "a0123456789ABCDEF0",
            "meter_id": None,
            "channel": "PEAK",
            "interval_start": datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
            "interval_end": datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
            "consumption_kwh": Decimal("1.230"),
            "quality": "Not yet billed",
            "source_updated_at": None,
        }
        values.update(changes)
        return UsageInterval(**values)  # type: ignore[arg-type]

    def open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        self.addCleanup(connection.close)
        return connection

    def test_creates_secure_versioned_database_reopenable_by_second_connection(
        self,
    ) -> None:
        result = upsert_usage_intervals(
            self.db_path,
            [self.make_interval()],
            fetched_at_utc=self.fetched_at,
        )

        self.assertEqual((result.inserted, result.updated, result.unchanged), (1, 0, 0))
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)

        connection = self.open_database()
        version = connection.execute("PRAGMA user_version").fetchone()
        row = connection.execute(
            """
            SELECT account_id, service_point_id, meter_id, channel,
                   interval_start_utc, interval_end_utc, consumption_kwh,
                   quality, source_updated_at_utc, fetched_at_utc
            FROM usage_intervals
            """
        ).fetchone()

        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(
            row,
            (
                "000123",
                "a0123456789ABCDEF0",
                None,
                "PEAK",
                "2026-07-01T00:00:00.000000Z",
                "2026-07-01T00:30:00.000000Z",
                "1.23",
                "Not yet billed",
                None,
                "2026-08-08T01:02:03.000000Z",
            ),
        )

    def test_identical_resync_is_unchanged_and_refreshes_fetch_time(self) -> None:
        interval = self.make_interval()
        upsert_usage_intervals(
            self.db_path, [interval], fetched_at_utc=self.fetched_at
        )
        later = self.fetched_at + timedelta(hours=1)

        result = upsert_usage_intervals(
            self.db_path,
            [interval, interval],
            fetched_at_utc=later,
        )

        self.assertEqual((result.inserted, result.updated, result.unchanged), (0, 0, 1))
        connection = self.open_database()
        row = connection.execute(
            "SELECT COUNT(*), fetched_at_utc FROM usage_intervals"
        ).fetchone()
        self.assertEqual(row, (1, "2026-08-08T02:02:03.000000Z"))

    def test_revised_provider_values_update_existing_identity(self) -> None:
        original = self.make_interval()
        upsert_usage_intervals(
            self.db_path, [original], fetched_at_utc=self.fetched_at
        )
        revised = replace(
            original,
            consumption_kwh=Decimal("2.500"),
            quality="Billed",
            source_updated_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        )

        result = upsert_usage_intervals(
            self.db_path,
            [revised],
            fetched_at_utc=self.fetched_at + timedelta(hours=1),
        )

        self.assertEqual((result.inserted, result.updated, result.unchanged), (0, 1, 0))
        connection = self.open_database()
        row = connection.execute(
            """
            SELECT consumption_kwh, quality, source_updated_at_utc, fetched_at_utc
            FROM usage_intervals
            """
        ).fetchone()
        self.assertEqual(
            row,
            (
                "2.5",
                "Billed",
                "2026-08-07T12:00:00.000000Z",
                "2026-08-08T02:02:03.000000Z",
            ),
        )

    def test_accounts_services_meters_and_null_meters_do_not_collide(self) -> None:
        base = self.make_interval()
        records = [
            base,
            replace(base, account_id="000124"),
            replace(base, service_point_id="a0123456789ABCDEF1"),
            replace(base, meter_id="METER-1"),
        ]

        result = upsert_usage_intervals(
            self.db_path, records, fetched_at_utc=self.fetched_at
        )

        self.assertEqual((result.inserted, result.updated, result.unchanged), (4, 0, 0))
        connection = self.open_database()
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM usage_intervals").fetchone(),
            (4,),
        )

    def test_conflicting_duplicate_rejects_batch_without_partial_write(self) -> None:
        existing = self.make_interval(channel="OFF_PEAK")
        upsert_usage_intervals(
            self.db_path, [existing], fetched_at_utc=self.fetched_at
        )
        first = self.make_interval(consumption_kwh=Decimal("1"))
        conflicting = replace(first, consumption_kwh=Decimal("9"))

        with self.assertRaisesRegex(StorageError, "conflicting duplicate"):
            upsert_usage_intervals(
                self.db_path,
                [first, conflicting],
                fetched_at_utc=self.fetched_at,
            )

        connection = self.open_database()
        self.assertEqual(
            connection.execute(
                "SELECT channel, consumption_kwh FROM usage_intervals"
            ).fetchall(),
            [("OFF_PEAK", "1.23")],
        )

    def test_sql_failure_rolls_back_the_complete_batch(self) -> None:
        existing = self.make_interval(channel="OFF_PEAK")
        upsert_usage_intervals(
            self.db_path, [existing], fetched_at_utc=self.fetched_at
        )
        setup_connection = sqlite3.connect(self.db_path)
        setup_connection.execute(
            """
            CREATE TRIGGER reject_peak
            BEFORE INSERT ON usage_intervals
            WHEN NEW.channel = 'PEAK'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic write failure');
            END
            """
        )
        setup_connection.commit()
        setup_connection.close()
        records = [
            self.make_interval(channel="SUPER_OFFPEAK"),
            self.make_interval(
                channel="PEAK",
                interval_start=datetime(2026, 7, 1, 0, 30, tzinfo=UTC),
                interval_end=datetime(2026, 7, 1, 1, 0, tzinfo=UTC),
            ),
        ]

        with self.assertRaises(StorageError):
            upsert_usage_intervals(
                self.db_path, records, fetched_at_utc=self.fetched_at
            )

        connection = self.open_database()
        self.assertEqual(
            connection.execute(
                "SELECT channel, consumption_kwh FROM usage_intervals"
            ).fetchall(),
            [("OFF_PEAK", "1.23")],
        )

    def test_schema_contains_only_normalized_columns(self) -> None:
        upsert_usage_intervals(self.db_path, [], fetched_at_utc=self.fetched_at)

        connection = self.open_database()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(usage_intervals)")
        }

        self.assertEqual(
            columns,
            {
                "account_id",
                "service_point_id",
                "meter_id",
                "channel",
                "interval_start_utc",
                "interval_end_utc",
                "consumption_kwh",
                "quality",
                "source_updated_at_utc",
                "fetched_at_utc",
            },
        )

    def test_invalid_paths_and_fetch_times_fail_before_database_creation(self) -> None:
        cases = (
            (Path(":memory:"), self.fetched_at),
            (Path(self.temporary_directory.name), self.fetched_at),
            (self.db_path, datetime(2026, 8, 8, 1, 2, 3)),
        )
        for path, fetched_at in cases:
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                upsert_usage_intervals(path, [], fetched_at_utc=fetched_at)
        self.assertFalse(self.db_path.exists())

    def test_corrupt_database_error_is_safe(self) -> None:
        self.db_path.parent.mkdir()
        self.db_path.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(
            StorageError, "normalized usage could not be stored in SQLite"
        ) as raised:
            upsert_usage_intervals(
                self.db_path,
                [self.make_interval()],
                fetched_at_utc=self.fetched_at,
            )

        self.assertNotIn("not a sqlite database", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
