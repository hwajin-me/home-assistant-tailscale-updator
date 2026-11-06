"""Keep switches synchronized with the remote policy."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApiError, AuthError, ConflictError, TailscaleClient
from .const import CONF_DOMAIN_GROUPS, DOMAIN
from .groups import rename_group_member
from .policy import Policy, PolicyError, domain_bases
from .registry import forget_domains, remembered_pairs

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
        self.edit_lock = asyncio.Lock()

    async def _async_update_data(self):
        try:
            # Serialize reads with writes to avoid publishing a stale poll afterward.
            async with self.client.write_lock:
                return await self.client.get_policy()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (ApiError, PolicyError) as err:
            raise UpdateFailed(str(err)) from err

    async def async_delete_domains(self, connector, domains):
        """Delete from ACL and forget entities; an ordinary Off never forgets them."""
        async with self.edit_lock:

            async def remove():
                snapshot = await self.client.get_policy()
                if connector in snapshot.policy.connectors():
                    await self.client.set_domains(connector, domains, False)

            await self._async_write(remove)
            forget_domains(self.hass, self.config_entry, connector, domains)

    async def async_replace_connectors(self, text, expected_etag):
        """Explicit app-policy edits also forget explicitly removed domains."""
        async with self.edit_lock:
            removed = {}

            async def replace():
                snapshot = await self.client.get_policy()
                if snapshot.etag != expected_etag:
                    raise ConflictError("Policy changed; reload it before retrying")
                original_policy = snapshot.policy
                proposed = Policy(original_policy.merge_connector_changes(text))
                before_names = original_policy.connectors()
                after_names = proposed.connectors()
                remembered = remembered_pairs(self.hass, self.config_entry)
                for connector in before_names:
                    saved = {domain for name, domain in remembered if name == connector}
                    try:
                        original = domain_bases(original_policy.domains(connector))
                    except PolicyError:
                        original = None
                    if connector not in after_names:
                        deleted = (original or set()) | saved
                    elif original is None:
                        continue
                    else:
                        try:
                            remaining = domain_bases(proposed.domains(connector))
                        except PolicyError:
                            deleted = original | saved
                        else:
                            deleted = original - remaining
                    if deleted:
                        removed[connector] = sorted(deleted)
                await self.client.replace_policy(text, expected_etag)

            await self._async_write(replace)
            for connector, domains in removed.items():
                forget_domains(self.hass, self.config_entry, connector, domains)

    async def async_rename_domain(self, connector, old_domain, new_domain):
        async with self.edit_lock:

            async def rename():
                await self.client.change_domain_pairs(
                    connector, [new_domain], [old_domain], require_existing=True
                )

            await self._async_write(rename)
            rename_group_member(
                self.hass, self.config_entry, connector, old_domain, new_domain
            )

    async def async_set_domain_group(self, group_id, enabled):
        async with self.edit_lock:
            # Read membership after waiting for any deletion or group edit.
            group = self.config_entry.options.get(CONF_DOMAIN_GROUPS, {}).get(
                group_id, {}
            )
            await self._async_write(
                self.client.set_domain_group, group.get("members", []), enabled
            )

    async def async_write(self, operation, *args):
        async with self.edit_lock:
            await self._async_write(operation, *args)

    async def _async_write(self, operation, *args):
        try:
            await operation(*args)
        except AuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(str(err)) from err
        except PolicyError as err:
            # Invalid local input does not invalidate the last successful remote read.
            raise HomeAssistantError(str(err)) from err
        except ApiError as err:
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(str(err)) from err
        # Re-read under the coordinator's lock rather than publish a possibly stale result.
        await self.async_refresh()
        if not self.last_update_success:
            raise HomeAssistantError(
                "Policy was submitted, but its current state could not be read"
            )
