"""Config flow for the WA Synergy integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import CannotConnect, InvalidAuth, InvalidResponse, SynergyServiceClient
from .const import CONF_API_TOKEN, DEFAULT_URL, DOMAIN
from .tariffs import CONF_PLAN, DEFAULT_PLAN, PLAN_OPTIONS


class SynergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a WA Synergy companion service."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SynergyOptionsFlow:
        """Allow tariff selection without changing service credentials."""

        return SynergyOptionsFlow()

    async def _validate(self, data: dict[str, Any]) -> str:
        base_url = _normalized_url(data[CONF_URL])
        client = SynergyServiceClient(
            async_get_clientsession(self.hass),
            base_url,
            data[CONF_API_TOKEN],
        )
        status = await client.async_get_status()
        return status.instance_id

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle user setup."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                user_input[CONF_URL] = _normalized_url(user_input[CONF_URL])
                instance_id = await self._validate(user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (CannotConnect, InvalidResponse, ValueError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(instance_id)
                self._abort_if_unique_id_configured()
                user_input.setdefault(CONF_PLAN, DEFAULT_PLAN)
                return self.async_create_entry(title="WA Synergy", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input).extend(_plan_schema(user_input).schema),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start API-token reauthentication."""

        self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate replacement service connection details."""

        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                user_input[CONF_URL] = _normalized_url(user_input[CONF_URL])
                instance_id = await self._validate(user_input)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except (CannotConnect, InvalidResponse, ValueError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(instance_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        defaults = user_input or {
            CONF_URL: entry.data[CONF_URL],
            CONF_API_TOKEN: entry.data[CONF_API_TOKEN],
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema(defaults),
            errors=errors,
        )


class SynergyOptionsFlow(config_entries.OptionsFlow):
    """Select the published tariff used locally by Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage tariff selection."""

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_PLAN: user_input.get(CONF_PLAN, DEFAULT_PLAN)},
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_plan_schema(
                {
                    CONF_PLAN: self.config_entry.options.get(
                        CONF_PLAN, self.config_entry.data.get(CONF_PLAN, DEFAULT_PLAN)
                    )
                }
            ),
        )


def _plan_schema(values: dict[str, Any] | None) -> vol.Schema:
    values = values or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_PLAN, default=values.get(CONF_PLAN, DEFAULT_PLAN)
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(PLAN_OPTIONS),
                    translation_key="plan",
                )
            )
        }
    )


def _schema(values: dict[str, Any] | None) -> vol.Schema:
    values = values or {}
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=values.get(CONF_URL, DEFAULT_URL)): str,
            vol.Required(
                CONF_API_TOKEN,
                default=values.get(CONF_API_TOKEN, ""),
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


def _normalized_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("service URL must be a string")
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("service URL must contain only an HTTP origin")
    return normalized
