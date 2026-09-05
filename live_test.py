"""Exercise the real Synergy account through the app and Home Assistant."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from websockets.sync.client import connect

_COMPOSE_BIN = (
    "docker" if shutil.which(
        "docker") else "podman" if shutil.which("podman") else None
)
_COMPOSE = (
    ("docker", "compose", "-f", "compose.ha-test.yaml")
    if shutil.which("docker")
    else ("podman-compose", "-f", "compose.ha-test.yaml")
)
_APP_URL = "http://127.0.0.1:18099"
_HA_URL = "http://127.0.0.1:18123"
_APP_TOKEN = "ha-live-test-token-0123456789abcdef0123456789"
_HA_USERNAME = "wa-synergy-live-test"
_HA_PASSWORD = "wa-synergy-live-test-password"

_SYNC_FAILURE = "Synchronization failed:"
_SYNC_THREAD_CRASH = "Exception in thread synergy-sync"

_TARIFF_SENSOR_KEYS = (
    "electricity_price",
    "daily_supply_charge",
    "k1_first_20_price",
    "k1_20_to_1650_price",
    "k1_above_1650_price",
)
# Independently transcribed July 2026 published prices, in AUD (not cents).
# Array index is the Perth hour; do not derive these from integration code.
_EXPECTED_TARIFFS = {
    "home_a1": {
        "supply": "1.192419",
        "prices": ["0.332621"] * 24,
        "pricing_type": "flat",
    },
    "midday_saver": {
        "supply": "1.327806",
        "prices": (
            ["0.243431"] * 9 + ["0.088520"] * 6 +
            ["0.553253"] * 6 + ["0.243431"] * 3
        ),
        "pricing_type": "time_of_use",
    },
    "electric_vehicle": {
        "supply": "1.327806",
        "prices": (
            ["0.199172"] * 6
            + ["0.243431"] * 3
            + ["0.088520"] * 6
            + ["0.553253"] * 6
            + ["0.243431"] * 2
            + ["0.199172"]
        ),
        "pricing_type": "time_of_use",
    },
    "home_business_k1": {
        "supply": "2.104115",
        "prices": [None] * 24,
        "pricing_type": "tiered",
    },
    "not_set": {
        "supply": None,
        "prices": [None] * 24,
        "pricing_type": "not_set",
    },
}


def _can_compose() -> bool:
    if not _COMPOSE_BIN:
        return False
    try:
        probe = subprocess.run(
            [_COMPOSE_BIN, "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _compose(*arguments: str, check: bool = True) -> None:
    subprocess.run(
        (*_COMPOSE, *arguments),
        check=check,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _compose_output(*arguments: str) -> str:
    result = subprocess.run(
        (*_COMPOSE, *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def _compose_logs() -> str:
    return _compose_output("logs", "--timestamps", "synergy-app")


def _runtime(*arguments: str) -> str:
    if _COMPOSE_BIN is None:
        raise RuntimeError("container runtime is unavailable")
    result = subprocess.run(
        (_COMPOSE_BIN, *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _wait_for(
    description: str,
    probe: Callable[[], Any | None],
    *,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if result := probe():
                return result
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 404):
                raise
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {description}")


def _app_status() -> dict[str, Any] | None:
    response = httpx.get(
        f"{_APP_URL}/v1/status",
        headers={"Authorization": f"Bearer {_APP_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()
    status = response.json()
    if (
        status["ready"]
        and not status["syncing"]
        and status["last_success"]
        and status["service_points"]
    ):
        return status
    return None


def _recorded_sync_failure(previous_count: int) -> str | None:
    logs = _compose_logs()
    return logs if logs.count(_SYNC_FAILURE) > previous_count else None


def _exercise_live_network_failure() -> None:
    existing_logs = _compose_logs()
    if _SYNC_THREAD_CRASH in existing_logs:
        raise RuntimeError(
            "companion sync thread crashed before network fault injection"
        )

    container_id = _runtime(
        "ps",
        "--filter",
        "label=com.docker.compose.service=synergy-app",
        "--format",
        "{{.ID}}",
    )
    if not container_id:
        raise RuntimeError(
            "cannot identify the running companion service container")
    inspected = json.loads(_runtime("inspect", container_id))
    labels = inspected[0]["Config"]["Labels"]
    expected_labels = {
        "io.hass.arch": "amd64",
        "io.hass.type": "app",
        "io.hass.version": "0.1.11",
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError(
            f"companion image has invalid Home Assistant labels: {labels}"
        )
    networks = tuple(inspected[0]["NetworkSettings"]["Networks"])
    if len(networks) != 1:
        raise RuntimeError(
            f"expected one companion service network, found {len(networks)}"
        )
    network = networks[0]

    print("Disconnecting the live companion container from the network...")
    _runtime("network", "disconnect", network, container_id)
    try:
        _runtime("restart", container_id)
        failure_logs = _wait_for(
            "typed synchronization failure after a real network outage",
            lambda: _recorded_sync_failure(existing_logs.count(_SYNC_FAILURE)),
            timeout=90,
        )
        if _SYNC_THREAD_CRASH in failure_logs:
            raise RuntimeError(
                "synergy-sync thread crashed during network outage")
        if "| ERROR    | Traceback (most recent call last):" not in failure_logs:
            raise RuntimeError(
                "companion traceback lines have no application timestamp"
            )
    finally:
        _runtime("stop", container_id)
        _runtime("network", "connect", network, container_id)
        _runtime("start", container_id)

    status = _wait_for(
        "scheduled live synchronization after network restoration",
        _app_status,
        timeout=900,
    )
    recovered_logs = _compose_logs()
    if _SYNC_THREAD_CRASH in recovered_logs:
        raise RuntimeError(
            "synergy-sync thread crashed instead of surviving the outage"
        )
    print(
        "Live outage recovery complete:",
        f"data through {status['data_through']}",
    )


def _ha_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.request(
        method,
        f"{_HA_URL}{path}",
        headers=headers,
        json=json_body,
        data=form,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _onboard_home_assistant() -> str:
    client_id = f"{_HA_URL}/"
    user = _ha_request(
        "POST",
        "/api/onboarding/users",
        json_body={
            "client_id": client_id,
            "language": "en",
            "name": "WA Synergy Live Test",
            "username": _HA_USERNAME,
            "password": _HA_PASSWORD,
        },
    )
    token = _ha_request(
        "POST",
        "/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": user["auth_code"],
            "client_id": client_id,
        },
    )["access_token"]
    _ha_request(
        "POST",
        "/api/onboarding/core_config",
        token=token,
        json_body={
            "latitude": -31.9523,
            "longitude": 115.8613,
            "elevation": 0,
            "location_name": "WA Synergy Live Test",
            "time_zone": "Australia/Perth",
            "unit_system": "metric",
            "currency": "AUD",
        },
    )
    return token


def _configure_integration(token: str, service_url: str = _APP_URL) -> dict[str, Any]:
    flow = _ha_request(
        "POST",
        "/api/config/config_entries/flow",
        token=token,
        json_body={"handler": "wa_synergy", "show_advanced_options": False},
    )
    result = _ha_request(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        token=token,
        json_body={
            "url": service_url,
            "api_token": _APP_TOKEN,
            "plan": "home_a1",
        },
    )
    if result["type"] != "create_entry":
        raise RuntimeError(f"WA Synergy config flow failed: {result}")
    return result


def _status_sensors(token: str) -> list[dict[str, Any]] | None:
    registry = _ha_websocket(token, {"type": "config/entity_registry/list"})
    unique_ids = {
        entity["entity_id"]: entity["unique_id"]
        for entity in registry
        if entity["platform"] == "wa_synergy"
    }
    sensors = [
        {**state, "unique_id": unique_ids[state["entity_id"]]}
        for state in _ha_request("GET", "/api/states", token=token)
        if state["entity_id"] in unique_ids
    ]
    if len(sensors) != len(unique_ids) or not sensors:
        return None
    for sensor in sensors:
        attributes = sensor["attributes"]
        is_period = sensor["unique_id"].endswith(
            ("_latest_day", "_last_7_days", "_month_to_date")
        )
        is_cost = sensor["unique_id"].endswith("_grid_import_cost")
        is_tariff = sensor["unique_id"].endswith(
            tuple(f"_{key}" for key in _TARIFF_SENSOR_KEYS)
        )
        if sensor["state"] == "unavailable" or (
            sensor["state"] == "unknown"
            and attributes.get("device_class") != "energy"
            and not is_tariff
            and not is_cost
        ):
            return None
        if is_cost:
            if attributes.get("device_class") != "monetary":
                raise RuntimeError(f"invalid cost device class: {sensor}")
            if attributes.get("unit_of_measurement") != "AUD":
                raise RuntimeError(f"invalid cost unit: {sensor}")
            if attributes.get("state_class") != "total":
                raise RuntimeError(f"invalid cost state class: {sensor}")

        if attributes.get("device_class") == "energy":
            if attributes.get("unit_of_measurement") != "kWh":
                raise RuntimeError(f"invalid energy unit: {sensor}")
            expected_class = None if is_period else "total"
            if attributes.get("state_class") != expected_class:
                raise RuntimeError(f"invalid energy state class: {sensor}")
    return sensors


def _ha_websocket(token: str, command: dict[str, Any]) -> Any:
    with connect("ws://localhost:18123/api/websocket", open_timeout=30) as socket:
        required = json.loads(socket.recv())
        if required["type"] != "auth_required":
            raise RuntimeError(
                f"unexpected Home Assistant WebSocket greeting: {required}"
            )
        socket.send(json.dumps({"type": "auth", "access_token": token}))
        authenticated = json.loads(socket.recv())
        if authenticated["type"] != "auth_ok":
            raise RuntimeError(
                f"Home Assistant WebSocket authentication failed: {authenticated}"
            )
        socket.send(json.dumps({"id": 1, **command}))
        while True:
            response = json.loads(socket.recv())
            if response.get("id") != 1:
                continue
            if not response["success"]:
                raise RuntimeError(
                    f"Home Assistant WebSocket command failed: {response}"
                )
            return response["result"]


def _select_tariff(token: str, entry_id: str, plan: str) -> None:
    """Submit the real Home Assistant options flow."""
    flow = _ha_request(
        "POST",
        "/api/config/config_entries/options/flow",
        token=token,
        json_body={"handler": entry_id},
    )
    assert flow["type"] == "form", flow
    result = _ha_request(
        "POST",
        f"/api/config/config_entries/options/flow/{flow['flow_id']}",
        token=token,
        json_body={"plan": plan},
    )
    assert result["type"] == "create_entry", result


def _verify_tariff(token: str, entry_id: str, plan: str) -> dict[str, dict[str, Any]]:
    """Check live entities against published prices, not tariff implementation."""
    expected = _EXPECTED_TARIFFS[plan]

    def ready() -> tuple[dict[str, dict[str, Any]], int] | None:
        entries = _ha_request(
            "GET", "/api/config/config_entries/entry", token=token)
        if not any(
            item["entry_id"] == entry_id and item["state"] == "loaded"
            for item in entries
        ):
            return None
        registry = _ha_websocket(
            token, {"type": "config/entity_registry/list"})
        entity_keys = {
            entity["entity_id"]: key
            for entity in registry
            if entity["config_entry_id"] == entry_id
            for key in _TARIFF_SENSOR_KEYS
            if entity["unique_id"].endswith(f"_{key}")
        }
        before = datetime.now(ZoneInfo("Australia/Perth"))
        states = _ha_request("GET", "/api/states", token=token)
        after = datetime.now(ZoneInfo("Australia/Perth"))
        # Avoid testing against a different hour from the one actually read.
        if (before.date(), before.hour) != (after.date(), after.hour):
            return None
        sensors = {
            entity_keys[state["entity_id"]]: state
            for state in states
            if state["entity_id"] in entity_keys
        }
        if any(key not in sensors for key in _TARIFF_SENSOR_KEYS):
            return None
        if any(sensor["state"] == "unavailable" for sensor in sensors.values()):
            return None
        if sensors["electricity_price"]["attributes"].get("plan") != plan:
            return None
        return sensors, after.hour

    sensors, hour = _wait_for(
        f"loaded {plan} tariff entities", ready, timeout=120)

    def check_price(key: str, wanted: str | None, unit: str = "AUD/kWh") -> None:
        sensor = sensors[key]
        assert sensor["attributes"]["unit_of_measurement"] == unit, sensor
        if wanted is None:
            assert sensor["state"] == "unknown", (plan, key, sensor)
        else:
            assert Decimal(sensor["state"]) == Decimal(wanted), (
                plan,
                key,
                sensor,
                wanted,
            )

    check_price("electricity_price", expected["prices"][hour])
    check_price("daily_supply_charge", expected["supply"], "AUD/day")
    for key, price in (
        ("k1_first_20_price", "0.347481"),
        ("k1_20_to_1650_price", "0.327455"),
        ("k1_above_1650_price", "0.369194"),
    ):
        check_price(key, price if plan == "home_business_k1" else None)
    attributes = sensors["electricity_price"]["attributes"]
    assert attributes["rates_effective_from"] == "2026-07-01", attributes
    assert attributes["time_zone"] == "Australia/Perth", attributes
    assert attributes["pricing_type"] == expected["pricing_type"], attributes
    if plan == "not_set":
        assert attributes["source_url"] is None, attributes
    else:
        assert attributes["source_url"].startswith("https://www.synergy.net.au/"), (
            attributes
        )
    periods_by_price = {
        "0.332621": "flat",
        "0.243431": "off_peak",
        "0.088520": "super_off_peak",
        "0.553253": "peak",
        "0.199172": "overnight",
    }
    schedule = attributes["hourly_prices"]
    assert len(schedule) == 24, (plan, schedule)
    for scheduled_hour, (row, price) in enumerate(
        zip(schedule, expected["prices"], strict=True)
    ):
        assert row["hour"] == scheduled_hour, (plan, row, scheduled_hour)
        if price is None:
            assert row["price"] is None and row["period"] is None, (plan, row)
        else:
            assert isinstance(row["price"], (int, float)), (plan, row)
            assert Decimal(str(row["price"])) == Decimal(
                price), (plan, row, price)
            assert row["period"] == periods_by_price[price], (plan, row, price)
    assert attributes["period"] == schedule[hour]["period"], attributes
    print(
        f"Tariff {plan}: Perth hour {hour:02d}, "
        f"price={sensors['electricity_price']['state']} AUD/kWh, "
        f"supply={sensors['daily_supply_charge']['state']} AUD/day; "
        "24 published hourly prices verified"
    )
    if plan == "home_business_k1":
        print(
            "K1 tier prices verified:",
            ", ".join(
                f"{key}={sensors[key]['state']} AUD/kWh"
                for key in _TARIFF_SENSOR_KEYS[2:]
            ),
        )
    return sensors


def _exercise_tariffs(token: str, entry_id: str) -> None:
    _verify_tariff(token, entry_id, "home_a1")
    for plan in (
        "midday_saver",
        "electric_vehicle",
        "home_business_k1",
        "not_set",
        "home_a1",
    ):
        _select_tariff(token, entry_id, plan)
        _verify_tariff(token, entry_id, plan)
        result = _ha_request(
            "POST",
            f"/api/config/config_entries/entry/{entry_id}/reload",
            token=token,
        )
        assert result["require_restart"] is False, result
        _verify_tariff(token, entry_id, plan)
        print(
            f"Tariff option {plan} persisted an explicit config-entry reload")
        if plan == "electric_vehicle":
            _ha_websocket(
                token,
                {"type": "config/core/update", "time_zone": "America/New_York"},
            )
            try:
                config = _ha_request("GET", "/api/config", token=token)
                assert config["time_zone"] == "America/New_York", config
                result = _ha_request(
                    "POST",
                    f"/api/config/config_entries/entry/{entry_id}/reload",
                    token=token,
                )
                assert result["require_restart"] is False, result
                _verify_tariff(token, entry_id, plan)
                print(
                    "EV tariff retained published Perth-hour pricing after "
                    "reloading with Home Assistant in America/New_York"
                )
            finally:
                _ha_websocket(
                    token,
                    {"type": "config/core/update", "time_zone": "Australia/Perth"},
                )
            config = _ha_request("GET", "/api/config", token=token)
            assert config["time_zone"] == "Australia/Perth", config
    print(
        "Tariff options restored to home_a1. Hourly clock-boundary behavior "
        "was not exercised; current rates were checked at the actual Perth hour."
    )


def _app_statistics(since: datetime | None = None) -> dict[str, Any]:
    response = httpx.get(
        f"{_APP_URL}/v1/statistics",
        headers={"Authorization": f"Bearer {_APP_TOKEN}"},
        params={"since": since.isoformat()} if since is not None else None,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _expected_summaries(points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Derive consumption from cumulative points, without using service summaries."""
    perth = ZoneInfo("Australia/Perth")
    today = datetime.now(perth).date()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        assert point["stream"] == "grid_import", point
        grouped[point["service_point_id"]].append(point)

    def period(daily: dict[date, Decimal], start: date, end: date) -> dict[str, Any]:
        days = [start + timedelta(days=i)
                for i in range((end - start).days + 1)]
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "import_kwh": (
                sum((daily[day] for day in days), Decimal(0))
                if all(day in daily for day in days)
                else None
            ),
        }

    expected = {}
    for service_id, service_points in grouped.items():
        hours: dict[datetime, Decimal] = {}
        previous = Decimal(0)
        for point in sorted(service_points, key=lambda item: item["start"]):
            start = datetime.fromisoformat(point["start"]).astimezone(perth)
            assert start.minute == start.second == start.microsecond == 0, point
            assert start not in hours, point
            cumulative = Decimal(point["sum_kwh"])
            hours[start] = cumulative - previous
            previous = cumulative
        daily: dict[date, Decimal] = {}
        for day in {hour.date() for hour in hours if hour.date() < today}:
            midnight = datetime.combine(day, datetime.min.time(), tzinfo=perth)
            required = [midnight + timedelta(hours=hour) for hour in range(24)]
            if all(hour in hours for hour in required):
                daily[day] = sum((hours[hour]
                                 for hour in required), Decimal(0))

        total_cost = Decimal(0)
        previous_cost_sum = Decimal(0)
        for point in sorted(service_points, key=lambda item: item["start"]):
            start_dt = datetime.fromisoformat(point["start"]).astimezone(perth)
            cum = Decimal(point["sum_kwh"])
            price = Decimal(_EXPECTED_TARIFFS["home_a1"]["prices"][start_dt.hour])
            delta = cum - previous_cost_sum
            if delta > 0:
                total_cost += delta * price
            previous_cost_sum = cum

        summary: dict[str, Any] = {
            "service_point_id": service_id,
            "total_import_kwh": previous,
            "total_import_cost": round(total_cost, 2),
            "latest_day": None,
            "last_7_days": None,
            "month_to_date": None,
        }
        if daily:
            latest = max(daily)
            summary["latest_day"] = period(daily, latest, latest)
            summary["last_7_days"] = period(
                daily, latest - timedelta(days=6), latest)
            month_start = today.replace(day=1)
            if latest >= month_start:
                summary["month_to_date"] = period(daily, month_start, latest)
        expected[service_id] = summary
    return expected


