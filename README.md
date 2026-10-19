# omini-plugin-docker

[Omini](https://github.com/riccardoalv/omini) plugin for **Docker**: one host through the **Docker Engine API** over TCP (ideally behind a read-only socket proxy), or every Docker environment of a **Portainer** server through its API with an access token. Each running container that can be reached on its own, and each Swarm service that publishes ports, is reported **under the host that runs it**, with what it runs and publishes, so that Omini shows a self-hosted app as **one node: the app its network scan found on that port** (Jellyfin, with its icon and its link), carrying the container or service behind it. The other containers are listed as the host's services.

It reads with `GET` requests only, and never starts, stops, pulls or inspects anything inside a container.

Needs **Omini 1.7 or later** (it uses the HTTP client, optional reads and errors of the plugin SDK that ships with it). With **Omini 1.9 or later**, each container and Swarm service is shown as the app the network scan found on its port (with its icon and web link) and Swarm services are read; on 1.7 and 1.8 containers are listed as before.

> **Experimental.** This plugin was built from the official API documentation and tested against answers shaped like the documented ones, not on a real Docker host or Portainer server yet. Reports from real hosts are welcome.

## What it reads

| Data | Used for |
|---|---|
| The host: name, operating system, number of CPUs, installed memory (`/info`) | One device per Docker host |
| Every container: name, image, state, networks, ports (`/containers/json?all=1`) | Container devices, or the host's services |
| Networks and their drivers (`/networks`) | Which containers have an address of their own (macvlan, ipvlan) |
| A Swarm manager's services: name, stack, image, mode and replicas, published ports (`/services?status=true`) | One device per service that publishes ports |
| Their running tasks (`/tasks`, desired state running) | Running replicas, and the nodes a service published in host mode runs on |
| The swarm's nodes and their addresses (`/nodes`) | The other nodes the routing mesh publishes a service's ports on |

### Which containers are devices

| Container | Reported as |
|---|---|
| Running, with an address on a **macvlan** network | A device keyed by its **MAC**, with its IPv4 and IPv6 addresses, the ports it exposes as services |
| Running, with an address on an **ipvlan** network | A device keyed `docker:<host>:<name>` (ipvlan shares the MAC of the host's interface), with its addresses |
| Running, with **published ports** (`-p 8096:8096`) on a bridge | A device keyed `docker:<host>:<name>`, its published ports as services (`8096/tcp`, "published from container port 8096/tcp"), **without addresses**: its bridge address (`172.17.0.x`) and MAC (`02:42:ac:…`) are private to the host and repeat on every Docker host, so they would merge unrelated containers |
| A **Swarm service** that publishes ports (read from a manager) | A device keyed `docker:<manager>:<service>` (`docker:192.168.1.42:homelab_jellyfin`), its published ports as services; its tasks on the manager are not containers of their own |
| Running on a bridge without published ports, or on the host network | A **service of the host** (name, image, running), not a device: it has no address of its own on the network |
| Stopped (exited, created, paused, restarting) | A service of the host, not running |

The host itself is keyed `docker:<address>` (the Engine API has no MAC addresses) and reported at that address, so Omini merges it with the machine its network scan finds there. Each container device declares a link to it (a neighbor of protocol `other`), like the Proxmox plugin does for guests:

```json
{"local_port": "lan", "protocol": "other", "remote_name": "docker1", "remote_port": "eth0",
 "remote_ip": "192.168.1.50", "remote_platform": "Docker"}
```

`local_port` is the container's network; `remote_port` is the host's interface behind a macvlan/ipvlan network (`parent`), or its bridge (`docker0`). A Swarm service's `local_port` is `ingress` (the routing mesh) or `host` (published in host mode).

### What Omini shows

Each container device and Swarm service also says what it is (`container`: its name, image without digest, stack (`com.docker.stack.namespace`, or the Compose project), state, replicas and running replicas of a service, the ports it publishes, and for a service the other nodes its ports answer on). Omini matches it with the apps its network scan found **by host and published port**:

| Case | On the map |
|---|---|
| The scan found an app on a published port (Jellyfin on `192.168.1.42:8096`) | That app's node, with its icon and link; its panel shows **Container**: `homelab_jellyfin`, `lscr.io/linuxserver/jellyfin:latest`, running 1/1 replicas, stack `homelab`, published ports. The container is no node of its own |
| A Swarm service through the routing mesh | The same app found on any node of the swarm carries the service |
| Published ports, no app found on them | An app node of its own under the host, with its image's icon when Omini's app catalog knows it (`jc21/nginx-proxy-manager` → Nginx Proxy Manager) and a link to its port |
| An address of its own (macvlan, ipvlan) | A device of its own; its apps come from scanning its address |
| No published ports | A service in the host's panel |

A port published on the host's loopback only (`127.0.0.1:3002:3001`) is not one the network reaches: it is listed as a service, not matched with an app.

Container names (not IDs) are used in keys, so a container recreated by `docker compose up` stays the same device. A Docker Swarm task (`stack_web.1.<task id>`, or `stack_agent.<node id>.<task id>` for a global service) is named after its service (`stack_web`, `stack_web.2` for its second replica), since the task id changes on every deploy.

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
      SERVICES: 1     # a Swarm manager's services and the ports they publish
      TASKS: 1        # where they run, how many replicas are running
      NODES: 1        # the swarm's nodes (the routing mesh's addresses)
      POST: 0         # no changes (the default)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "192.168.1.50:2375:2375"   # the host's LAN address only
```

Then use `http://192.168.1.50:2375` as the address. `VERSION` and `PING` are allowed by the proxy by default. With `POST: 0` the proxy refuses every change, whatever else is allowed. Without `NETWORKS`, containers with their own address are listed as the host's services (only published ports make devices); without `INFO`, the host is named by its address. `SERVICES`, `TASKS` and `NODES` only matter on a Swarm manager (a worker is never asked): without `SERVICES` a service's tasks are the containers of the node they run on, as on a host without Swarm; without `TASKS` a service's running replicas come from its status (Docker 24+) or stay unknown; without `NODES` its ports are matched on the manager only. **Test connection** says which of them the proxy refuses.

A Docker API over TLS (`https://host:2376`) works when it does not ask for a client certificate; client certificates are not supported (yet).

## Portainer: an access token

1. In Portainer, create a user for Omini (**Settings → Users → Add user**) and give it access to the environments to read (**Environments → (environment) → Manage access**, or through a team).
   - **Portainer Business Edition** has a **Read-only user** role: use it.
   - **Portainer Community Edition** has no read-only role: a standard user can change the containers of the environments it has access to. The plugin only reads, but the token could do more; keep it in Omini only.
2. Sign in as that user, then **My account → Access tokens → Add access token**. Copy the token (shown once).
3. In Omini: **Read through** Portainer, the address of Portainer (`https://192.168.1.50:9443`), the token, and optionally one environment ID (else every Docker environment the user sees).

Docker environments (local socket, Portainer agent, Edge agent) are read; Kubernetes and Azure ACI environments and environments Portainer reports down are skipped. An environment reached on a local socket (`unix://`) is the host Portainer runs on; one behind an agent (`tcp://192.168.1.60:9001`) is that agent's address; an Edge agent without an address is keyed by its environment ID (`docker:portainer-environment-<id>`) and named after the environment. One environment failing never fails the others.

## Form fields

| Field | Example | Notes |
|---|---|---|
| Read through | `Docker Engine` | `Docker Engine` or `Portainer` |
| Address | `http://192.168.1.50:2375` / `https://192.168.1.50:9443` | Without a scheme: `http://` and port 2375 for Docker Engine (2376 for `https://`), `https://` and port 9443 for Portainer. `tcp://host:2375` (as in `DOCKER_HOST`) is accepted. IPv6: `http://[fd00::50]:2375` |
| Portainer access token | `ptr_…` | Portainer only. Stored encrypted by Omini, never logged |
| Portainer environment ID | `2` | Portainer only, optional: empty reads every Docker environment |
| Verify the TLS certificate (Advanced) | off | Portainer uses a self-signed certificate by default |
| Timeout, seconds (Advanced) | `15` | How long to wait for each answer (5 to 120). A refused connection or a busy API (429, 503) is asked again, twice, with a pause; a read that timed out, once |

## How it works

Every request is a `GET`. Hosts and containers are reported with the vendor `Docker`. A section the API refuses (403) or does not have (404), or that answers another error, is skipped and logged, never failing the collection, except the container list; **Test connection** lists what a refusal needs (a socket proxy section, or a Portainer user's access to an environment). Docker Engine: to the address itself, unversioned (the daemon answers with its own API version). Portainer: to `<address>/api/…` with the header `X-API-Key: <token>`, and the Engine API of each environment through Portainer's proxy, `/api/endpoints/{id}/docker/…`.

| Endpoint | Socket proxy section | Read for |
|---|---|---|
| [`/version`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/System/operation/SystemVersion) | `VERSION` (on by default) | The connection test: checks it is a Docker API |
| [`/info`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/System/operation/SystemInfo) | `INFO` | `Name`, `OperatingSystem`, `NCPU`, `MemTotal` |
| [`/containers/json?all=1`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Container/operation/ContainerList) | `CONTAINERS` | `Names`, `Image`, `State`, `Ports`, `HostConfig.NetworkMode`, `NetworkSettings.Networks` (`IPAddress`, `IPPrefixLen`, `GlobalIPv6Address`, `GlobalIPv6PrefixLen`, `MacAddress`) |
| [`/networks`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Network/operation/NetworkList) | `NETWORKS` | `Name`, `Driver`, `Options.parent`, `Options["com.docker.network.bridge.name"]` |
| [`/services?status=true`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Service/operation/ServiceList) | `SERVICES` | A manager only (`/info` `Swarm.ControlAvailable`): `ID`, `Spec.Name`, `Spec.Labels["com.docker.stack.namespace"]`, `Spec.TaskTemplate.ContainerSpec.Image`, `Spec.Mode` (`Replicated.Replicas`, `Global`), `Endpoint.Ports` (`PublishedPort`, `TargetPort`, `Protocol`, `PublishMode`), `ServiceStatus` (`RunningTasks`, `DesiredTasks`) |
| [`/tasks?filters={"desired-state":["running"]}`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Task/operation/TaskList) | `TASKS` | `ServiceID`, `NodeID`, `Status.State` |
| [`/nodes`](https://docs.docker.com/reference/api/engine/version/v1.47/#tag/Node/operation/NodeList) | `NODES` | `ID`, `Status.State`, `Status.Addr` (a manager's `0.0.0.0`: `ManagerStatus.Addr`) |
| [Portainer `/api/system/status`](https://app.swaggerhub.com/apis/portainer/portainer-ce/) (or `/api/status`) | | Portainer's version, for the connection test |
| [Portainer `/api/endpoints`](https://app.swaggerhub.com/apis/portainer/portainer-ce/) | | Environments: `Id`, `Name`, `Type` (1, 2, 4: Docker), `URL`, `Status` (2: down) |
| Portainer `/api/endpoints/{id}/docker/…` | | The Engine endpoints above, for each environment |

IPv6 is first-class: a container's global and ULA IPv6 addresses are reported next to its IPv4 (link-local stays out), in their short form; an IPv6 Docker host (`http://[fd00::50]:2375`) is keyed and linked by its IPv6 address.

## Known limitations

- **No MAC address for the Docker host**: the Engine API has none. The host is keyed by its address and merged by it with what the network scan finds there.
- **No host storage, CPU or memory use**: `/system/df` computes the size of every image and volume (slow on a large host) and is not read; the API has no live CPU or memory figures for the host (per-container stats are a slow, per-container stream). They are left empty, never estimated.
- **No container uptimes or health**: they are not in the container list (`Status` is a sentence, "Up 3 days (healthy)", not a value), and inspecting each container is one more request per container.
- **ipvlan containers** have no MAC of their own; the network scan sees their addresses with the host's MAC.
- **Kubernetes** is not read.
- **Swarm services** are read from a manager only; a worker's tasks show up as the containers of the node they run on. Two managers added to Omini report each service twice; Omini shows each app once.
- **Device.container** comes with the Omini release after 1.8.1: with an older one the plugin reads no Swarm services and reports as before.
- **Client TLS certificates** for the Docker API are not supported.

### Not verified on hardware

- That `tecnativa/docker-socket-proxy` answers `403` for a section it does not allow (as its documentation says), which the plugin reports as the section to turn on.
- That Portainer's agent environments report their agent's address in `URL` (`tcp://host:9001`), and that Edge agents in asynchronous mode refuse the Docker proxy (they are then skipped with a log line, not failed).
- That `/api/system/status` stays readable without a token (it is documented as public).
- Swarm services, tasks and nodes were tested against answers shaped like the API docs and like the maintainer's swarm, not through a socket proxy or Portainer's agent yet.
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
