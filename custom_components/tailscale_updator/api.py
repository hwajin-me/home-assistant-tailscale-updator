"""OAuth client credentials and conditional Tailscale policy writes."""

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import API_BASE
from .policy import Policy, PolicyError, domain_base, domain_bases


class ApiError(Exception):
    """Tailscale request failed (never includes credentials or policy bodies)."""


class AuthError(ApiError):
    """Client credentials rejected."""


class ApiHttpError(ApiError):
    """An API endpoint returned an unsuccessful HTTP status."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Tailscale API returned HTTP {status}")


class ConflictError(ApiError):
    """The policy changed since it was read."""


@dataclass(frozen=True)
class Snapshot:
    text: str
    etag: str

    @property
    def policy(self):
        return Policy(self.text)


class TailscaleClient:
    def __init__(
        self, session: ClientSession, tailnet: str, client_id: str, client_secret: str
    ):
        self.session = session
        self.tailnet = tailnet
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = None
        self._expires = 0.0
        self._token_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self._url = f"{API_BASE}/tailnet/{quote(tailnet, safe='')}/acl"

    async def _access_token(self):
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                async with self.session.post(
                    f"{API_BASE}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                    timeout=ClientTimeout(total=30),
                ) as response:
                    if response.status in (400, 401, 403):
                        raise AuthError("OAuth client credentials were rejected")
                    if response.status != 200:
                        raise ApiError(
                            f"OAuth endpoint returned HTTP {response.status}"
                        )
                    data = await response.json()
                    token = data["access_token"]
                    lifetime = float(data["expires_in"])
                    if not isinstance(token, str) or not token or lifetime <= 0:
                        raise ValueError
                    self._token = token
                    self._expires = time.monotonic() + max(0, lifetime - 60)
                    return token
            except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as err:
                raise ApiError("Could not obtain OAuth token") from err

    async def _request(self, method: str, *, body=None, etag=None):
        for attempt in range(2):
            token = await self._access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/hujson",
            }
            if body is not None:
                headers["Content-Type"] = "application/hujson"
                headers["If-Match"] = etag
            try:
                async with self.session.request(
                    method,
                    self._url,
                    headers=headers,
                    data=body,
                    timeout=ClientTimeout(total=30),
                ) as response:
                    if response.status == 401:
                        if self._token == token:
                            self._token = None
                        if attempt == 0:
                            continue
                        raise AuthError("API authentication failed")
                    if response.status == 412:
                        raise ConflictError("Policy changed; reload it before retrying")
                    if not 200 <= response.status < 300:
                        raise ApiHttpError(response.status)
                    return await response.text(), response.headers.get("ETag", "")
            except (ClientError, TimeoutError) as err:
                raise ApiError("Cannot communicate with Tailscale API") from err

    async def get_policy(self) -> Snapshot:
        text, etag = await self._request("GET")
        Policy(text)
        return Snapshot(text, etag)

    async def _write(self, text: str, etag: str):
        if not etag or etag.strip() == "*":
            raise ApiError("A specific policy ETag is required for safe updates")
        await self._request("POST", body=text.encode("utf-8"), etag=etag)

    async def set_domains(
        self, connector: str, domains: list[str], enabled: bool
    ) -> Snapshot:
        return await self.change_domain_pairs(
            connector, domains if enabled else [], domains if not enabled else []
        )

    async def change_domain_pairs(
        self,
        connector: str,
        add: list[str],
        remove: list[str],
        *,
        require_existing: bool = False,
    ) -> Snapshot:
        """Read, edit and conditionally write complete domain pairs."""
        async with self.write_lock:
            for attempt in range(3):
                snapshot = await self.get_policy()
                if require_existing:
                    existing = domain_bases(snapshot.policy.domains(connector))
                    if not {domain_base(d) for d in remove} <= existing:
                        raise PolicyError("Selected domain no longer exists")
                updated = snapshot.policy.change_domain_pairs(connector, add, remove)
                if updated == snapshot.text:
                    return snapshot
                try:
                    await self._write(updated, snapshot.etag)
                except ConflictError:
                    if attempt < 2:
                        continue
                    raise
                return await self.get_policy()
        raise ConflictError("Policy remained busy")

    async def replace_policy(self, text: str, expected_etag: str) -> Snapshot:
        Policy(text)
        async with self.write_lock:
            # Never rebase a whole-document replacement over somebody else's edits.
            await self._write(text, expected_etag)
            return await self.get_policy()
