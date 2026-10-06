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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_api import policy

from custom_components.tailscale_updator import async_setup_entry, async_unload_entry
from custom_components.tailscale_updator.api import (
    ApiError,
    ApiHttpError,
    AuthError,
    PolicyResponseError,
    Snapshot,
)
from custom_components.tailscale_updator.config_flow import ConfigFlow, OptionsFlow
from custom_components.tailscale_updator.const import DOMAIN
from custom_components.tailscale_updator.coordinator import PolicyCoordinator
from custom_components.tailscale_updator.policy import PolicyError
from custom_components.tailscale_updator.services import async_setup_services
from custom_components.tailscale_updator.switch import DomainSwitch


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = MagicMock()
    await er.async_load(instance)
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
    credentials = {"client_id": "id", "client_secret": "secret"}
    form = await flow.async_step_user()
    assert {str(key) for key in form["data_schema"].schema} == {
        "client_id",
        "client_secret",
    }
    with patch.object(flow, "_validate", AsyncMock()):
        result = await flow.async_step_user(credentials)
        assert result["type"] == "create_entry"
        assert result["data"] == credentials
        assert result["title"] == "Tailscale OAuth"
        flow.async_set_unique_id.assert_awaited_with("oauth:id")
    with patch.object(flow, "_validate", AsyncMock(side_effect=AuthError())):
        result = await flow.async_step_user(credentials)
        assert result["errors"]["base"] == "invalid_auth"


async def test_validation_always_uses_oauth_tailnet(hass):
    flow = ConfigFlow()
    flow.hass = hass
    with (
        patch(
            "custom_components.tailscale_updator.config_flow.async_get_clientsession"
        ),
        patch(
            "custom_components.tailscale_updator.config_flow.TailscaleClient"
        ) as factory,
    ):
        factory.return_value.get_policy = AsyncMock()
        await flow._validate(
            {
                "tailnet": "old-name.example",
                "client_id": "id",
                "client_secret": "secret",
            }
        )
        assert factory.call_args.args[1:] == ("-", "id", "secret")
        factory.return_value.get_policy.assert_awaited_once()


@pytest.mark.parametrize(
    "error,expected",
    [
        (ApiHttpError(403), "policy_forbidden"),
        (ApiHttpError(404), "tailnet_not_found"),
        (ApiHttpError(429), "api_error"),
        (PolicyError("bad HuJSON"), "invalid_policy"),
        (PolicyResponseError("bad response"), "invalid_policy"),
        (ApiError("timeout"), "cannot_connect"),
    ],
)
async def test_user_flow_reports_policy_read_failure(hass, error, expected):
    flow = ConfigFlow()
    flow.hass = hass
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    with patch.object(flow, "_validate", AsyncMock(side_effect=error)):
        result = await flow.async_step_user(
            {"client_id": "id", "client_secret": "secret"}
        )
    assert result["errors"]["base"] == expected


