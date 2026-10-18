"""Minimal read-only client for the Docker Engine API and Portainer's API.

https://docs.docker.com/reference/api/engine/
https://docs.portainer.io/api/docs
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from omini_sdk import PluginError, log

ENGINE = "Docker Engine"
PORTAINER = "Portainer"


class Forbidden(Exception):
    """The API refused a path (HTTP 403): a socket proxy that does not allow
    that section, or a Portainer user without access to an environment."""


class NotFound(Exception):
    """The path does not exist on this version (HTTP 404)."""


class Unavailable(Exception):
    """The API answered with another error for this path (an environment that
    is down, an Edge agent without a tunnel...)."""


def base_url(url: str, source: str) -> str:
    """'192.168.1.50' → 'http://192.168.1.50:2375' (Docker Engine) or
    'https://192.168.1.50:9443' (Portainer). An explicit scheme and port are kept."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = ("https://" if source == PORTAINER else "http://") + url
    if url.startswith("tcp://"):  # as in DOCKER_HOST
        url = "http://" + url[len("tcp://") :]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise PluginError("the address must start with http:// or https://")
    if not parts.hostname:
        raise PluginError("the address has no host")
    netloc = parts.netloc
    if parts.port is None:
        if source == PORTAINER:
            port = 9443 if parts.scheme == "https" else 9000
        else:
            port = 2376 if parts.scheme == "https" else 2375
        netloc = f"{netloc}:{port}"
    path = parts.path
    if source == PORTAINER and path.endswith("/api"):
        path = path[: -len("/api")]
    return urlunsplit((parts.scheme, netloc, path, "", ""))


class Client:
    def __init__(
        self,
        url: str,
        source: str = ENGINE,
        token: str = "",
        verify_tls: bool = False,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ):
        self.source = source
        self.base = base_url(url, source)
        headers = {"Accept": "application/json", "User-Agent": "omini-plugin-docker"}
        if source == PORTAINER:
            headers["X-API-Key"] = token
        self.http = httpx.Client(
            base_url=self.base + ("/api" if source == PORTAINER else ""),
            verify=verify_tls,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    @property
    def host(self) -> str:
        return urlsplit(self.base).hostname or ""

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GETs an API path and returns its JSON."""
        try:
            r = self.http.get(path, params=params)
        except httpx.ConnectError as e:
            raise PluginError(f"cannot connect to {self.base}") from e
        except httpx.TimeoutException as e:
            raise PluginError(f"{self.base} did not answer in time") from e
        except httpx.HTTPError as e:
            raise PluginError(f"request to {self.base} failed ({type(e).__name__})") from e
        if r.status_code == 401:
            if self.source == PORTAINER:
                raise PluginError(
                    "Portainer rejected the access token: check it, or create a new one"
                )
            raise PluginError(
                f"{self.base} asks for authentication: the Docker Engine API has none, "
                "is it a Docker API or a socket proxy?"
            )
        if r.status_code == 403:
            raise Forbidden(path)
        if r.status_code == 404:
            raise NotFound(path)
        if r.status_code >= 400:
            raise Unavailable(f"{path}: HTTP {r.status_code} {r.reason_phrase}".strip())
        try:
            return r.json()
        except json.JSONDecodeError as e:
            log.debug("%s answered %s with something that is not JSON", self.base, path)
            raise PluginError(
                f"{self.base} did not answer with JSON: is it the {self.source} address?"
            ) from e
