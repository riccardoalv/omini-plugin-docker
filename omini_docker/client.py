"""Read-only client for the Docker Engine API and Portainer's API, on the
SDK's HTTP client: timeouts, retries, and errors that name what a socket
proxy (or Portainer) must allow.

https://docs.docker.com/reference/api/engine/
https://docs.portainer.io/api/docs
"""

from __future__ import annotations

import re

import httpx
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import Forbidden, NotFound, Unavailable, normalize_url

__all__ = ["ENGINE", "PORTAINER", "SECTIONS", "Client", "Forbidden", "NotFound", "Unavailable"]

ENGINE = "Docker Engine"
PORTAINER = "Portainer"

# What a socket proxy (tecnativa/docker-socket-proxy and alikes) must allow
# for each piece of data, by the API section it opens.
SECTIONS = {
    "info": "INFO=1 on the socket proxy (host name, CPUs, memory, operating system)",
    "networks": "NETWORKS=1 on the socket proxy (which containers have their own address)",
    "containers": "CONTAINERS=1 on the socket proxy (the containers)",
}

ENVIRONMENT_PATH = re.compile(r"^/endpoints/(\d+)/docker(/.*)?$")


def base_url(url: str, source: str) -> str:
    """'192.168.1.50' → 'http://192.168.1.50:2375' (Docker Engine) or
    'https://192.168.1.50:9443' (Portainer). An explicit scheme and port are kept."""
    url = url.strip()
    if url.startswith("tcp://"):  # as in DOCKER_HOST
        url = "http://" + url[len("tcp://") :]
    if source == PORTAINER:
        return normalize_url(
            url, scheme="https", port={"https": 9443, "http": 9000}, strip=("/api",)
        )
    return normalize_url(url, scheme="http", port={"https": 2376, "http": 2375})


def section(path: str) -> str | None:
    """The socket proxy section of an Engine API path ('/networks' → NETWORKS)."""
    name = path.lstrip("/").split("/", 1)[0]
    return SECTIONS.get(name)


class Client(HttpClient):
    def __init__(
        self,
        url: str,
        source: str = ENGINE,
        token: str = "",
        verify_tls: bool = False,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
    ):
        self.source = source
        portainer = source == PORTAINER
        base = base_url(url, source)
        super().__init__(
            base,
            name=source,
            api="/api" if portainer else "",
            verify_tls=verify_tls,
            timeout=timeout,
            headers={"X-API-Key": token} if portainer else None,
            user_agent="omini-plugin-docker",
            unauthorized=(
                "Portainer rejected the access token: check it, or create a new one"
                if portainer
                else f"{base} asks for authentication: the Docker Engine API has none, "
                "is it a Docker API or a socket proxy?"
            ),
            forbidden_hint=self.needs,
            transport=transport,
        )

    def needs(self, method: str, path: str) -> str | None:
        """What a refused path needs: the socket proxy section to turn on, or
        (Portainer) the user's access to the environment."""
        if self.source == ENGINE:
            return section(path)
        m = ENVIRONMENT_PATH.match(path)
        if m:
            return f"access to Portainer environment {m.group(1)} (Environments → Manage access)"
        return None

    def check(self, r: httpx.Response, method: str, path: str) -> None:
        """As the SDK's, but another 4xx to a read is the environment being
        unavailable (an Edge agent without a tunnel, a section the proxy
        answers otherwise), not a bug: it is skipped like a server error."""
        status = r.status_code
        if 400 <= status < 500 and status not in (401, 403, 404, 410) and method == "GET":
            raise Unavailable(
                f"{self.name} answered {status} for {path}",
                status=status,
                method=method,
                resource=path,
            )
        super().check(r, method, path)
