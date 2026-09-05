# wa-synergy

Fetch WA Synergy interval usage and store it in SQLite.

## Install

```console
python -m pip install -e '.[dev]'
python -m playwright install chromium
```

Set these values in `.env`:

```dotenv
WA_SYNERGY_EMAIL=your-account@gmail.com
WA_SYNERGY_PASSWORD=your-synergy-password
WA_SYNERGY_GMAIL_APP_PASSWORD=your-gmail-app-password
```

## Use

```python
import os
from pathlib import Path

from dotenv import load_dotenv

from wa_synergy import SynergyClient, SynergyCredentials, UsageQuery, sync_usage_to_db

load_dotenv()
credentials = SynergyCredentials(
    email=os.environ["WA_SYNERGY_EMAIL"],
    password=os.environ["WA_SYNERGY_PASSWORD"],
    gmail_app_password=os.environ["WA_SYNERGY_GMAIL_APP_PASSWORD"],
)
query = UsageQuery(start="2026-07-01", end="2026-08-01")

with SynergyClient(credentials=credentials) as client:
    result = sync_usage_to_db(
        client=client,
        query=query,
        db_path=Path("data/synergy.sqlite3"),
    )

print(result)
```

The client opens Playwright only for login and OTP, captures the `sid`, Aura token, Aura context, and service identifier, closes the browser, calls the Aura API directly, then stores normalized intervals in SQLite.

Salesforce can return HTTP 200 with a rejected session inside the Aura response.
The client matches the requested action by ID, independently of additional portal
warnings, and reauthenticates once after an Apex-class access denial. If the new
session is also rejected, synchronization fails with Salesforce's error message.
Check `last_success`, `last_error`, and `data_through` at `/v1/status` to distinguish
a completed sync from an HTTP request that merely returned 200.

Run the real flow with:

```console
python live_test.py
```

## Home Assistant

The Home Assistant deployment has two parts:

1. Install this repository as a Home Assistant app repository, then install and
   configure the **WA Synergy** app. Use a random API token of at least 32
   characters. The app owns Chromium, Gmail OTP access, synchronization, and its
   interval database.
2. Install the `wa_synergy` custom integration through HACS, restart Home
   Assistant, and add **WA Synergy** under **Settings > Devices & services**.
   Enter the app URL and the same API token. With the default published port, the
   URL is `http://<home-assistant-host>:8099`.

The integration exposes a cumulative `Grid import <service point>` energy sensor
and imports timestamped hourly grid-import statistics into Recorder. To use the
historical data in the Energy dashboard, add a **Grid consumption** source and
select `Synergy <service point> grid import`. On first setup the integration
imports the complete app history; subsequent updates rewrite a rolling correction
window. `VAL_SOLAR` is retained in the source database but is not exposed as
generation or export because the provider response does not establish that
meaning.

### Sensors

Each service point exposes these energy sensors:

| Sensor | Meaning |
| --- | --- |
| Grid import | Cumulative kWh across the app's stored complete hourly readings, not a lifetime meter reading. |
| Latest day usage | Import kWh for the latest complete reported Perth calendar day. |
| Last 7 days usage | Import kWh for seven consecutive complete days ending on that reported day. |
| Month to date usage | Import kWh from the first of the current Perth month through that reported day. |

The three period sensors include inclusive `start_date` and `end_date` attributes.
A complete day requires all 24 complete hourly readings. Missing coverage yields
`unknown`, never zero or a partial period total. Month-to-date is also unknown if
no complete day has been reported in the current month. These are delayed usage
readings, not live power measurements or cost estimates.

The cumulative sensor uses Home Assistant's `total` state class so downward
provider corrections are not interpreted as meter resets. Period snapshots have
no state class: adding their changing totals as new Energy dashboard consumption
would double-count usage. Continue using the external hourly statistics described
above for the Energy dashboard. Existing sensor unique IDs and Recorder statistic
IDs are retained.

Account-level diagnostics include:

- **Data available through** and **Last successful sync** timestamps.
- **Data age**, in hours since the available data ends.
- **Sync status**: `waiting`, `syncing`, `idle`, or `error`, with a `last_error`
  attribute containing the provider-sync error type.

Diagnostics reflect the latest hourly integration poll; an unreachable app makes
its entities unavailable. Update/rebuild the companion app before updating the
integration, then restart Home Assistant: the new sensors require the app's
full-history usage summaries.

AppDaemon is not used. It would still require a custom Chromium-capable image and
would need privileged WebSocket access to Recorder's statistics import API, while
adding a second framework and YAML configuration instead of a native config flow.

## Home Assistant test stack

`live_test.py` requires Docker Compose or Podman Compose. It builds the current
companion app, synchronizes it against the real Synergy account configured in
`.env`, starts Home Assistant 2026.9 in a container, completes onboarding,
configures the integration, and verifies its sensors and Recorder statistics
through Home Assistant's APIs, including a subsequent incremental statistics
refresh with a timezone-aware `since` parameter. It independently calculates
period totals from live hourly readings, checks the actual HA sensors and their
reporting dates, and verifies Recorder sums. Summaries must remain unchanged even
when a future `since` returns no hourly points.

Install the development dependencies, then run the complete live test:

```console
python -m pip install -e '.[dev]'
python live_test.py
```

The test starts `compose.ha-test.yaml` with fresh app and Home Assistant volumes.
After the initial verification, it disconnects and restarts the real companion
container, verifies that the resulting authentication failure is logged without
killing the sync thread, restores the network, and performs another live Synergy
sync. Failures print timestamped container logs. The test then removes the
containers and volumes. Set `WA_SYNERGY_LIVE_TEST_BACKFILL_DAYS` to override the
default 14-day live-data window.

To verify that insufficient history leaves seven-day and month-to-date periods
unknown where coverage is missing, run a short live backfill:

```console
WA_SYNERGY_LIVE_TEST_BACKFILL_DAYS=3 python live_test.py
```

