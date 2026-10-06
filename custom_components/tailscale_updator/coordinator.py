"""Keep switches synchronized with the remote policy."""

import logging
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApiError, AuthError, TailscaleClient
from .const import DOMAIN
from .policy import PolicyError

_LOGGER = logging.getLogger(__name__)


class PolicyCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client: TailscaleClient):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=60),
        )
        self.client = client

    async def _async_update_data(self):
        try:
            # Serialize reads with writes to avoid publishing a stale poll afterward.
            async with self.client.write_lock:
                return await self.client.get_policy()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (ApiError, PolicyError) as err:
            raise UpdateFailed(str(err)) from err

    async def async_write(self, operation, *args):
        try:
            await operation(*args)
        except AuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(str(err)) from err
        except (ApiError, PolicyError) as err:
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(str(err)) from err
        # Re-read under the coordinator's lock rather than publish a possibly stale result.
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError(
                "Policy was submitted, but its current state could not be read"
            )
