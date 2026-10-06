"""Admin-only policy services, including response-capable policy reads."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN


@callback
def async_setup_services(hass):
    if hass.services.has_service(DOMAIN, "get_acl"):
        return

    async def handle(call):
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized()
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or not hasattr(entry, "runtime_data")
        ):
            raise HomeAssistantError("Select a loaded Tailscale Updator entry")
        if entry.state != ConfigEntryState.LOADED:
            raise HomeAssistantError("Tailscale Updator entry is not loaded")
        coordinator = entry.runtime_data
        if call.service == "get_acl":
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise HomeAssistantError("Unable to read policy")
            return {"policy": coordinator.data.text, "etag": coordinator.data.etag}
        if call.service == "set_acl":
            await coordinator.async_write(
                coordinator.client.replace_policy,
                call.data["policy"],
                call.data["expected_etag"],
            )
        else:
            await coordinator.async_write(
                coordinator.client.set_domains,
                call.data["connector"],
                call.data["domains"],
                call.service == "add_domains",
            )

    base = {vol.Required("entry_id"): cv.string}
    hass.services.async_register(
        DOMAIN,
        "get_acl",
        handle,
        schema=vol.Schema(base),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "set_acl",
        handle,
        schema=vol.Schema(
            {
                **base,
                vol.Required("policy"): cv.string,
                vol.Required("expected_etag"): cv.string,
            }
        ),
    )
    for service in ("add_domains", "remove_domains"):
        hass.services.async_register(
            DOMAIN,
            service,
            handle,
            schema=vol.Schema(
                {
                    **base,
                    vol.Required("connector"): cv.string,
                    vol.Required("domains"): vol.All(
                        cv.ensure_list, [cv.string], vol.Length(min=1)
                    ),
                }
            ),
        )
