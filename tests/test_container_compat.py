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


async def test_container_end_to_end(tmp_path):
    domains = ["example.com"]
    writes = []
    version = 1
    token_calls = 0

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
                return_value=ThreadedResolver(),
            ),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "user"},
                data={
                    "tailnet": "-",
                    "client_id": "test-id",
                    "client_secret": "test-secret",
                },
            )
            assert result["type"] == "create_entry"
            entry = result["result"]
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            entries = er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            assert len(entries) == 1
            initial = entries[0]
            assert hass.states.get(initial.entity_id).state == "off"

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
            assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await hass.async_stop(force=True)
        await runner.cleanup()
