"""Exercise the real Synergy account through the app and Home Assistant."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from dotenv import load_dotenv
from websockets.sync.client import connect

_COMPOSE_BIN = (
    "docker" if shutil.which("docker") else "podman" if shutil.which("podman") else None
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
        raise RuntimeError("cannot identify the running companion service container")
    inspected = json.loads(_runtime("inspect", container_id))
    labels = inspected[0]["Config"]["Labels"]
    expected_labels = {
        "io.hass.arch": "amd64",
        "io.hass.type": "app",
        "io.hass.version": "0.1.7",
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
            raise RuntimeError("synergy-sync thread crashed during network outage")
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
        },
    )
    if result["type"] != "create_entry":
        raise RuntimeError(f"WA Synergy config flow failed: {result}")
    return result


def _status_sensors(token: str) -> list[dict[str, Any]] | None:
    states = _ha_request("GET", "/api/states", token=token)
    sensors = [
        state for state in states if state["entity_id"].startswith("sensor.wa_synergy_")
    ]
    energy_sensors = [
        state
        for state in sensors
        if state["attributes"].get("device_class") == "energy"
        and state["attributes"].get("state_class") == "total_increasing"
        and state["attributes"].get("unit_of_measurement") == "kWh"
    ]
    if (
        len(sensors) >= 3
        and energy_sensors
        and all(state["state"] not in {"unknown", "unavailable"} for state in sensors)
    ):
        return sensors
    return None


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


def _imported_statistics(token: str) -> dict[str, list[dict[str, Any]]] | None:
    metadata = _ha_websocket(
        token,
        {"type": "recorder/list_statistic_ids", "statistic_type": "sum"},
    )
    statistic_ids = [
        item["statistic_id"]
        for item in metadata
        if item["statistic_id"].startswith("wa_synergy:")
    ]
    if not statistic_ids:
        return None
    statistics = _ha_websocket(
        token,
        {
            "type": "recorder/statistics_during_period",
            "start_time": (datetime.now(UTC) - timedelta(days=45)).isoformat(),
            "statistic_ids": statistic_ids,
            "period": "hour",
            "types": ["sum"],
            "units": {"energy": "kWh"},
        },
    )
    if all(statistics.get(statistic_id) for statistic_id in statistic_ids):
        return statistics
    return None


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
    status = _wait_for("live Synergy synchronization", _app_status, timeout=900)
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
    print("Waiting for WA Synergy Recorder statistics...")
    statistics = _wait_for(
        "WA Synergy Recorder statistics",
        lambda: _imported_statistics(token),
        timeout=120,
    )
    print("Refreshing Home Assistant's incremental statistics...")
    energy_sensor = next(
        sensor
        for sensor in sensors
        if sensor["attributes"].get("device_class") == "energy"
    )
    _ha_request(
        "POST",
        "/api/services/homeassistant/update_entity",
        token=token,
        json_body={"entity_id": energy_sensor["entity_id"]},
    )
    if _status_sensors(token) is None:
        raise RuntimeError("Home Assistant incremental statistics refresh failed")
    print("Configuring the Home Assistant Energy dashboard...")
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
        raise RuntimeError(f"missing live Synergy credentials: {', '.join(missing)}")

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
