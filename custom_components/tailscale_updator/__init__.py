"""Tailscale Updator integration."""

from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TailscaleClient
from .coordinator import PolicyCoordinator
from .services import async_setup_services

PLATFORMS = [Platform.SWITCH]


async def async_setup_entry(hass, entry):
    client = TailscaleClient(
        async_get_clientsession(hass),
        "-",
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
    )
    coordinator = PolicyCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