def _verify_usage_sensors(token: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    expected = _expected_summaries(snapshot["statistics"])
    assert expected, "live account returned no complete hourly import data"
    actual = {summary["service_point_id"]              : summary for summary in snapshot["summaries"]}
    assert len(actual) == len(
        snapshot["summaries"]), "duplicate service summaries"
    assert actual.keys() == expected.keys(), (actual.keys(), expected.keys())
    for service_id, summary in expected.items():
        received = actual[service_id]
        assert isinstance(received["total_import_kwh"], str), received
        assert Decimal(received["total_import_kwh"]
                       ) == summary["total_import_kwh"]
        for key in ("latest_day", "last_7_days", "month_to_date"):
            wanted = summary[key]
            if wanted is None:
                assert received[key] is None, (service_id, key, received[key])
                continue
            assert received[key] is not None, (service_id, key, wanted)
            assert received[key]["start_date"] == wanted["start_date"]
            assert received[key]["end_date"] == wanted["end_date"]
            value = received[key]["import_kwh"]
            assert value is None or isinstance(value, str), received[key]
            assert (Decimal(value) if value is not None else None) == wanted[
                "import_kwh"
            ]

    future = datetime.now(UTC) + timedelta(days=365)
    for since in (
        future,
        datetime.now(UTC) - timedelta(days=31),
    ):
        filtered = _app_statistics(since)
        if since == future:
            assert filtered["statistics"] == [
            ], "future since returned hourly data"
        assert filtered["summaries"] == snapshot["summaries"], (
            "summaries changed when filtering hourly statistics",
            since,
        )
        assert filtered["statistics"] == [
            point
            for point in snapshot["statistics"]
            if datetime.fromisoformat(point["start"]) >= since
        ], (
            "since must filter only hourly points, retaining full-history cumulative sums"
        )

    sensors = _status_sensors(token)
    assert sensors is not None, "Home Assistant sensors are not ready"
    by_id = {sensor["unique_id"]: sensor for sensor in sensors}
    status = snapshot["status"]
    prefix = status["instance_id"]

    def sensor_for(key: str) -> dict[str, Any]:
        return by_id[f"{prefix}_{key}"]

    def check_energy(sensor: dict[str, Any], value: Decimal | None) -> None:
        assert sensor["attributes"]["device_class"] == "energy", sensor
        assert sensor["attributes"]["unit_of_measurement"] == "kWh", sensor
        if value is None:
            assert sensor["state"] == "unknown", sensor
        else:
            assert abs(Decimal(sensor["state"]) - value) < Decimal("0.000001"), (
                sensor,
                value,
            )

    for service_id in status["service_points"]:
        summary = expected.get(service_id)
        service_key = service_id.casefold()
        total = sensor_for(f"{service_key}_grid_import")
        check_energy(total, summary["total_import_kwh"] if summary else None)
        assert total["attributes"]["state_class"] == "total", total
        cost = sensor_for(f"{service_key}_grid_import_cost")
        assert cost["attributes"]["device_class"] == "monetary", cost
        assert cost["attributes"]["unit_of_measurement"] == "AUD", cost
        assert cost["attributes"]["state_class"] == "total", cost
        expected_cost = summary["total_import_cost"] if summary else None
        if expected_cost is None:
            assert cost["state"] == "unknown", cost
        else:
            assert abs(Decimal(cost["state"]) - expected_cost) < Decimal("0.01"), (
                cost,
                expected_cost,
            )
        for key in ("latest_day", "last_7_days", "month_to_date"):
            sensor = sensor_for(f"{service_key}_{key}")
            period = summary[key] if summary else None
            check_energy(sensor, period["import_kwh"] if period else None)
            assert sensor["attributes"].get("state_class") is None, sensor
            for attribute in ("start_date", "end_date"):
                assert sensor["attributes"].get(attribute) == (
                    period[attribute] if period else None
                ), sensor
    for key in ("data_through", "last_success"):
        sensor = sensor_for(key)
        assert sensor["attributes"]["device_class"] == "timestamp", sensor
        difference = datetime.fromisoformat(sensor["state"]) - datetime.fromisoformat(
            status[key]
        )
        assert abs(difference.total_seconds()) < 1, sensor
    age = sensor_for("data_age")
    assert age["attributes"]["device_class"] == "duration", age
    assert age["attributes"]["unit_of_measurement"] == "h", age
    assert age["attributes"]["state_class"] == "measurement", age
    expected_age = max(
        0,
        (
            datetime.now(UTC) - datetime.fromisoformat(status["data_through"])
        ).total_seconds()
        / 3600,
    )
    assert abs(float(age["state"]) - expected_age) < 0.1, (age, expected_age)
    sync = sensor_for("sync_status")
    assert sync["state"] == "idle", sync
    assert sync["attributes"]["device_class"] == "enum", sync
    assert set(sync["attributes"]["options"]) == {
        "waiting", "syncing", "idle", "error"}
    assert "last_error" in sync["attributes"], sync
    assert sync["attributes"]["last_error"] == status["last_error"], sync
    return sensors


def _imported_statistics(
    token: str, points: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]] | None:
    metadata = _ha_websocket(
        token,
        {"type": "recorder/list_statistic_ids", "statistic_type": "sum"},
    )
    statistic_ids = [
        item["statistic_id"]
        for item in metadata
        if item["statistic_id"].startswith("wa_synergy:")
    ]
    expected: dict[str, dict[float, Decimal]] = defaultdict(dict)
    for point in points:
        statistic_id = f"wa_synergy:{point['service_point_id'].casefold()}_grid_import"
        expected[statistic_id][datetime.fromisoformat(point["start"]).timestamp()] = (
            Decimal(point["sum_kwh"])
        )
    if not statistic_ids:
        return None
    if set(statistic_ids) != expected.keys():
        return None
    statistics = _ha_websocket(
        token,
        {
            "type": "recorder/statistics_during_period",
            "start_time": min(point["start"] for point in points),
            "statistic_ids": statistic_ids,
            "period": "hour",
            "types": ["sum"],
            "units": {"energy": "kWh"},
        },
    )
    for statistic_id, hourly in expected.items():
        rows = statistics.get(statistic_id, [])
        actual = {}
        for row in rows:
            start = row["start"]
            timestamp = (
                datetime.fromisoformat(start).timestamp()
                if isinstance(start, str)
                else start / 1000
            )
            actual[timestamp] = row["sum"]
        if actual.keys() != hourly.keys():
            return None
        assert len(actual) == len(
            rows), f"duplicate Recorder hours: {statistic_id}"
        for timestamp, total in hourly.items():
            assert actual[timestamp] is not None, (statistic_id, timestamp)
            assert abs(Decimal(str(actual[timestamp])) - total) < Decimal("0.000001"), (
                statistic_id,
                timestamp,
                actual[timestamp],
                total,
            )
    return statistics


