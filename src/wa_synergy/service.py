"""Authenticated local service for Home Assistant and other consumers."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .client import SynergyClient
from .config import SynergyCredentials
from .errors import ConfigurationError, SynergyError
from .models import UsageInterval, UsageQuery
from .statistics import build_hourly_import_statistics
from .storage import get_usage_intervals
from .sync import sync_usage_to_db

_LOGGER = logging.getLogger(__name__)
_PERTH = ZoneInfo("Australia/Perth")
_REQUEST_DAYS = 31


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Validated service process settings."""

    credentials: SynergyCredentials = field(repr=False)
    api_token: str = field(repr=False)
    db_path: Path
    host: str = "0.0.0.0"
    port: int = 8099
    sync_hours: int = 6
    sync_retry_seconds: int = 300
    backfill_days: int = 730
    refresh_days: int = 30
    sync_on_start: bool = True

    def __post_init__(self) -> None:
        if len(self.api_token) < 32:
            raise ConfigurationError(
                "service API token must contain at least 32 characters"
            )
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("service port must be between 1 and 65535")
        for name in (
            "sync_hours",
            "sync_retry_seconds",
            "backfill_days",
            "refresh_days",
        ):
            if getattr(self, name) < 1:
                raise ConfigurationError(f"service {name} must be positive")

    @property
    def instance_id(self) -> str:
        """Return a stable non-secret identifier for the configured account."""

        normalized = self.credentials.email.casefold().encode()
        return hashlib.sha256(normalized).hexdigest()[:24]


class SynergyService:
    """Own synchronization state and expose JSON-ready snapshots."""

    def __init__(self, settings: ServiceSettings) -> None:
        self.settings = settings
        self._client = SynergyClient(credentials=settings.credentials)
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._syncing = False

    def close(self) -> None:
        """Close the provider client."""

        self._client.close()

    def sync(self) -> dict[str, int]:
        """Refresh either the initial backfill or the correction window."""

        if not self._sync_lock.acquire(blocking=False):
            raise ConfigurationError("a synchronization is already running")
        with self._state_lock:
            self._syncing = True
        try:
            existing = get_usage_intervals(self.settings.db_path)
            days = (
                self.settings.refresh_days if existing else self.settings.backfill_days
            )
            end = datetime.now(_PERTH).date()
            current = end - timedelta(days=days)
            refresh_cutoff = end - timedelta(days=self.settings.refresh_days)
            inserted = 0
            updated = 0
            unchanged = 0
            while current < end:
                chunk_end = min(current + timedelta(days=_REQUEST_DAYS), end)
                force_chunk = chunk_end > refresh_cutoff
                result = sync_usage_to_db(
                    client=self._client,
                    query=UsageQuery(
                        start=current,
                        end=chunk_end,
                        interval_type="DAILY",
                    ),
                    db_path=self.settings.db_path,
                    force=force_chunk,
                )
                inserted += result.inserted
                updated += result.updated
                unchanged += result.unchanged
                current = chunk_end
            with self._state_lock:
                self._last_success = datetime.now(UTC)
                self._last_error = None
            return {
                "inserted": inserted,
                "updated": updated,
                "unchanged": unchanged,
            }
        except SynergyError as exc:
            with self._state_lock:
                self._last_error = type(exc).__name__
            _LOGGER.exception("Synchronization failed: %s", exc)
            raise
        finally:
            with self._state_lock:
                self._syncing = False
            self._sync_lock.release()

    def _status(self, intervals: tuple[UsageInterval, ...]) -> dict[str, object]:
        data_through = max(
            (interval.interval_end for interval in intervals),
            default=None,
        )
        service_points = sorted({item.service_point_id for item in intervals})
        with self._state_lock:
            last_success = self._last_success
            last_error = self._last_error
            syncing = self._syncing
        return {
            "instance_id": self.settings.instance_id,
            "ready": last_success is not None or bool(intervals),
            "syncing": syncing,
            "last_success": last_success.isoformat() if last_success else None,
            "last_error": last_error,
            "data_through": data_through.isoformat() if data_through else None,
            "service_points": service_points,
        }

    def status(self) -> dict[str, object]:
        """Return safe service and stored-data status."""

        return self._status(get_usage_intervals(self.settings.db_path))

    def statistics(self, since: datetime | None) -> dict[str, object]:
        """Return cumulative grid-import statistics and safe status."""

        intervals = get_usage_intervals(self.settings.db_path)
        statistics = build_hourly_import_statistics(intervals, since=since)
        return {
            "status": self._status(intervals),
            "statistics": [
                {
                    "service_point_id": statistic.service_point_id,
                    "stream": "grid_import",
                    "start": statistic.start.isoformat(),
                    "sum_kwh": str(statistic.sum_kwh),
                }
                for statistic in statistics
            ],
        }


