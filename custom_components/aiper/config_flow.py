"""Config flow for the Aiper IrriSense integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AiperAuthError, AiperClient, AiperError
from .const import (
    CONF_API_BASE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TOKEN,
    DOMAIN,
    REGION_CHINA,
    REGION_INTERNATIONAL,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=REGION_INTERNATIONAL): vol.In(
            [REGION_INTERNATIONAL, REGION_CHINA]
        ),
    }
)


class AiperConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email: str = user_input[CONF_EMAIL]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = AiperClient(session, region=user_input[CONF_REGION])
            try:
                result = await client.login(email, user_input[CONF_PASSWORD])
            except AiperAuthError as exc:
                _LOGGER.warning("Aiper login failed: %s", exc)
                errors["base"] = "invalid_auth"
            except (AiperError, aiohttp.ClientError) as exc:
                _LOGGER.warning("Aiper login error: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_REGION: user_input[CONF_REGION],
                        CONF_API_BASE: result.api_base,
                        CONF_TOKEN: result.token,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )
