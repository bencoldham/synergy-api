"""Constants for the WA Synergy integration."""

from datetime import timedelta

DOMAIN = "wa_synergy"
CONF_API_TOKEN = "api_token"
DEFAULT_URL = "http://localhost:8099"
CORRECTION_WINDOW = timedelta(days=31)
UPDATE_INTERVAL = timedelta(hours=1)
