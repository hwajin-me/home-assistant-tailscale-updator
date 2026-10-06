"""Configure OAuth credentials and app-domain switches from the UI."""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, ApiHttpError, AuthError, TailscaleClient
from .const import CONF_TAILNET, DOMAIN
from .policy import PolicyError, domain_base, domain_bases

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


def credentials_schema(reauth=False):
    fields = {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
    if not reauth:
        fields = {vol.Required(CONF_TAILNET, default="-"): str, **fields}
    return vol.Schema(fields)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, data):
        client = TailscaleClient(
            async_get_clientsession(self.hass),
            data[CONF_TAILNET],
            data[CONF_CLIENT_ID],
            data[CONF_CLIENT_SECRET],
        )
        await client.get_policy()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_TAILNET] = user_input.get(CONF_TAILNET, "-").strip().lower()
            if not user_input[CONF_TAILNET]:
                errors["base"] = "invalid_tailnet"
            else:
                unique_id = (
                    f"oauth:{user_input[CONF_CLIENT_ID]}"
                    if user_input[CONF_TAILNET] == "-"
                    else user_input[CONF_TAILNET]
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                try:
                    await self._validate(user_input)
                except AuthError:
                    errors["base"] = "invalid_auth"
                except (ApiError, PolicyError) as err:
                    errors["base"] = policy_read_error(err)
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_TAILNET], data=user_input
                    )
        return self.async_show_form(
            step_id="user", data_schema=credentials_schema(), errors=errors
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            try:
                await self._validate({**entry.data, **user_input})
            except AuthError:
                errors["base"] = "invalid_auth"
            except (ApiError, PolicyError) as err:
                errors["base"] = policy_read_error(err)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=credentials_schema(True),
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
        except (ApiError, PolicyError):
            return self.async_abort(reason="cannot_connect")
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
                            options=["add", "remove", "rename"]
                        )
                    ),
                }
            ),
        )

    async def async_step_change(self, user_input=None):
        coordinator = self.config_entry.runtime_data
        try:
            snapshot = await coordinator.client.get_policy()
            existing = sorted(domain_bases(snapshot.policy.domains(self._connector)))
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
                await coordinator.client.change_domain_pairs(
                    self._connector,
                    add,
                    remove,
                    require_existing=bool(remove),
                )
                await coordinator.async_refresh()
                if not coordinator.last_update_success:
                    raise ApiError("Policy was saved but could not be refreshed")
            except AuthError:
                self.config_entry.async_start_reauth(self.hass)
                errors["base"] = "invalid_auth"
            except PolicyError:
                errors["base"] = "invalid_domains"
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
