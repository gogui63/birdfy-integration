"""Config flow for Birdfy integration."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
from typing import Any
import uuid

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_MAX_DAILY_ANALYSES,
    DEFAULT_MAX_DAILY_ANALYSES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://localweb.nvts.co/v1/users/login/v2"

UCID = "b3cf543b57"

STEP_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
})
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


def _generate_udid() -> str:
    return f"android-{uuid.uuid4()}"


async def _test_login(hass: HomeAssistant, email: str, password: str, udid: str) -> str | None:
    """Return error key or None on success."""
    pwd_md5 = hashlib.md5(password.encode()).hexdigest()
    payload = {"username": email, "password": pwd_md5, "locale": "en-US"}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-nvs-ucid": UCID,
        "x-nvs-udid": udid,
        "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
    }
    try:
        async with async_get_clientsession(hass).post(LOGIN_URL, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if data.get("ret", 0) != 0 or not data.get("token"):
                _LOGGER.error("Birdfy login failed: %s", data.get("msg"))
                return "invalid_auth"
    except Exception as e:
        _LOGGER.error("Birdfy login exception: %s", e)
        return "cannot_connect"
    return None


class BirdfyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Birdfy config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BirdfyOptionsFlow:
        return BirdfyOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            udid = _generate_udid()
            error = await _test_login(self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD], udid)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        "ucid": UCID,
                        "udid": udid,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            error = await _test_login(
                self.hass, entry.data[CONF_EMAIL], user_input[CONF_PASSWORD], entry.data["udid"]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class BirdfyOptionsFlow(config_entries.OptionsFlowWithReload):
    """Choose the AI Task entity identifying the birds."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            user_input[CONF_MAX_DAILY_ANALYSES] = int(user_input[CONF_MAX_DAILY_ANALYSES])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_AI_TASK_ENTITY,
                description={"suggested_value": options.get(CONF_AI_TASK_ENTITY)},
            ): EntitySelector(
                EntitySelectorConfig(
                    filter=EntityFilterSelectorConfig(
                        domain="ai_task",
                        supported_features=["ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS"],
                    )
                )
            ),
            vol.Required(
                CONF_MAX_DAILY_ANALYSES,
                default=options.get(CONF_MAX_DAILY_ANALYSES, DEFAULT_MAX_DAILY_ANALYSES),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=1000, step=1, mode=NumberSelectorMode.BOX)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