async def test_reauth_updates_client_identity_and_removes_legacy_tailnet(hass, entry):
    flow = ConfigFlow()
    flow.hass = hass
    flow._get_reauth_entry = MagicMock(return_value=entry)
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
    with patch.object(flow, "_validate", AsyncMock()) as validate:
        await flow.async_step_reauth_confirm(
            {"client_id": " new ", "client_secret": " new-secret "}
        )
        assert validate.call_args.args[0] == {
            "client_id": "new",
            "client_secret": "new-secret",
        }
        flow.async_update_reload_and_abort.assert_called_once_with(
            entry,
            data={"client_id": "new", "client_secret": "new-secret"},
            unique_id="oauth:new",
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
        assert factory.call_args.args[1:] == ("-", "id", "secret")
        assert entry.runtime_data.data.text == "{}"
        assert hass.services.has_service(DOMAIN, "get_acl")
        assert await async_unload_entry(hass, entry)
        entry.add_update_listener.assert_called_once()


async def test_reauth_rejects_client_already_used_by_another_entry(hass, entry):
    flow = ConfigFlow()
    flow.hass = hass
    flow._get_reauth_entry = MagicMock(return_value=entry)
    flow._async_current_entries = MagicMock(
        return_value=[
            entry,
            SimpleNamespace(entry_id="another", data={"client_id": "other"}),
        ]
    )
    with patch.object(flow, "_validate", AsyncMock()) as validate:
        result = await flow.async_step_reauth_confirm(
            {"client_id": "other", "client_secret": "secret"}
        )
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()


@pytest.mark.parametrize("step", ["domains", "change"])
async def test_options_policy_read_auth_failure_starts_reauth(hass, entry, step):
    coordinator = make_coordinator(hass, entry)
    coordinator.client.get_policy.side_effect = AuthError("revoked")
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    result = await getattr(flow, f"async_step_{step}")()
    assert result["reason"] == "invalid_auth"
    entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.parametrize(
    "value",
    [{"app": "a.com", "other": "b.com"}, "ab", ["", "a.com"], ["app"], ["app", 1]],
)
def test_invalid_registered_pair_is_ignored(value):
    import json

    from custom_components.tailscale_updator.switch import registered_pair

    assert registered_pair("entry", f"entry:{json.dumps(value)}") is None


async def test_group_toggle_reads_membership_after_waiting_for_edit(hass, entry):
    coordinator = make_coordinator(hass, entry)
    coordinator.client.set_domain_group = AsyncMock()
    entry.options = {
        "domain_groups": {
            "group": {
                "name": "Media",
                "members": [
                    {"connector": "app", "domain": "a.com"},
                    {"connector": "app", "domain": "b.com"},
                ],
            }
        }
    }
    await coordinator.edit_lock.acquire()
    task = asyncio.create_task(coordinator.async_set_domain_group("group", True))
    await asyncio.sleep(0)
    entry.options = {
        "domain_groups": {
            "group": {
                "name": "Media",
                "members": [{"connector": "app", "domain": "b.com"}],
            }
        }
    }
    coordinator.edit_lock.release()
    await task
    coordinator.client.set_domain_group.assert_awaited_once_with(
        [{"connector": "app", "domain": "b.com"}], True
    )


async def test_invalid_local_edit_does_not_mark_healthy_entities_unavailable(
    hass, entry
):
    coordinator = make_coordinator(hass, entry)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write(
            AsyncMock(side_effect=PolicyError("invalid input"))
        )
    assert coordinator.last_update_success


async def test_editing_deleted_group_does_not_recreate_it(hass, entry):
    make_coordinator(hass, entry)
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    flow._group_id = "deleted-group"
    result = await flow.async_step_group_details(
        {"name": "Old editor", "members": ['["app", "a.com"]']}
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_group"
    hass.config_entries.async_update_entry.assert_not_called()


async def test_domain_rename_updates_group_membership(hass, entry):
    make_coordinator(hass, entry)
    entry.options = {
        "domain_groups": {
            "media": {
                "name": "Media",
                "members": [{"connector": "app", "domain": "a.com"}],
            }
        }
    }
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    await flow.async_step_domains({"connector": "app", "action": "rename"})
    result = await flow.async_step_change(
        {"existing_domain": "a.com", "domain": "b.com"}
    )
    assert result["reason"] == "updated"
    options = hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert options["domain_groups"]["media"]["members"] == [
        {"connector": "app", "domain": "b.com"}
    ]


async def test_stale_group_editor_cannot_overwrite_new_membership(hass, entry):
    make_coordinator(hass, entry)
    entry.options = {
        "domain_groups": {
            "media": {
                "name": "Media",
                "members": [{"connector": "app", "domain": "a.com"}],
            }
        }
    }
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    await flow.async_step_group_edit({"group_id": "media"})
    entry.options = {
        "domain_groups": {
            "media": {
                "name": "Changed elsewhere",
                "members": [{"connector": "app", "domain": "a.com"}],
            }
        }
    }
    result = await flow.async_step_group_details(
        {"name": "Old editor", "members": ['["app", "a.com"]']}
    )
    assert result["errors"]["base"] == "invalid_group"
    hass.config_entries.async_update_entry.assert_not_called()


async def test_scoped_policy_deletion_forgets_entities_and_group_members(hass, entry):
    from custom_components.tailscale_updator.registry import unique_id

    coordinator = make_coordinator(hass, entry)
    entry.options = {
        "domain_groups": {
            "media": {
                "name": "Media",
                "members": [{"connector": "app", "domain": "a.com"}],
            }
        }
    }
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "switch", DOMAIN, unique_id(entry.entry_id, "app", "a.com"), config_entry=entry
    )
    coordinator.client.get_policy.side_effect = [
        Snapshot(policy(["a.com", "*.a.com"]), '"1"'),
        Snapshot(policy([]), '"2"'),
    ]
    await coordinator.async_replace_connectors(policy([]), '"1"')
    assert registry.async_get(entity.entity_id) is None
    options = hass.config_entries.async_update_entry.call_args.kwargs["options"]
    assert options["domain_groups"]["media"]["members"] == []


async def test_group_delete_when_integration_unloaded_is_handled(hass, entry):
    del entry.runtime_data
    flow = OptionsFlow()
    flow.hass = hass
    flow._config_entry = entry
    result = await flow.async_step_group_delete({"group_id": "media"})
    assert result["reason"] == "cannot_connect"


async def test_scoped_policy_edit_preserves_previously_absent_domains(hass, entry):
    from custom_components.tailscale_updator.registry import unique_id

    coordinator = make_coordinator(hass, entry)
    registry = er.async_get(hass)
    saved = [
        registry.async_get_or_create(
            "switch",
            DOMAIN,
            unique_id(entry.entry_id, connector, "off.com"),
            config_entry=entry,
        )
        for connector in ("app", "previously-removed-app")
    ]
    coordinator.client.get_policy.return_value = Snapshot(policy([]), '"1"')
    await coordinator.async_replace_connectors(policy([]), '"1"')
    assert all(registry.async_get(entity.entity_id) is not None for entity in saved)
    hass.config_entries.async_update_entry.assert_not_called()