def _configure_energy_dashboard(
    token: str,
    statistic_ids: list[str],
) -> dict[str, Any]:
    energy_sources = [
        {
            "type": "grid",
            "stat_energy_from": statistic_id,
            "stat_energy_to": None,
            "stat_cost": None,
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0,
            "name": "WA Synergy",
        }
        for statistic_id in sorted(statistic_ids)
    ]
    preferences = _ha_websocket(
        token,
        {
            "type": "energy/save_prefs",
            "energy_sources": energy_sources,
            "device_consumption": [],
            "device_consumption_water": [],
        },
    )
    validation = _ha_websocket(token, {"type": "energy/validate"})
    issues = [
        issue for groups in validation.values() for group in groups for issue in group
    ]
    if issues:
        raise RuntimeError(f"Energy dashboard rejected WA Synergy: {issues}")
    return preferences


def _run_ha_verification(service_url: str = _APP_URL) -> None:
    print("Waiting for live Synergy synchronization...")
    status = _wait_for("live Synergy synchronization",
                       _app_status, timeout=900)
    print(
        f"Live sync complete: {len(status['service_points'])} service point(s), "
        f"data through {status['data_through']}"
    )
    print("Waiting for Home Assistant API...")
    _wait_for(
        "Home Assistant API",
        lambda: _ha_request("GET", "/api/onboarding"),
        timeout=180,
    )
    print("Onboarding Home Assistant...")
    token = _onboard_home_assistant()
    print(f"Configuring WA Synergy integration (service_url={service_url})...")
    entry = _configure_integration(token, service_url=service_url)
    print(f"Config entry created: {entry['title']}")
    print("Waiting for Home Assistant status sensors...")
    sensors = _wait_for(
        "WA Synergy Home Assistant sensors",
        lambda: _status_sensors(token),
        timeout=120,
    )
    _exercise_tariffs(token, entry["result"]["entry_id"])
    snapshot = _app_statistics()
    sensors = _verify_usage_sensors(token, snapshot)
    print("Waiting for WA Synergy Recorder statistics...")
    statistics = _wait_for(
        "WA Synergy Recorder statistics",
        lambda: _imported_statistics(token, snapshot["statistics"]),
        timeout=120,
    )
    print("Refreshing Home Assistant's incremental statistics...")
    energy_sensor = next(
        sensor for sensor in sensors if sensor["unique_id"].endswith("_grid_import")
    )
    _ha_request(
        "POST",
        "/api/services/homeassistant/update_entity",
        token=token,
        json_body={"entity_id": energy_sensor["entity_id"]},
    )
    snapshot = _app_statistics()
    sensors = _verify_usage_sensors(token, snapshot)
    statistics = _wait_for(
        "unchanged hourly Recorder sums after incremental refresh",
        lambda: _imported_statistics(token, snapshot["statistics"]),
        timeout=120,
    )
    print("Configuring the Home Assistant Energy dashboard...")
    # Delayed historical consumption must not use today's tariff for costs.
    energy_preferences = _configure_energy_dashboard(token, list(statistics))

    print(
        "Live Synergy sync:",
        f"{len(status['service_points'])} service point(s),",
        f"data through {status['data_through']}",
    )
    print("Home Assistant config entry:", entry["title"])
    print(
        "Home Assistant Energy dashboard:",
        ", ".join(
            source["stat_energy_from"]
            for source in energy_preferences["energy_sources"]
        ),
    )
    print(
        "Home Assistant sensors:",
        ", ".join(f"{item['entity_id']}={item['state']}" for item in sensors),
    )
    print("Live usage summaries (kWh, inclusive Perth dates):")
    print(json.dumps(snapshot["summaries"], indent=2))
    print(
        "Home Assistant Recorder:",
        ", ".join(
            f"{statistic_id}={len(rows)} point(s)"
            for statistic_id, rows in statistics.items()
        ),
    )


def main() -> None:
    load_dotenv()
    required = (
        "WA_SYNERGY_EMAIL",
        "WA_SYNERGY_PASSWORD",
        "WA_SYNERGY_GMAIL_APP_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"missing live Synergy credentials: {', '.join(missing)}")

    if not _can_compose():
        raise RuntimeError(
            "live_test.py requires a working Docker or Podman Compose runtime"
        )

    _compose("down", "--volumes", check=False)
    try:
        _compose("up", "--build", "--detach")
        _run_ha_verification(service_url="http://synergy-app:8099")
        _exercise_live_network_failure()
    except BaseException:
        _compose("logs", "--timestamps", check=False)
        raise
    finally:
        _compose("down", "--volumes", check=False)


if __name__ == "__main__":
    main()
