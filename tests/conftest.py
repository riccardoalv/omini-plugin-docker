import copy
import json
import threading
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_docker.collect as collect_module
from omini_docker.client import Client

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "ptr_k3JmUe0yQ1bS8wX2v7Lr9aZcT4nH6pD5fG0sE1uI2oA="


class FakeDocker:
    """Answers like Docker Engine 27.3 (the API as a socket proxy exposes it)
    and Portainer 2.21, from the JSON files in fixtures/
    (`containers__json.json` is GET /containers/json; `portainer__endpoints.json`
    is GET /api/endpoints). Portainer's environments 1 and 2 proxy the Engine
    API under /api/endpoints/{id}/docker."""

    def __init__(self):
        self.engine = {}
        self.portainer = {}
        for f in FIXTURES.glob("*.json"):
            body = json.loads(f.read_text())
            if f.stem.startswith("portainer__"):
                self.portainer["/api/" + f.stem.removeprefix("portainer__").replace("__", "/")] = (
                    body
                )
            else:
                self.engine["/" + f.stem.replace("__", "/")] = body
        # Environment 2 (a NAS with the Portainer agent) runs one container.
        nas = copy.deepcopy(self.engine)
        nas["/info"]["Name"] = "nas"
        nas["/containers/json"] = [
            c for c in nas["/containers/json"] if c["Names"] == ["/jellyfin"]
        ]
        self.environments = {"1": self.engine, "2": nas}
        self.errors = {}  # path prefix → HTTP status
        self.calls = []
        self.methods = set()
        self.token = TOKEN
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        with self.lock:
            self.calls.append(path)
            self.methods.add(request.method)
        for prefix, status in self.errors.items():
            if path.startswith(prefix):
                return httpx.Response(status, json={"message": "forbidden"})
        if path.startswith("/api/"):
            return self.portainer_answer(request, path)
        if path not in self.engine:
            return httpx.Response(404, json={"message": "page not found"})
        return httpx.Response(200, json=self.engine[path])

    def portainer_answer(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/api/system/status":  # public
            return httpx.Response(200, json=self.portainer[path])
        if request.headers.get("x-api-key") != self.token:
            return httpx.Response(
                401, json={"message": "Unauthorized", "details": "Invalid API key"}
            )
        if path in self.portainer:
            return httpx.Response(200, json=self.portainer[path])
        parts = path.split("/", 5)  # '', api, endpoints, id, docker, rest
        if len(parts) == 6 and parts[2] == "endpoints" and parts[4] == "docker":
            routes = self.environments.get(parts[3])
            if routes is not None and "/" + parts[5] in routes:
                return httpx.Response(200, json=routes["/" + parts[5]])
        return httpx.Response(404, json={"message": "Not found"})


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(fake.handler))
    )
    return fake


@pytest.fixture
def cfg():
    from omini_sdk import Config

    return Config({"source": "Docker Engine", "url": "http://192.168.1.50:2375"})


@pytest.fixture
def portainer_cfg():
    from omini_sdk import Config

    return Config({"source": "Portainer", "url": "https://192.168.1.50:9443", "token": TOKEN})
