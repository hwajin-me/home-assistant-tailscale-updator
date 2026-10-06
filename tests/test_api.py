"""OAuth renewal, failures and collision-safe policy writes with mocked HTTP."""

import asyncio
import json
from unittest.mock import patch

import pytest
from aiohttp import ClientSession
from aioresponses import aioresponses

from custom_components.tailscale_updator.api import (
    ApiError,
    ApiHttpError,
    AuthError,
    ConflictError,
    TailscaleClient,
)
from custom_components.tailscale_updator.const import API_BASE

TOKEN = f"{API_BASE}/oauth/token"
ACL = f"{API_BASE}/tailnet/example.com/acl"


def policy(domains):
    return json.dumps(
        {
            "nodeAttrs": [
                {
                    "app": {
                        "tailscale.com/app-connectors": [
                            {"name": "app", "domains": domains}
                        ]
                    }
                }
            ]
        }
    )


@pytest.fixture
async def client():
    async with ClientSession() as session:
        yield TailscaleClient(session, "example.com", "client-id", "secret")


def token(mock, value="token"):
    mock.post(TOKEN, payload={"access_token": value, "expires_in": 3600})


async def test_token_reuse_and_expiry(client):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body="{}", headers={"ETag": '"a"'}, repeat=True)
        with patch(
            "custom_components.tailscale_updator.api.time.monotonic", return_value=100
        ):
            await asyncio.gather(client.get_policy(), client.get_policy())
        assert len([k for k in mock.requests if k[0] == "POST"]) == 1
        token(mock, "renewed")
        with patch(
            "custom_components.tailscale_updator.api.time.monotonic", return_value=3700
        ):
            await client.get_policy()
        assert (
            len(
                mock.requests[
                    ("POST", next(k[1] for k in mock.requests if str(k[1]) == TOKEN))
                ]
            )
            == 2
        )


async def test_401_renews_once(client):
    with aioresponses() as mock:
        token(mock)
        token(mock, "renewed")
        mock.get(ACL, status=401)
        mock.get(ACL, body="{}")
        await client.get_policy()
        assert client._token == "renewed"


async def test_repeated_401_requires_reauth(client):
    with aioresponses() as mock:
        token(mock)
        token(mock)
        mock.get(ACL, status=401, repeat=True)
        with pytest.raises(AuthError):
            await client.get_policy()


@pytest.mark.parametrize("status", [403, 404, 429, 500])
async def test_policy_read_preserves_http_status(client, status):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, status=status)
        with pytest.raises(ApiHttpError) as raised:
            await client.get_policy()
        assert raised.value.status == status


@pytest.mark.parametrize(
    "status, error", [(401, AuthError), (403, AuthError), (500, ApiError)]
)
async def test_token_failures(client, status, error):
    with aioresponses() as mock:
        mock.post(TOKEN, status=status)
        with pytest.raises(error):
            await client.get_policy()


async def test_conflict_rebases_only_target_domains(client):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body=policy([]), headers={"ETag": '"1"'})
        mock.post(ACL, status=412)
        mock.get(ACL, body=policy(["external.com"]), headers={"ETag": '"2"'})
        mock.post(ACL, body="{}")
        mock.get(
            ACL,
            body=policy(["external.com", "new.com", "*.new.com"]),
            headers={"ETag": '"3"'},
        )
        result = await client.set_domains("app", ["new.com"], True)
        assert result.policy.domains("app") == ["external.com", "new.com", "*.new.com"]
        calls = next(
            v for k, v in mock.requests.items() if k[0] == "POST" and str(k[1]) == ACL
        )
        assert calls[0].kwargs["headers"]["If-Match"] == '"1"'
        assert calls[1].kwargs["headers"]["If-Match"] == '"2"'
        assert json.loads(calls[1].kwargs["data"])["nodeAttrs"][0]["app"][
            "tailscale.com/app-connectors"
        ][0]["domains"] == ["external.com", "new.com", "*.new.com"]


async def test_missing_etag_prevents_write(client):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body=policy([]))
        with pytest.raises(ApiError, match="ETag"):
            await client.set_domains("app", ["new.com"], True)
        assert all(str(k[1]) != ACL or k[0] == "GET" for k in mock.requests)


async def test_whole_policy_conflict_is_not_retried(client):
    with aioresponses() as mock:
        token(mock)
        mock.post(ACL, status=412)
        with pytest.raises(ConflictError):
            await client.replace_policy("{}", '"old"')
        assert len(next(v for k, v in mock.requests.items() if str(k[1]) == ACL)) == 1


async def test_idempotent_update_does_not_post(client):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body=policy(["new.com", "*.new.com"]))
        await client.set_domains("app", ["new.com"], True)
        assert all(str(k[1]) != ACL or k[0] == "GET" for k in mock.requests)


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_failed_write_never_reports_success(client, status):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body=policy([]), headers={"ETag": '"1"'})
        mock.post(ACL, status=status)
        with pytest.raises(ApiError):
            await client.set_domains("app", ["new.com"], True)


async def test_conflicts_are_bounded(client):
    with aioresponses() as mock:
        token(mock)
        mock.get(ACL, body=policy([]), headers={"ETag": '"1"'}, repeat=True)
        mock.post(ACL, status=412, repeat=True)
        with pytest.raises(ConflictError):
            await client.set_domains("app", ["new.com"], True)
        assert (
            len(
                next(
                    v
                    for k, v in mock.requests.items()
                    if k[0] == "POST" and str(k[1]) == ACL
                )
            )
            == 3
        )


async def test_concurrent_domain_updates_serialize(client):
    with aioresponses() as mock:
        token(mock)
        for domains in (
            [],
            ["a.com", "*.a.com"],
            ["a.com", "*.a.com"],
            ["a.com", "*.a.com", "b.com", "*.b.com"],
        ):
            mock.get(ACL, body=policy(domains), headers={"ETag": '"1"'})
        mock.post(ACL, body="{}", repeat=True)
        first, second = await asyncio.gather(
            client.set_domains("app", ["a.com"], True),
            client.set_domains("app", ["b.com"], True),
        )
        assert first.policy.domains("app") == ["a.com", "*.a.com"]
        assert second.policy.domains("app") == ["a.com", "*.a.com", "b.com", "*.b.com"]


async def test_wildcard_etag_rejected_before_network(client):
    with pytest.raises(ApiError):
        await client.replace_policy("{}", "*")
