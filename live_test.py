import os
from pathlib import Path

from dotenv import load_dotenv

from wa_synergy import (
    SynergyClient,
    SynergyCredentials,
    UsageQuery,
    sync_usage_to_db,
)

load_dotenv()
credentials = SynergyCredentials(
    email=os.environ["WA_SYNERGY_EMAIL"],
    password=os.environ["WA_SYNERGY_PASSWORD"],
    gmail_app_password=os.environ["WA_SYNERGY_GMAIL_APP_PASSWORD"],
)
query = UsageQuery(
    start="2026-07-01",
    end="2030-08-01",
    # account_ids=("provider-account-id",),
    # service_point_ids=("provider-service-id",),
)

with SynergyClient(credentials=credentials) as client:
    result = sync_usage_to_db(
        client=client,
        query=query,
        db_path=Path("data/synergy.sqlite3"),
    )

total = result.inserted + result.updated + result.unchanged
print(
    f"Synced {total} intervals: "
    f"{result.inserted} inserted, {result.updated} updated, "
    f"{result.unchanged} unchanged"
)
