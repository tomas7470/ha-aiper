"""Constants for the Aiper integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final[str] = "aiper"
PLATFORMS: Final[list[str]] = ["valve", "sensor", "binary_sensor", "switch", "button", "number"]

# Config-entry data keys.
CONF_EMAIL: Final[str] = "email"
CONF_PASSWORD: Final[str] = "password"
CONF_REGION: Final[str] = "region"
CONF_API_BASE: Final[str] = "api_base"
CONF_TOKEN: Final[str] = "token"

# Region values that the user can pick in config flow. The actual API base is
# returned by the login response (`domain[0]`) — these are just for the initial
# /login call.
REGION_INTERNATIONAL: Final[str] = "international"
REGION_CHINA: Final[str] = "chinese"

REGION_BASES: Final[dict[str, str]] = {
    # The international app round-robins between these on first launch and
    # follows the redirect in the login response. We default to EU; if the
    # account lives elsewhere the server returns 5050 and we retry US.
    "international": "https://apieurope.aiper.com",
    "chinese": "https://apichina.aiper.com",
}

# Fallback list to try in order when initial region is unknown.
REGION_FALLBACK_BASES: Final[tuple[str, ...]] = (
    "https://apiamerica.aiper.com",
    "https://apieurope.aiper.com",
    "https://apichina.aiper.com",
)

# Pulled from the iOS app's User-Agent in the prior research; the Android v3.3.0
# UA looks similar — server doesn't appear to gate on this.
USER_AGENT: Final[str] = (
    "Aiper-Link-Android/3.3.0 (com.aiper.link; build:74; Android 16) okhttp/5.0.0-alpha.10"
)
APP_VERSION: Final[str] = "3.3.0"

# Default poll interval (seconds) for the cloud_polling MVP.
DEFAULT_SCAN_INTERVAL: Final[int] = 30
