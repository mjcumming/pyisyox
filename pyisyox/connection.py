"""Connection to the ISY."""

from __future__ import annotations

from argparse import Namespace
import asyncio
from dataclasses import InitVar, dataclass, field
from urllib.parse import ParseResult, quote, urlencode, urlparse

import aiohttp

from pyisyox.configuration import Configuration, ConfigurationData
from pyisyox.constants import URL_PING
from pyisyox.exceptions import ISYConnectionError, ISYInvalidAuthError
from pyisyox.helpers.session import get_new_client_session, get_sslcontext
from pyisyox.logging import _LOGGER, enable_logging

MAX_HTTPS_CONNECTIONS_ISY = 1
MAX_HTTP_CONNECTIONS_ISY = 1
# Lowered from 20/50 — empirical testing against ISY-994-era hardware
# under realistic load (Home Assistant fanning out commands while ISY-side
# programs are also active) showed bursty traffic overwhelms the controller
# and produces "Invalid Command" rejections. Consumers with healthier hub
# setups can override via the `max_concurrent` kwarg on `Connection` / `ISY`.
MAX_HTTPS_CONNECTIONS_IOX = 2
MAX_HTTP_CONNECTIONS_IOX = 2

MAX_RETRIES = 5
RETRY_BACKOFF = [0.01, 0.10, 0.25, 1, 2]  # Seconds
COMMAND_INVALID_BACKOFF = [0.5, 1.5, 4, 8, 15]  # Seconds

HTTP_OK = 200  # Valid request received, will run it
HTTP_UNAUTHORIZED = 401  # User authentication failed
HTTP_NOT_FOUND = 404  # Unrecognized request received and ignored
HTTP_SERVICE_UNAVAILABLE = 503  # Valid request received, system too busy to run it

HTTP_TIMEOUT = 30

HTTP_HEADERS = {
    "Connection": "keep-alive",
    "Keep-Alive": "5000",
    "Accept-Encoding": "gzip, deflate",
}

EMPTY_XML_RESPONSE = '<?xml version="1.0" encoding="UTF-8"?>'


@dataclass
class ISYConnectionInfo:
    """Dataclass to represent connection details."""

    url: str
    username: InitVar[str]
    password: InitVar[str]
    rest_url: str = field(init=False)
    ws_url: str = field(init=False)
    auth: aiohttp.BasicAuth = field(init=False)
    parsed_url: ParseResult = field(init=False)
    use_https: bool = field(init=False)
    websession: aiohttp.ClientSession | None = None
    tls_version: float | None = None

    def __post_init__(self, username: str, password: str) -> None:
        """Post process the connection info."""
        self.rest_url = f"{self.url.rstrip('/')}/rest"
        self.ws_url = f"{self.rest_url.replace('http', 'ws').rstrip('/')}/subscribe"
        self.auth = aiohttp.BasicAuth(username, password)
        self.parsed_url = urlparse(self.url)
        self.use_https = self.url.startswith("https")


