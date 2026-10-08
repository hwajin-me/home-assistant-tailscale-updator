"""Real HA managers and HTTP against a local fake Tailscale control API.

This file intentionally does not use aioresponses; recent Home Assistant
releases can upgrade aiohttp before that test helper catches up.
"""

import json
from importlib.metadata import version as package_version
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.resolver import ThreadedResolver
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.tailscale_updator.api import ConflictError
from custom_components.tailscale_updator.const import DOMAIN


class CompatDNSResolver(ThreadedResolver):
    """Support both old session cleanup and current HA resolver cleanup."""

    async def real_close(self):
        await super().close()


async def test_container_end_to_end(tmp_path):
    domains = ["example.com", "EXAMPLE.COM.", ".example.com"]
    writes = []
    version = 1
    token_calls = 0
    reject_writes = False

    async def token(request):
        nonlocal token_calls
        token_calls += 1
        form = await request.post()
        assert form["grant_type"] == "client_credentials"
        assert form["client_id"] == "test-id"
        assert form["client_secret"] == "test-secret"
        return web.json_response({"access_token": "test-token", "expires_in": 3600})

    async def acl(request):
        nonlocal version
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "POST" and reject_writes:
            return web.Response(status=403)
        if request.method == "POST":
            assert request.headers["Content-Type"] == "application/hujson"
            if request.headers.get("If-Match") != f'"{version}"':
                return web.Response(status=412)
            posted = json.loads(await request.text())
            domains[:] = posted["nodeAttrs"][0]["app"]["tailscale.com/app-connectors"][
                0
            ]["domains"]
            writes.append((request.headers["If-Match"], list(domains)))
            version += 1
        policy = {
            "nodeAttrs": [
                {
                    "app": {
                        "tailscale.com/app-connectors": [
                            {"name": "app", "domains": list(domains)}
                        ]
                    }
                }
            ]
        }
        return web.Response(
            text=json.dumps(policy),
            content_type="application/hujson",
            headers={"ETag": f'"{version}"'},
        )

    application = web.Application()

    async def devices(request):
        return web.json_response(
            {"devices": [{"name": "node.my-tail.ts.net", "isExternal": False}]}
        )

    application.router.add_get("/api/v2/tailnet/-/devices", devices)
    application.router.add_post("/api/v2/oauth/token", token)
    application.router.add_route("*", "/api/v2/tailnet/-/acl", acl)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    (tmp_path / "custom_components").symlink_to(
        Path("custom_components").resolve(), target_is_directory=True
    )
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    if int(package_version("homeassistant").split(".")[0]) >= 2026:
        dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    try:
        with (
            patch(
                "custom_components.tailscale_updator.api.API_BASE",
                f"http://127.0.0.1:{port}/api/v2",
            ),
            patch(
                "homeassistant.helpers.aiohttp_client._async_make_resolver",
                return_value=CompatDNSResolver(),
            ),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "user"},
                data={
                    "client_id": "test-id",
                    "client_secret": "test-secret",
                },
            )
            assert result["step_id"] == "initial_groups"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "initial_group"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "name": "Initial group",
                    "members": [json.dumps(["app", "example.com"])],
                },
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "finish"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            assert result["type"] == "create_entry"
            entry = result["result"]
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            assert entry.title == "my-tail.ts.net"
            devices = dr.async_get(hass)
            initial_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
            assert len(initial_devices) == 3
            parent = next(
                device
                for device in initial_devices
                if (DOMAIN, entry.entry_id) in device.identifiers
            )
            assert parent.name == entry.title
            domain_device = next(
                device
                for device in initial_devices
                if device.name == "Tailscale Domain"
            )
            initial_group_device = next(
                device
                for device in initial_devices
                if device.name == "Group - Initial group"
            )
            assert (
                domain_device.via_device_id
                == initial_group_device.via_device_id
                == parent.id
            )
            assert domains == ["example.com", "*.example.com"]
            initial_groups = entry.options["domain_groups"]
            assert len(initial_groups) == 1
            group_entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            group_entity = next(
                entity for entity in group_entities if ":group:" in entity.unique_id
            )
            assert group_entity.device_id == initial_group_device.id
            assert all(
                entity.device_id == domain_device.id
                for entity in group_entities
                if ":group:" not in entity.unique_id
            )
            assert hass.states.get(group_entity.entity_id).state == "on"
            # Upgrade a legacy entry where every switch shared the tailnet device.
            # Disabled entities and user names must migrate without new entity IDs.
            registry = er.async_get(hass)
            assert await hass.config_entries.async_unload(entry.entry_id)
            identities = {
                entity.entity_id: entity.unique_id for entity in group_entities
            }
            for entity in group_entities:
                registry.async_update_entity(
                    entity.entity_id, device_id=parent.id, name="My saved name"
                )
            registry.async_update_entity(
                group_entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            for entity_id, entity_unique_id in identities.items():
                migrated = registry.async_get(entity_id)
                assert migrated.unique_id == entity_unique_id
                assert migrated.name == "My saved name"
                assert migrated.device_id == (
                    initial_group_device.id
                    if ":group:" in entity_unique_id
                    else domain_device.id
                )
            assert (
                registry.async_get(group_entity.entity_id).disabled_by
                == er.RegistryEntryDisabler.USER
            )
            registry.async_update_entity(group_entity.entity_id, disabled_by=None)
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert hass.states.get(group_entity.entity_id).state == "on"
            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "group_delete"}
            )
            await hass.config_entries.options.async_configure(
                options["flow_id"], {"group_id": next(iter(initial_groups))}
            )
            await hass.async_block_till_done()
            assert devices.async_get(initial_group_device.id) is None
            entries = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entries) == 1
            initial = entries[0]
            assert hass.states.get(initial.entity_id).state == "on"

            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": initial.entity_id}, blocking=True
            )
            assert domains == ["example.com", "*.example.com"]
            assert hass.states.get(initial.entity_id).state == "on"
            assert writes[0][0] == '"1"'
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": initial.entity_id}, blocking=True
            )
            assert domains == []
            assert hass.states.get(initial.entity_id).state == "off"

            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "domains"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "add"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"domain": "added.com"}
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert domains == ["added.com", "*.added.com"]
            entries = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entries) == 2
            added = next(e for e in entries if "added.com" in e.unique_id)
            assert hass.states.get(added.entity_id).state == "on"

            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "domains"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "rename"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"],
                {"existing_domain": "added.com", "domain": "renamed.com"},
            )
            assert options["reason"] == "updated"
            assert domains == ["renamed.com", "*.renamed.com"]
            await hass.async_block_till_done()
            assert hass.states.get(added.entity_id).state == "off"

            with pytest.raises(ConflictError):
                await entry.runtime_data.client.replace_policy("{}", '"1"')
            assert domains == ["renamed.com", "*.renamed.com"]

            entry.runtime_data.client._expires = 0
            await entry.runtime_data.client.get_policy()
            assert token_calls >= 3

            domains[:] = [*domains, "external.com", "*.external.com"]
            version += 1
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            entries = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entries) == 4
            external = next(e for e in entries if "external.com" in e.unique_id)
            assert hass.states.get(external.entity_id).state == "on"
            # Removed entities can be rediscovered without reloading the integration.
            registry = er.async_get(hass)
            registry.async_remove(external.entity_id)
            await hass.async_block_till_done()
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(external.entity_id).state == "on"
            assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 4
            assert await hass.config_entries.async_unload(entry.entry_id)
            # Collapse parent/wildcard/case aliases while retaining other platforms.
            aliases = []
            for domain in ("*.external.com", ".EXTERNAL.COM."):
                aliases.append(
                    registry.async_get_or_create(
                        "switch",
                        DOMAIN,
                        f"{entry.entry_id}:{json.dumps(['app', domain])}",
                        config_entry=entry,
                    )
                )
            sensor = registry.async_get_or_create(
                "sensor",
                DOMAIN,
                f"{entry.entry_id}:{json.dumps(['app', '*.external.com'])}",
                config_entry=entry,
            )
            registry.async_update_entity(external.entity_id, name="Keep my name")
            hass.config_entries.async_update_entry(
                entry,
                options={
                    "switches": [
                        None,
                        {},
                        {"connector": "app", "domain": "https://bad"},
                        {"connector": "app", "domain": "*.EXTERNAL.COM"},
                    ]
                },
            )
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            entries = er.async_entries_for_config_entry(registry, entry.entry_id)
            assert len([item for item in entries if item.domain == "switch"]) == 4
            assert registry.async_get(external.entity_id).name == "Keep my name"
            assert registry.async_get(sensor.entity_id) is not None
            assert all(registry.async_get(item.entity_id) is None for item in aliases)
            for _ in range(3):
                await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 5
            # Failed explicit deletion must keep the entity and its remote domains.
            from homeassistant.exceptions import HomeAssistantError

            reject_writes = True
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    DOMAIN,
                    "remove_domains",
                    {
                        "entry_id": entry.entry_id,
                        "connector": "app",
                        "domains": ["external.com"],
                    },
                    blocking=True,
                )
            assert registry.async_get(external.entity_id) is not None
            assert "external.com" in domains
            reject_writes = False
            # An external ACL deletion keeps the same switch, now Off.
            domains[:] = []
            version += 1
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(external.entity_id).state == "off"
            # Explicit Delete can select an Off domain missing from the ACL.
            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "domains"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "remove"}
            )
            assert options["step_id"] == "change"
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"existing_domain": "external.com"}
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert registry.async_get(external.entity_id) is None
            assert hass.states.get(external.entity_id) is None
            assert {
                "connector": "app",
                "domain": "*.EXTERNAL.COM",
            } not in entry.options["switches"]
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            assert registry.async_get(external.entity_id) is None
            assert hass.states.get(initial.entity_id).state == "off"
            assert hass.states.get(added.entity_id).state == "off"
            # A later explicit Add discovers exactly one switch again.
            await hass.services.async_call(
                DOMAIN,
                "add_domains",
                {
                    "entry_id": entry.entry_id,
                    "connector": "app",
                    "domains": ["external.com"],
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            assert hass.states.get(external.entity_id).state == "on"
            # Create a local group through the real options flow, including an Off domain.
            import json as group_json

            previous_writes = len(writes)
            options = await hass.config_entries.options.async_init(entry.entry_id)
            assert options["type"] == "menu"
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "group_add"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"],
                {
                    "name": "Streaming",
                    "members": [
                        group_json.dumps(["app", "external.com"]),
                        group_json.dumps(["app", "added.com"]),
                    ],
                },
            )
            assert options["reason"] == "group_updated"
            await hass.async_block_till_done()
            assert len(writes) == previous_writes
            group_id = next(iter(entry.options["domain_groups"]))
            group_entity = next(
                e
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if ":group:" in e.unique_id
            )
            group_device_id = group_entity.device_id
            assert group_device_id != external.device_id
            assert devices.async_get(group_device_id).via_device_id == parent.id
            assert hass.states.get(group_entity.entity_id).state == "off"
            assert hass.states.get(group_entity.entity_id).attributes["partially_on"]
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": group_entity.entity_id},
                blocking=True,
            )
            await hass.async_block_till_done()
            assert len(writes) == previous_writes + 1
            assert set(domains) == {
                "external.com",
                "*.external.com",
                "added.com",
                "*.added.com",
            }
            assert hass.states.get(group_entity.entity_id).state == "on"
            await hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": group_entity.entity_id},
                blocking=True,
            )
            await hass.async_block_till_done()
            assert domains == []
            assert hass.states.get(external.entity_id).state == "off"
            assert hass.states.get(added.entity_id).state == "off"
            assert hass.states.get(group_entity.entity_id).state == "off"
            # Editing local name/membership retains identity and makes no policy write.
            previous_writes = len(writes)
            await hass.services.async_call(
                DOMAIN,
                "set_domain_group",
                {
                    "entry_id": entry.entry_id,
                    "group_id": group_id,
                    "name": "Media",
                    "members": [
                        {"connector": "app", "domain": "external.com"},
                        {"connector": "app", "domain": "added.com"},
                    ],
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            assert len(writes) == previous_writes
            assert (
                registry.async_get(group_entity.entity_id).unique_id
                == group_entity.unique_id
            )
            assert (
                registry.async_get(group_entity.entity_id).device_id == group_device_id
            )
            assert devices.async_get(group_device_id).name == "Group - Media"
            # Explicit deletion also removes group membership, so group On cannot resurrect it.
            await hass.services.async_call(
                DOMAIN,
                "remove_domains",
                {
                    "entry_id": entry.entry_id,
                    "connector": "app",
                    "domains": ["external.com"],
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            assert entry.options["domain_groups"][group_id]["members"] == [
                {"connector": "app", "domain": "added.com"}
            ]
            await hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": group_entity.entity_id},
                blocking=True,
            )
            await hass.async_block_till_done()
            assert set(domains) == {"added.com", "*.added.com"}
            assert registry.async_get(external.entity_id) is None
            previous_writes = len(writes)
            await hass.services.async_call(
                DOMAIN,
                "delete_domain_group",
                {
                    "entry_id": entry.entry_id,
                    "group_id": group_id,
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            assert len(writes) == previous_writes
            assert registry.async_get(group_entity.entity_id) is None
            assert devices.async_get(group_device_id) is None
            assert hass.states.get(added.entity_id).state == "on"
            # Renaming an active domain migrates group membership instead of resurrecting the old name.
            await hass.services.async_call(
                DOMAIN,
                "set_domain_group",
                {
                    "entry_id": entry.entry_id,
                    "group_id": "rename-check",
                    "name": "Rename check",
                    "members": [{"connector": "app", "domain": "added.com"}],
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"next_step_id": "domains"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "rename"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"],
                {"existing_domain": "added.com", "domain": "updated.com"},
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert entry.options["domain_groups"]["rename-check"]["members"] == [
                {"connector": "app", "domain": "updated.com"}
            ]
            updated_entity = next(
                e
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if "updated.com" in e.unique_id
            )
            # Explicit deletion through scoped set_acl also forgets only the newly deleted domain.
            current = await hass.services.async_call(
                DOMAIN,
                "get_acl",
                {"entry_id": entry.entry_id},
                blocking=True,
                return_response=True,
            )
            document = json.loads(current["policy"])
            document["nodeAttrs"][0]["app"]["tailscale.com/app-connectors"][0][
                "domains"
            ] = []
            await hass.services.async_call(
                DOMAIN,
                "set_acl",
                {
                    "entry_id": entry.entry_id,
                    "policy": json.dumps(document),
                    "expected_etag": current["etag"],
                },
                blocking=True,
            )
            await hass.async_block_till_done()
            assert registry.async_get(updated_entity.entity_id) is None
            assert entry.options["domain_groups"]["rename-check"]["members"] == []
            assert hass.states.get(added.entity_id).state == "off"
            assert hass.states.get(initial.entity_id).state == "off"
            assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await hass.async_stop(force=True)
        await runner.cleanup()


async def test_bundled_brand_images_are_served_by_home_assistant(tmp_path):
    brands = pytest.importorskip("homeassistant.components.brands")
    (tmp_path / "custom_components").symlink_to(
        Path("custom_components").resolve(), target_is_directory=True
    )
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    try:
        view = brands.BrandsIntegrationView(hass)
        for path in Path("custom_components/tailscale_updator/brand").glob("*.png"):
            expected = path.read_bytes()
            response = await view._serve_from_custom_integration(DOMAIN, path.name)
            assert response is not None
            assert response.content_type == "image/png"
            assert response.body == expected
    finally:
        await hass.async_stop(force=True)
