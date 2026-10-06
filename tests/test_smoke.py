"""Load the custom integration through real Home Assistant managers."""

import json
from pathlib import Path
from unittest.mock import patch

from aiohttp.resolver import ThreadedResolver
from aioresponses import CallbackResult, aioresponses
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_api import policy, token

from custom_components.tailscale_updator.const import API_BASE, DOMAIN

ACL = f"{API_BASE}/tailnet/-/acl"


async def test_real_flow_setup_options_switch_and_unload(tmp_path):
    (tmp_path / "custom_components").symlink_to(
        Path("custom_components").resolve(), target_is_directory=True
    )
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await er.async_load(hass)
    await dr.async_load(hass)
    try:
        with (
            aioresponses() as mock,
            patch(
                "homeassistant.helpers.aiohttp_client._async_make_resolver",
                return_value=ThreadedResolver(),
            ),
        ):
            token(mock)
            token(mock)
            server_domains = ["a.com"]

            def read_policy(url, **kwargs):
                return CallbackResult(
                    body=policy(server_domains), headers={"ETag": '"1"'}
                )

            def write_policy(url, **kwargs):
                data = json.loads(kwargs["data"])
                server_domains[:] = data["nodeAttrs"][0]["app"][
                    "tailscale.com/app-connectors"
                ][0]["domains"]
                return CallbackResult(body=policy(server_domains))

            mock.get(ACL, callback=read_policy, repeat=True)
            mock.post(ACL, callback=write_policy, repeat=True)
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "user"},
                data={
                    "client_id": "id",
                    "client_secret": "secret",
                },
            )
            assert result["type"] == "create_entry"
            entry = result["result"]
            await hass.async_block_till_done()
            assert entry.state == ConfigEntryState.LOADED
            # A single pre-existing parent domain is discovered automatically.
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 1
            initial = entities[0]
            assert hass.states.get(initial.entity_id).state == "off"
            assert hass.states.get(initial.entity_id).attributes["parent_present"]
            assert not hass.states.get(initial.entity_id).attributes["wildcard_present"]
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": initial.entity_id}, blocking=True
            )
            assert server_domains == ["a.com", "*.a.com"]
            assert hass.states.get(initial.entity_id).state == "on"
            await hass.services.async_call(
                "switch", "turn_off", {"entity_id": initial.entity_id}, blocking=True
            )
            assert server_domains == []
            assert hass.states.get(initial.entity_id).state == "off"

            # Add, rename and remove domain pairs in the HA Configure flow.
            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "add"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"domain": "b.com"}
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert server_domains == ["b.com", "*.b.com"]
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 2
            b_entity = next(e for e in entities if "b.com" in e.unique_id)
            assert hass.states.get(b_entity.entity_id).state == "on"

            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "rename"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"existing_domain": "b.com", "domain": "c.com"}
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert server_domains == ["c.com", "*.c.com"]
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 3
            c_entity = next(e for e in entities if "c.com" in e.unique_id)
            assert hass.states.get(c_entity.entity_id).state == "on"

            options = await hass.config_entries.options.async_init(entry.entry_id)
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"connector": "app", "action": "remove"}
            )
            options = await hass.config_entries.options.async_configure(
                options["flow_id"], {"existing_domain": "c.com"}
            )
            assert options["reason"] == "updated"
            await hass.async_block_till_done()
            assert server_domains == []
            assert hass.states.get(c_entity.entity_id) is None
            assert er.async_get(hass).async_get(c_entity.entity_id) is None
            # A legacy wildcard-only registry entry is folded into its pair.
            assert await hass.config_entries.async_unload(entry.entry_id)
            legacy = er.async_get(hass).async_get_or_create(
                "switch",
                DOMAIN,
                f"{entry.entry_id}:{json.dumps(['app', '*.a.com'])}",
                config_entry=entry,
            )
            token(mock)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 2
            assert er.async_get(hass).async_get(legacy.entity_id) is None
            assert all(hass.states.get(e.entity_id).state == "off" for e in entities)
            # An external ACL edit is discovered without reconfiguring HA.
            server_domains[:] = ["external.com", "*.external.com"]
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done()
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 3
            external = next(e for e in entities if "external.com" in e.unique_id)
            assert hass.states.get(external.entity_id).state == "on"
            # HA actions use the same pair semantics and update discovered entities.
            await hass.services.async_call(
                DOMAIN,
                "add_domains",
                {
                    "entry_id": entry.entry_id,
                    "connector": "app",
                    "domains": ["service.com"],
                },
                blocking=True,
            )
            assert "service.com" in server_domains
            assert "*.service.com" in server_domains
            await hass.async_block_till_done()
            entities = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entities) == 4
            result = await hass.services.async_call(
                DOMAIN,
                "get_acl",
                {"entry_id": entry.entry_id},
                blocking=True,
                return_response=True,
            )
            assert result["etag"] == '"1"'
            assert '"*.service.com"' in result["policy"]
            await hass.services.async_call(
                DOMAIN,
                "remove_domains",
                {
                    "entry_id": entry.entry_id,
                    "connector": "app",
                    "domains": ["service.com"],
                },
                blocking=True,
            )
            assert "service.com" not in server_domains
            assert "*.service.com" not in server_domains
            assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await hass.async_stop(force=True)
