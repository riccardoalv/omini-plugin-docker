# omini-plugin-docker

[Omini](https://github.com/riccardoalv/omini) plugin for **Docker**: one host through the **Docker Engine API** over TCP (ideally behind a read-only socket proxy), or every Docker environment of a **Portainer** server through its API with an access token. Each running container that can be reached on its own is reported as its own device, **placed under the host that runs it**; the other containers are listed as the host's services.

It reads with `GET` requests only, and never starts, stops, pulls or inspects anything inside a container.

> **Experimental.** This plugin was built from the official API documentation and tested against answers shaped like the documented ones, not on a real Docker host or Portainer server yet. Reports from real hosts are welcome.

## What it reads

| Data | Used for |
|---|---|
| The host: name, operating system, number of CPUs, installed memory (`/info`) | One device per Docker host |
| Every container: name, image, state, networks, ports (`/containers/json?all=1`) | Container devices, or the host's services |
| Networks and their drivers (`/networks`) | Which containers have an address of their own (macvlan, ipvlan) |

### Which containers are devices

| Container | Reported as |
|---|---|
| Running, with an address on a **macvlan** network | A device keyed by its **MAC**, with its IPv4 and IPv6 addresses, the ports it exposes as services |
| Running, with an address on an **ipvlan** network | A device keyed `docker:<host>:<name>` (ipvlan shares the MAC of the host's interface), with its addresses |
| Running, with **published ports** (`-p 8096:8096`) on a bridge | A device keyed `docker:<host>:<name>`, its published ports as services (`8096/tcp`, "published from container port 8096/tcp"), **without addresses**: its bridge address (`172.17.0.x`) and MAC (`02:42:ac:…`) are private to the host and repeat on every Docker host, so they would merge unrelated containers |
| Running on a bridge without published ports, or on the host network | A **service of the host** (name, image, running), not a device: it has no address of its own on the network |
| Stopped (exited, created, paused, restarting) | A service of the host, not running |

The host itself is keyed `docker:<address>` (the Engine API has no MAC addresses) and reported at that address, so Omini merges it with the machine its network scan finds there. Each container device declares a link to it (a neighbor of protocol `other`), like the Proxmox plugin does for guests:

```json
{"local_port": "lan", "protocol": "other", "remote_name": "docker1", "remote_port": "eth0",
 "remote_ip": "192.168.1.50", "remote_platform": "Docker"}
```

`local_port` is the container's network; `remote_port` is the host's interface behind a macvlan/ipvlan network (`parent`), or its bridge (`docker0`).

Container names (not IDs) are used in keys, so a container recreated by `docker compose up` stays the same device.

## Install

**Integrations → Add → Docker** (when it is in Omini's plugin catalog), or install it from its address, `https://github.com/riccardoalv/omini-plugin-docker`.

## Docker Engine: a read-only socket proxy

The Docker API has no accounts: whoever reaches it controls the host (`-H tcp://0.0.0.0:2375` gives root on it to the whole network). Do **not** expose the daemon itself. Put a filtering proxy in front of the socket that only allows reads, such as [tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy):

```yaml
# docker-compose.yml on the Docker host
services:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    restart: unless-stopped
    environment:
      CONTAINERS: 1   # list the containers (required)
      NETWORKS: 1     # networks and their drivers: macvlan/ipvlan containers as devices
      INFO: 1         # host name, CPUs, memory, operating system
      POST: 0         # no changes (the default)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "192.168.1.50:2375:2375"   # the host's LAN address only
```

Then use `http://192.168.1.50:2375` as the address. `VERSION` and `PING` are allowed by the proxy by default. With `POST: 0` the proxy refuses every change, whatever else is allowed. Without `NETWORKS`, containers with their own address are listed as the host's services (only published ports make devices); without `INFO`, the host is named by its address. **Test connection** says which of them the proxy refuses.

A Docker API over TLS (`https://host:2376`) works when it does not ask for a client certificate; client certificates are not supported (yet).

## Portainer: an access token

1. In Portainer, create a user for Omini (**Settings → Users → Add user**) and give it access to the environments to read (**Environments → (environment) → Manage access**, or through a team).
   - **Portainer Business Edition** has a **Read-only user** role: use it.
   - **Portainer Community Edition** has no read-only role: a standard user can change the containers of the environments it has access to. The plugin only reads, but the token could do more; keep it in Omini only.
2. Sign in as that user, then **My account → Access tokens → Add access token**. Copy the token (shown once).
3. In Omini: **Read through** Portainer, the address of Portainer (`https://192.168.1.50:9443`), the token, and optionally one environment ID (else every Docker environment the user sees).

Docker environments (local socket, Portainer agent, Edge agent) are read; Kubernetes and Azure ACI environments and environments Portainer reports down are skipped. An environment reached on a local socket (`unix://`) is the host Portainer runs on; one behind an agent (`tcp://192.168.1.60:9001`) is that agent's address; an Edge agent without an address is named after the environment. One environment failing never fails the others.

## Form fields

| Field | Example | Notes |
|---|---|---|
| Read through | `Docker Engine` | `Docker Engine` or `Portainer` |
| Address | `http://192.168.1.50:2375` / `https://192.168.1.50:9443` | Without a scheme: `http://` and port 2375 for Docker Engine (2376 for `https://`), `https://` and port 9443 for Portainer. `tcp://host:2375` (as in `DOCKER_HOST`) is accepted. IPv6: `http://[fd00::50]:2375` |
| Portainer access token | `ptr_…` | Portainer only. Stored encrypted by Omini, never logged |
| Portainer environment ID | `2` | Portainer only, optional: empty reads every Docker environment |
| Verify the TLS certificate (Advanced) | off | Portainer uses a self-signed certificate by default |
| Wait for an answer, seconds (Advanced) | `10` | How long to wait for each answer (5 to 120) |

## How it works

Every request is a `GET`. Docker Engine: to the address itself, unversioned (the daemon answers with its own API version). Portainer: to `<address>/api/…` with the header `X-API-Key: <token>`, and the Engine API of each environment through Portainer's proxy, `/api/endpoints/{id}/docker/…`.

| Endpoint | Socket proxy section | Read for |
|---|---|---|
| [`/version`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/System/operation/SystemVersion) | `VERSION` (on by default) | The connection test: checks it is a Docker API |
| [`/info`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/System/operation/SystemInfo) | `INFO` | `Name`, `OperatingSystem`, `NCPU`, `MemTotal` |
| [`/containers/json?all=1`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Container/operation/ContainerList) | `CONTAINERS` | `Names`, `Image`, `State`, `Ports`, `HostConfig.NetworkMode`, `NetworkSettings.Networks` (`IPAddress`, `IPPrefixLen`, `GlobalIPv6Address`, `GlobalIPv6PrefixLen`, `MacAddress`) |
| [`/networks`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Network/operation/NetworkList) | `NETWORKS` | `Name`, `Driver`, `Options.parent`, `Options["com.docker.network.bridge.name"]` |
| [Portainer `/api/system/status`](https://app.swaggerhub.com/apis/portainer/portainer-ce/) (or `/api/status`) | | Portainer's version, for the connection test |
| [Portainer `/api/endpoints`](https://app.swaggerhub.com/apis/portainer/portainer-ce/) | | Environments: `Id`, `Name`, `Type` (1, 2, 4: Docker), `URL`, `Status` (2: down) |
| Portainer `/api/endpoints/{id}/docker/…` | | The Engine endpoints above, for each environment |

IPv6 is first-class: a container's global and ULA IPv6 addresses are reported next to its IPv4 (link-local stays out), in their short form; an IPv6 Docker host (`http://[fd00::50]:2375`) is keyed and linked by its IPv6 address.

## Known limitations

- **No MAC address for the Docker host**: the Engine API has none. The host is keyed by its address and merged by it with what the network scan finds there.
- **No host storage, CPU or memory use**: `/system/df` computes the size of every image and volume (slow on a large host) and is not read; the API has no live CPU or memory figures for the host (per-container stats are a slow, per-container stream). They are left empty, never estimated.
- **No container uptimes or health**: they are not in the container list (`Status` is a sentence, "Up 3 days (healthy)", not a value), and inspecting each container is one more request per container.
- **ipvlan containers** have no MAC of their own; the network scan sees their addresses with the host's MAC.
- **Swarm services and Kubernetes** are not read; Swarm tasks show up as the containers of the node they run on.
- **Client TLS certificates** for the Docker API are not supported.

### Not verified on hardware

- That `tecnativa/docker-socket-proxy` answers `403` for a section it does not allow (as its documentation says), which the plugin reports as the section to turn on.
- That Portainer's agent environments report their agent's address in `URL` (`tcp://host:9001`), and that Edge agents in asynchronous mode refuse the Docker proxy (they are then skipped with a log line, not failed).
- That `/api/system/status` stays readable without a token (it is documented as public).
- That a macvlan container's `MacAddress` is filled in the container list on Docker 26+ (it moved in the API: the plugin reads the per-network one).

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against answers shaped like the API docs (tests/fixtures)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-docker make run` in the Omini repository; every collection uses the current code.

## License

MIT
