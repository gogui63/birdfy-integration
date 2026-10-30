"""Tests of the config, options and reauth flows."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.birdfy.const import (
    CONF_AI_TASK_ENTITY,
    CONF_MAX_DAILY_ANALYSES,
    DOMAIN,
)

from .conftest import AI_ENTITY

LOGIN = "custom_components.birdfy.config_flow._test_login"
USER = {"email": "user@example.com", "password": "secret"}


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(LOGIN, return_value="invalid_auth"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(LOGIN, return_value="cannot_connect"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["errors"] == {"base": "cannot_connect"}

    with patch(LOGIN, return_value=None), patch(
        "custom_components.birdfy.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["email"] == USER["email"]
    assert result["data"]["udid"].startswith("android-")


async def test_duplicate_account_is_refused(hass, entry) -> None:
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_enables_and_disables_ai(
    hass, ai_task, entry, setup_entry
) -> None:
    await setup_entry(entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_AI_TASK_ENTITY: AI_ENTITY, CONF_MAX_DAILY_ANALYSES: 20.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_AI_TASK_ENTITY: AI_ENTITY, CONF_MAX_DAILY_ANALYSES: 20}
    assert entry.runtime_data.identifier is not None
    assert hass.states.get("sensor.birdfy_last_species") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MAX_DAILY_ANALYSES: 20}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.identifier is None
    assert hass.states.get("sensor.birdfy_last_species") is None


async def test_reauth_flow(hass, entry, setup_entry) -> None:
    await setup_entry(entry)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(LOGIN, return_value="invalid_auth"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "wrong"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(LOGIN, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
