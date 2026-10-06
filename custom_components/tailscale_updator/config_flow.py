"""Configure OAuth credentials and app-domain switches from the UI."""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, ApiHttpError, AuthError, TailscaleClient
from .const import DOMAIN
from .policy import PolicyError, domain_base, domain_bases
from .registry import remembered_pairs

_LOGGER = logging.getLogger(__name__)


def policy_read_error(err: ApiError | PolicyError) -> str:
    """Report the failed stage without exposing response bodies or credentials."""
    _LOGGER.warning("Tailscale policy read failed: %s", err)
    if isinstance(err, ApiHttpError):
        if err.status == 403:
            return "policy_forbidden"
        if err.status == 404:
            return "tailnet_not_found"
        return "api_error"
    if isinstance(err, PolicyError):
        return "invalid_policy"
    return "cannot_connect"


def credentials_schema():
    return vol.Schema(
        {
            vol.Required(CONF_CLIENT_ID): str,
            vol.Required(CONF_CLIENT_SECRET): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data):
        client = TailscaleClient(
            async_get_clientsession(self.hass),
            "-",
            data[CONF_CLIENT_ID],
            data[CONF_CLIENT_SECRET],
        )
        await client.get_policy()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = {
                CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET].strip(),
            }
            await self.async_set_unique_id(f"oauth:{user_input[CONF_CLIENT_ID]}")
            self._abort_if_unique_id_configured()
            # Older entries used the tailnet as their unique ID.
            self._async_abort_entries_match(
                {CONF_CLIENT_ID: user_input[CONF_CLIENT_ID]}
            )
            try:
                await self._validate(user_input)
            except AuthError:
                errors["base"] = "invalid_auth"
            except (ApiError, PolicyError) as err:
                errors["base"] = policy_read_error(err)
            else:
                return self.async_create_entry(title="Tailscale OAuth", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=credentials_schema(), errors=errors
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            user_input = {
                CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET].strip(),
            }
            if any(
                other.entry_id != entry.entry_id
                and other.data.get(CONF_CLIENT_ID) == user_input[CONF_CLIENT_ID]
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            try:
                await self._validate(user_input)
            except AuthError:
                errors["base"] = "invalid_auth"
            except (ApiError, PolicyError) as err:
                errors["base"] = policy_read_error(err)
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **{
                            key: value
                            for key, value in entry.data.items()
                            if key != "tailnet"
                        },
                        **user_input,
                    },
                    unique_id=f"oauth:{user_input[CONF_CLIENT_ID]}",
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=credentials_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Edit a named app directly from Home Assistant."""

    async def async_step_init(self, user_input=None):
        if not hasattr(self.config_entry, "runtime_data"):
            return self.async_abort(reason="cannot_connect")
        try:
            snapshot = await self.config_entry.runtime_data.client.get_policy()
            names = []
            for name in snapshot.policy.connectors():
                try:
                    snapshot.policy.domains(name)
                except PolicyError:
                    continue
                names.append(name)
        except AuthError:
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="invalid_auth")
        except (ApiError, PolicyError):
            return self.async_abort(reason="cannot_connect")
        names = sorted(
            set(names)
            | {name for name, _ in remembered_pairs(self.hass, self.config_entry)}
        )
        if not names:
            return self.async_abort(reason="no_connectors")
        if user_input is not None:
            self._connector = user_input["connector"]
            self._action = user_input["action"]
            return await self.async_step_change()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("connector"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=sorted(names))
                    ),
                    vol.Required("action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["add", "remove", "rename"],
                            translation_key="domain_action",
                        )
                    ),
                }
            ),
        )

    async def async_step_change(self, user_input=None):
        coordinator = self.config_entry.runtime_data
        try:
            snapshot = await coordinator.client.get_policy()
            existing = (
                domain_bases(snapshot.policy.domains(self._connector))
                if self._connector in snapshot.policy.connectors()
                else set()
            )
            if self._action == "remove":
                existing.update(
                    domain
                    for connector, domain in remembered_pairs(
                        self.hass, self.config_entry
                    )
                    if connector == self._connector
                )
            existing = sorted(existing)
        except AuthError:
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="invalid_auth")
        except (ApiError, PolicyError):
            return self.async_abort(reason="cannot_connect")
        if self._action != "add" and not existing:
            return self.async_abort(reason="no_domains")
        errors = {}
        if user_input is not None:
            try:
                if self._action == "add":
                    add = [domain_base(user_input["domain"])]
                    remove = []
                elif self._action == "remove":
                    add = []
                    remove = [domain_base(user_input["existing_domain"])]
                else:
                    add = [domain_base(user_input["domain"])]
                    remove = [domain_base(user_input["existing_domain"])]
                if self._action == "remove":
                    await coordinator.async_delete_domains(self._connector, remove)
                else:

                    async def change():
                        await coordinator.client.change_domain_pairs(
                            self._connector, add, remove, require_existing=bool(remove)
                        )

                    await coordinator.async_write(change)
            except AuthError:
                self.config_entry.async_start_reauth(self.hass)
                errors["base"] = "invalid_auth"
            except PolicyError:
                errors["base"] = "invalid_domains"
            except HomeAssistantError as err:
                if isinstance(err.__cause__, PolicyError):
                    errors["base"] = "invalid_domains"
                elif isinstance(err.__cause__, AuthError):
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except ApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_abort(reason="updated")
        fields = {}
        if self._action != "add":
            fields[vol.Required("existing_domain")] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=existing)
            )
        if self._action != "remove":
            fields[vol.Required("domain")] = selector.TextSelector()
        return self.async_show_form(
            step_id="change",
            description_placeholders={"connector": self._connector},
            data_schema=vol.Schema(fields),
            errors=errors,
        )
