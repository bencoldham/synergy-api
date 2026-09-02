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
