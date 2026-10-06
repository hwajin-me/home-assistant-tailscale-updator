"""Exercise HA flows, coordinator, entities, service permissions and lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    Unauthorized,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_api import policy

from custom_components.tailscale_updator import async_setup_entry, async_unload_entry
from custom_components.tailscale_updator.api import ApiError, AuthError, Snapshot
from custom_components.tailscale_updator.config_flow import ConfigFlow, OptionsFlow
from custom_components.tailscale_updator.const import DOMAIN
from custom_components.tailscale_updator.coordinator import PolicyCoordinator
from custom_components.tailscale_updator.services import async_setup_services
from custom_components.tailscale_updator.switch import DomainSwitch


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = MagicMock()
    yield instance
    await instance.async_block_till_done()
    await instance.async_stop(force=True)


@pytest.fixture
def entry():
    return MagicMock(
        entry_id="entry",
        domain=DOMAIN,
        title="example.com",
        data={"tailnet": "example.com", "client_id": "id", "client_secret": "secret"},
        options={},
        state=ConfigEntryState.LOADED,
    )


def make_coordinator(hass, entry):
    client = SimpleNamespace(
        write_lock=asyncio.Lock(),
        get_policy=AsyncMock(
            return_value=Snapshot(policy(["a.com", "*.a.com"]), '"1"')
        ),
        set_domains=AsyncMock(),
        change_domain_pairs=AsyncMock(),
        replace_policy=AsyncMock(),
    )
    coordinator = PolicyCoordinator(hass, entry, client)
    coordinator.async_set_updated_data(Snapshot(policy(["a.com", "*.a.com"]), '"1"'))
    entry.runtime_data = coordinator
    return coordinator


async def test_switch_tracks_remote_state_and_not_optimistic(hass, entry):
    coordinator = make_coordinator(hass, entry)
    switch = DomainSwitch(entry, "app", "a.com")
    assert switch.is_on
    coordinator.client.get_policy.return_value = Snapshot(policy([]), '"2"')
    await switch.async_turn_off()
    coordinator.client.set_domains.assert_awaited_once_with("app", ["a.com"], False)
    assert switch.is_on is False
    coordinator.client.set_domains.side_effect = ApiError("denied")
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    assert not switch.available
    assert switch.is_on is None


async def test_missing_connector_unavailable(hass, entry):
    coordinator = make_coordinator(hass, entry)
    switch = DomainSwitch(entry, "app", "a.com")
    coordinator.async_set_updated_data(Snapshot("{}", '"2"'))
    assert not switch.available


async def test_coordinator_auth_and_transport_errors(hass, entry):
    coordinator = make_coordinator(hass, entry)
    coordinator.client.get_policy.side_effect = AuthError("revoked")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    coordinator.client.get_policy.side_effect = ApiError("timeout")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write(AsyncMock(side_effect=AuthError("revoked")))
    entry.async_start_reauth.assert_called_once_with(hass)


async def test_user_flow_and_auth_errors(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    credentials = {
        "tailnet": "Example.COM",
        "client_id": "id",
        "client_secret": "secret",
    }
    with patch.object(flow, "_validate", AsyncMock()):
        result = await flow.async_step_user(credentials)
        assert result["type"] == "create_entry"
        assert result["data"]["tailnet"] == "example.com"
    with patch.object(flow, "_validate", AsyncMock(side_effect=AuthError())):
        result = await flow.async_step_user(credentials)
        assert result["errors"]["base"] == "invalid_auth"
    assert (await flow.async_step_user({**credentials, "tailnet": "-"}))["errors"][
        "base"
    ] == "invalid_tailnet"


async def test_reauth_preserves_tailnet(hass, entry):
    flow = ConfigFlow()
    flow.hass = hass
    flow._get_reauth_entry = MagicMock(return_value=entry)
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    with patch.object(flow, "_validate", AsyncMock()) as validate:
        await flow.async_step_reauth_confirm(
            {"client_id": "new", "client_secret": "new-secret"}
        )
        assert validate.call_args.args[0]["tailnet"] == "example.com"
        flow.async_update_reload_and_abort.assert_called_once_with(
            entry, data_updates={"client_id": "new", "client_secret": "new-secret"}
        )


async def test_options_change_acl_pairs_and_refresh(hass, entry):
    coordinator = make_coordinator(hass, entry)
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    result = await flow.async_step_init({"connector": "app", "action": "add"})
    assert result["step_id"] == "change"
    result = await flow.async_step_change({"domain": ".new.com"})
    assert result["type"] == "abort"
    assert result["reason"] == "updated"
    coordinator.client.change_domain_pairs.assert_awaited_once_with(
        "app", ["new.com"], [], require_existing=False
    )
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    await flow.async_step_init({"connector": "app", "action": "rename"})
    result = await flow.async_step_change(
        {"existing_domain": "a.com", "domain": "b.com"}
    )
    assert result["reason"] == "updated"
    coordinator.client.change_domain_pairs.assert_awaited_with(
        "app", ["b.com"], ["a.com"], require_existing=True
    )


async def test_admin_services_read_write_and_reject_unloaded(hass, entry):
    coordinator = make_coordinator(hass, entry)
    hass.config_entries.async_get_entry.return_value = entry
    async_setup_services(hass)
    result = await hass.services.async_call(
        DOMAIN, "get_acl", {"entry_id": "entry"}, blocking=True, return_response=True
    )
    assert result["etag"] == '"1"'
    await hass.services.async_call(
        DOMAIN,
        "add_domains",
        {"entry_id": "entry", "connector": "app", "domains": ["b.com"]},
        blocking=True,
    )
    coordinator.client.set_domains.assert_awaited_with("app", ["b.com"], True)
    entry.state = ConfigEntryState.NOT_LOADED
    with pytest.raises(HomeAssistantError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN,
            "get_acl",
            {"entry_id": "entry"},
            blocking=True,
            return_response=True,
        )


async def test_non_admin_cannot_read_policy(hass, entry):
    make_coordinator(hass, entry)
    hass.config_entries.async_get_entry.return_value = entry
    hass.auth = SimpleNamespace(
        async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
    )
    async_setup_services(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "get_acl",
            {"entry_id": "entry"},
            blocking=True,
            return_response=True,
            context=Context(user_id="non-admin"),
        )


async def test_setup_and_unload(hass, entry):
    entry.state = ConfigEntryState.SETUP_IN_PROGRESS
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    with (
        patch("custom_components.tailscale_updator.async_get_clientsession"),
        patch("custom_components.tailscale_updator.TailscaleClient") as factory,
    ):
        factory.return_value.write_lock = asyncio.Lock()
        factory.return_value.get_policy = AsyncMock(return_value=Snapshot("{}", '"1"'))
        assert await async_setup_entry(hass, entry)
        assert entry.runtime_data.data.text == "{}"
        assert hass.services.has_service(DOMAIN, "get_acl")
        assert await async_unload_entry(hass, entry)
        entry.add_update_listener.assert_called_once()
