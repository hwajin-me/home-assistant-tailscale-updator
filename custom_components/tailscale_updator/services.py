"""Admin-only policy services, including response-capable policy reads."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .groups import delete_group, save_group
from .policy import PolicyError


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
        if call.service in ("set_domain_group", "delete_domain_group"):
            try:
                async with coordinator.edit_lock:
                    if call.service == "set_domain_group":
                        save_group(
                            hass,
                            entry,
                            call.data["name"],
                            call.data["members"],
                            call.data["group_id"],
                        )
                    else:
                        delete_group(hass, entry, call.data["group_id"])
            except PolicyError as err:
                raise HomeAssistantError(str(err)) from err
            return
        if call.service == "get_acl":
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise HomeAssistantError("Unable to read policy")
            return {"policy": coordinator.data.text, "etag": coordinator.data.etag}
        if call.service == "set_acl":
            await coordinator.async_replace_connectors(
                call.data["policy"],
                call.data["expected_etag"],
            )
        elif call.service == "remove_domains":
            await coordinator.async_delete_domains(
                call.data["connector"], call.data["domains"]
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
    hass.services.async_register(
        DOMAIN,
        "set_domain_group",
        handle,
        schema=vol.Schema(
            {
                **base,
                vol.Required("group_id"): cv.string,
                vol.Required("name"): cv.string,
                vol.Required("members"): vol.All(
                    cv.ensure_list,
                    [
                        vol.Schema(
                            {
                                vol.Required("connector"): cv.string,
                                vol.Required("domain"): cv.string,
                            }
                        )
                    ],
                    vol.Length(min=1),
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "delete_domain_group",
        handle,
        schema=vol.Schema(
            {
                **base,
                vol.Required("group_id"): cv.string,
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
