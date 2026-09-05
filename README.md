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

AppDaemon is not used. It would still require a custom Chromium-capable image and
would need privileged WebSocket access to Recorder's statistics import API, while
adding a second framework and YAML configuration instead of a native config flow.

## Home Assistant test stack

`live_test.py` requires Docker Compose or Podman Compose. It builds the current
companion app, synchronizes it against the real Synergy account configured in
`.env`, starts Home Assistant 2026.9 in a container, completes onboarding,
configures the integration, and verifies its sensors and Recorder statistics
through Home Assistant's APIs.

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