class _Server(ThreadingHTTPServer):
    service: SynergyService
    api_token: str


class _RequestHandler(BaseHTTPRequestHandler):
    server: _Server

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        _LOGGER.info(
            "%s %s -> %s (%s)",
            self.command,
            self.path,
            code,
            self.address_string(),
        )

    def log_message(self, format: str, *args: object) -> None:
        _LOGGER.warning("%s (%s)", format % args, self.address_string())

    def _send(self, status: HTTPStatus, value: object) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.api_token}"
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, expected)

    def _require_authorization(self) -> bool:
        if self._authorized():
            return True
        self._send(HTTPStatus.UNAUTHORIZED, {"error": "invalid_api_token"})
        return False

    def do_GET(self) -> None:
        if not self._require_authorization():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/status":
            self._send(HTTPStatus.OK, self.server.service.status())
            return
        if parsed.path == "/v1/statistics":
            try:
                since = _parse_since(parse_qs(parsed.query).get("since", []))
                payload = self.server.service.statistics(since)
            except (ConfigurationError, ValueError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, payload)
            return
        self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._require_authorization():
            return
        if urlsplit(self.path).path != "/v1/sync":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            result = self.server.service.sync()
        except ConfigurationError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except SynergyError as exc:
            self._send(
                HTTPStatus.BAD_GATEWAY,
                {"error": "provider_sync_failed", "type": type(exc).__name__},
            )
            return
        self._send(HTTPStatus.OK, result)


def _parse_since(values: list[str]) -> datetime | None:
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("since must be supplied once")
    value = datetime.fromisoformat(values[0].replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("since must include a timezone")
    return value.astimezone(UTC)


def _option(
    data: dict[str, Any], key: str, env_name: str, default: object = None
) -> Any:
    if key in data:
        return data[key]
    return os.environ.get(env_name, default)


def _load_settings(options_path: Path | None) -> ServiceSettings:
    data: dict[str, Any] = {}
    if options_path is not None:
        try:
            loaded = json.loads(options_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read service options: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError("service options must be a JSON object")
        data = loaded

    sync_on_start_value = _option(
        data, "sync_on_start", "WA_SYNERGY_SYNC_ON_START", True
    )
    if isinstance(sync_on_start_value, str):
        sync_on_start = sync_on_start_value.casefold() not in {"0", "false", "no"}
    else:
        sync_on_start = bool(sync_on_start_value)

    return ServiceSettings(
        credentials=SynergyCredentials(
            email=str(_option(data, "email", "WA_SYNERGY_EMAIL", "")),
            password=str(_option(data, "password", "WA_SYNERGY_PASSWORD", "")),
            gmail_app_password=str(
                _option(
                    data,
                    "gmail_app_password",
                    "WA_SYNERGY_GMAIL_APP_PASSWORD",
                    "",
                )
            ),
        ),
        api_token=str(_option(data, "api_token", "WA_SYNERGY_API_TOKEN", "")),
        db_path=Path(
            str(_option(data, "db_path", "WA_SYNERGY_DB_PATH", "/data/synergy.sqlite3"))
        ),
        host=str(_option(data, "host", "WA_SYNERGY_HOST", "0.0.0.0")),
        port=int(_option(data, "port", "WA_SYNERGY_PORT", 8099)),
        sync_hours=int(_option(data, "sync_hours", "WA_SYNERGY_SYNC_HOURS", 6)),
        sync_retry_seconds=int(
            _option(
                data,
                "sync_retry_seconds",
                "WA_SYNERGY_SYNC_RETRY_SECONDS",
                300,
            )
        ),
        backfill_days=int(
            _option(data, "backfill_days", "WA_SYNERGY_BACKFILL_DAYS", 730)
        ),
        refresh_days=int(_option(data, "refresh_days", "WA_SYNERGY_REFRESH_DAYS", 30)),
        sync_on_start=sync_on_start,
    )


def _sync_loop(service: SynergyService, stop: threading.Event) -> None:
    regular_delay = service.settings.sync_hours * 60 * 60
    delay = 0 if service.settings.sync_on_start else regular_delay
    while not stop.wait(delay):
        try:
            service.sync()
        except SynergyError:
            delay = service.settings.sync_retry_seconds
        else:
            delay = regular_delay


def main() -> None:
    """Run the companion HTTP service."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--options", type=Path)
    arguments = parser.parse_args()
    settings = _load_settings(arguments.options)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    service = SynergyService(settings)
    server = _Server((settings.host, settings.port), _RequestHandler)
    server.service = service
    server.api_token = settings.api_token
    stop = threading.Event()
    scheduler = threading.Thread(
        target=_sync_loop,
        args=(service, stop),
        name="synergy-sync",
        daemon=True,
    )
    scheduler.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