class Connection:
    """Connection object to manage connection to and interaction with ISY."""

    connection_info: ISYConnectionInfo
    args: Namespace | None

    def __init__(
        self,
        connection_info: ISYConnectionInfo,
        args: Namespace | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        """Initialize the Connection object.

        Args:
            connection_info: Parsed URL + credentials + session.
            args: Optional CLI args namespace (passed through from
                the ``__main__`` entry point).
            max_concurrent: User override for the outbound HTTP
                concurrency cap. When ``None`` (default), the cap
                follows hub-platform auto-detection: starts at the
                single-lane legacy ISY value, bumps to the IoX value if
                :meth:`ISY.initialize` sees ``platform == "IoX"``.
                When set explicitly, the override wins and the
                IoX bump becomes a no-op. ``1`` forces strict
                serialization; useful for older/slower hardware that
                rejects bursty traffic.
        """
        if len(_LOGGER.handlers) == 0:
            enable_logging(add_null_handler=True)

        self.connection_info = connection_info
        self.args = args
        self._max_concurrent_override = max_concurrent

        initial = (
            max_concurrent
            if max_concurrent is not None
            else (
                MAX_HTTPS_CONNECTIONS_ISY
                if connection_info.use_https
                else MAX_HTTP_CONNECTIONS_ISY
            )
        )
        self.semaphore = asyncio.Semaphore(initial)

        if connection_info.websession is None:
            connection_info.websession = get_new_client_session(connection_info)
        self.req_session = connection_info.websession
        self.sslcontext = get_sslcontext(connection_info)

    async def test_connection(self) -> ConfigurationData:
        """Test the connection and get the config for the ISY."""
        config = Configuration()
        if not (config_data := await config.update(self)):
            raise ISYConnectionError(
                "Could not connect to the ISY with the parameters provided"
            )
        return config_data

    def increase_available_connections(self) -> None:
        """Increase the number of allowed connections for newer hardware.

        Called by :meth:`ISY.initialize` when the hub reports
        ``platform == "IoX"``. No-op when a ``max_concurrent`` override
        was passed to :meth:`__init__` — the user's explicit cap wins
        over platform auto-detection.
        """
        if self._max_concurrent_override is not None:
            _LOGGER.debug(
                "Skipping IoX cap bump; max_concurrent=%s overrides auto-detection",
                self._max_concurrent_override,
            )
            return
        _LOGGER.debug("Increasing available simultaneous connections")
        self.semaphore = asyncio.Semaphore(
            MAX_HTTPS_CONNECTIONS_IOX
            if self.connection_info.use_https
            else MAX_HTTP_CONNECTIONS_IOX
        )

    async def close(self) -> None:
        """Cleanup connections and prepare for exit."""
        await self.req_session.close()

    @property
    def url(self) -> str:
        """Return the full connection url."""
        return self.connection_info.url

    # COMMON UTILITIES
    def compile_url(self, path: list[str], query: dict[str, str] | None = None) -> str:
        """Compile the URL to fetch from the ISY."""
        url = f"{self.connection_info.rest_url}/{'/'.join([quote(item) for item in path])}"
        if query is not None:
            url += f"?{urlencode(query)}"
        return url

    async def request(
        self, url: str, retries: int = 0, ok404: bool = False, delay: float = 0
    ) -> str | None:
        """Execute request to ISY REST interface."""
        _LOGGER.debug("Request: %s", url)
        endpoint = url.split("rest", 1)[1]
        retry_backoff = RETRY_BACKOFF
        slept_for_retry = False
        if delay:
            await asyncio.sleep(delay)
        try:
            async with (
                self.semaphore,
                self.req_session.get(
                    url,
                    auth=self.connection_info.auth,
                    headers=HTTP_HEADERS,
                    timeout=HTTP_TIMEOUT,
                    ssl=self.sslcontext,
                ) as res,
            ):
                if res.status == HTTP_OK:
                    _LOGGER.debug("Response received: %s", endpoint)
                    results = await res.text(encoding="utf-8", errors="ignore")
                    if results != EMPTY_XML_RESPONSE:
                        return results
                    _LOGGER.debug("Invalid empty XML returned: %s", endpoint)
                    res.release()
                if res.status == HTTP_NOT_FOUND:
                    if ok404:
                        _LOGGER.debug("Response received %s", endpoint)
                        res.release()
                        return ""
                    # ISY-994-era controllers can return 404 "Invalid
                    # Command" for otherwise-valid commands when their
                    # internal queues are saturated, even immediately
                    # after a NOT_BUSY (_5) frame. Treat command 404s as
                    # transient and pace the whole command lane before
                    # retrying; truly invalid commands will still bottom
                    # out after MAX_RETRIES with the "Bad ISY Request"
                    # ERROR below.
                    if "/cmd/" in endpoint:
                        retry_backoff = COMMAND_INVALID_BACKOFF
                    _LOGGER.warning(
                        "Reported an Invalid Command received %s; will retry", endpoint
                    )
                    res.release()
                    if retries < MAX_RETRIES and "/cmd/" in endpoint:
                        delay_seconds = retry_backoff[retries]
                        _LOGGER.debug(
                            "Cooling down ISY command lane for %ss before retry %s.",
                            delay_seconds,
                            retries + 1,
                        )
                        await asyncio.sleep(delay_seconds)
                        slept_for_retry = True
                if res.status == HTTP_UNAUTHORIZED:
                    _LOGGER.error("Invalid credentials provided for ISY connection.")
                    res.release()
                    raise ISYInvalidAuthError(
                        "Invalid credentials provided for ISY connection."
                    )
                if res.status == HTTP_SERVICE_UNAVAILABLE:
                    _LOGGER.warning("ISY too busy to process request %s", endpoint)
                    res.release()

        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout while trying to connect to the ISY.")
        except (
            aiohttp.ClientOSError,
            aiohttp.ServerDisconnectedError,
        ):
            _LOGGER.debug("ISY not ready or closed connection.")
        except aiohttp.ClientResponseError as err:
            _LOGGER.error(
                "Client Response %s Error %s %s", err.status, err.message, endpoint
            )
        except aiohttp.ClientError as err:
            _LOGGER.error(
                "Could not receive response from device because of a network issue: %s",
                type(err),
            )

        if retries is None:
            raise ISYConnectionError()
        if retries < MAX_RETRIES:
            if not slept_for_retry:
                _LOGGER.debug(
                    "Retrying ISY Request in %ss, retry %s.",
                    retry_backoff[retries],
                    retries + 1,
                )
                # sleep to allow the ISY to catch up
                await asyncio.sleep(retry_backoff[retries])
            # recurse to try again
            retry_result = await self.request(url, retries + 1, ok404=ok404)
            return retry_result
        # fail for good
        _LOGGER.error(
            "Bad ISY Request: (%s) Failed after %s retries.",
            url,
            retries,
        )
        return None

    async def ping(self) -> bool:
        """Test connection to the ISY and return True if alive."""
        req_url = self.compile_url([URL_PING])
        result = await self.request(req_url, ok404=True)
        return result is not None

    async def get_description(self) -> str | None:
        """Fetch the services description from the ISY."""
        return await self.request(f"{self.connection_info.url}/desc")
