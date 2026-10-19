"""Turns Docker Engine (or Portainer) answers into Omini devices: one per
Docker host, plus one per running container that can be reached on its own
(an address on a macvlan / ipvlan network, or published ports) and, on a
Swarm manager, one per service that publishes ports, hung under the host
that runs it. The other containers are the host's services. A container or
service says what it runs and publishes (Device.container), so that Omini
shows it as the app its network scan finds on those ports."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlsplit

from omini_sdk import Config, Device, Interface, Neighbor, PluginError, Service, log
from omini_sdk import optional as sdk_optional
from omini_sdk.http import settings as http_settings

from omini_docker.client import (
    ENGINE,
    PORTAINER,
    SECTIONS,
    Client,
    Forbidden,
    NotFound,
    Unavailable,
)

try:  # Device.container came in a later SDK: without it, Swarm services are not read
    from omini_sdk.models import Container, ContainerPort
except ImportError:  # pragma: no cover
    Container = ContainerPort = None  # type: ignore[assignment,misc]
CONTAINER = Container is not None and "container" in Device.model_fields

T = TypeVar("T")

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Networks where a container has an address of its own on the LAN.
OWN_ADDRESS = {"macvlan", "ipvlan"}

# Portainer environment types that are Docker (1: local socket or TCP API,
# 2: Portainer agent, 4: Edge agent); Kubernetes and ACI are not read.
DOCKER_ENVIRONMENTS = {1, 2, 4}


def wait_s(cfg: Config) -> float:
    """How long to wait for each answer: the "timeout_s" field (the SDK's
    default, 15 s), kept within 5 to 120 s."""
    return float(min(max(http_settings(cfg)["timeout"], 5), 120))


def source_of(cfg: Config) -> str:
    value = cfg.str("source", ENGINE).strip().lower()
    return PORTAINER if value.startswith("portainer") else ENGINE


def client_from(cfg: Config) -> Client:
    url, source, token = cfg.str("url"), source_of(cfg), cfg.str("token").strip()
    if not url.strip():
        raise PluginError("the address is required")
    if source == PORTAINER and not token:
        raise PluginError("Portainer needs an access token (My account → Access tokens)")
    return Client(
        url,
        source=source,
        token=token,
        **{**http_settings(cfg), "timeout": wait_s(cfg)},
    )


# --- parsing helpers -------------------------------------------------------


def mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", ":")
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else None


def counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def address(ip: Any) -> str | None:
    """A usable address in its short form (IPv6 too), else None."""
    if not isinstance(ip, str) or not ip.strip():
        return None
    try:
        a = ipaddress.ip_address(ip.strip().split("/")[0])
    except ValueError:
        return None
    if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
        return None
    return a.compressed


def is_ip(host: str | None) -> bool:
    try:
        ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return True


def optional(what: str, missing: list[str], fn: Callable[[], T], default: T) -> T:
    """Optional data: a section the socket proxy does not allow, or an older
    API without it, never fails the collection (the SDK's optional); what a
    refusal needs (the client's hint) goes in ``missing``."""
    return sdk_optional(what, fn, default, missing=missing)


# --- environments ------------------------------------------------------------


@dataclass
class Environment:
    """One Docker host: the Engine API itself, or one Portainer environment."""

    host: str  # its address (or name), in device keys
    prefix: str = ""  # path of its Engine API under the client's base
    name: str | None = None  # the Portainer environment's name

    @property
    def address(self) -> str | None:
        return self.host if is_ip(self.host) else None


def environment_host(endpoint: dict[str, Any], portainer_host: str) -> str:
    """tcp://10.0.0.5:9001 → 10.0.0.5. A local socket (unix://) is the host
    Portainer runs on; an Edge agent without an address is its environment
    ID (stable; its name, which can be edited, is shown instead)."""
    url = str(endpoint.get("URL") or "")
    if url.startswith(("unix://", "npipe://")):
        return portainer_host
    if "://" in url:
        host = urlsplit(url).hostname
        if host:
            return host
    return f"portainer-environment-{endpoint.get('Id')}"


def environments(c: Client, cfg: Config) -> list[Environment]:
    if c.source == ENGINE:
        return [Environment(host=c.host)]
    try:
        rows = c.get("/endpoints")
    except (Forbidden, NotFound, Unavailable) as e:
        raise PluginError(f"could not list the environments of {c.base}: {e}") from e
    if not isinstance(rows, list):
        raise PluginError(f"{c.base} does not look like Portainer (no environment list)")
    only = cfg.int("environment", 0) or 0
    out = []
    for row in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: r.get("Id") or 0):
        if only and row.get("Id") != only:
            continue
        if row.get("Type") not in DOCKER_ENVIRONMENTS:
            log.info("environment %s is not Docker: skipped", row.get("Name"))
            continue
        if row.get("Status") == 2:
            log.info("environment %s is down: skipped", row.get("Name"))
            continue
        out.append(
            Environment(
                host=environment_host(row, c.host),
                prefix=f"/endpoints/{row.get('Id')}/docker",
                name=str(row.get("Name") or "") or None,
            )
        )
    if only and not out:
        raise PluginError(
            f"Portainer has no Docker environment {only} that this token can see "
            "(leave the field empty to read them all)"
        )
    return out


# --- containers --------------------------------------------------------------


@dataclass
class Network:
    driver: str | None
    parent: str | None  # macvlan / ipvlan: the host's interface
    bridge: str | None  # bridge: the host's bridge (docker0)


def networks(rows: Any) -> dict[str, Network]:
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("Name"):
            continue
        opts = row.get("Options") or {}
        out[str(row["Name"])] = Network(
            driver=str(row.get("Driver") or "") or None,
            parent=opts.get("parent") or None,
            bridge=opts.get("com.docker.network.bridge.name") or None,
        )
    return out


def container_name(row: dict[str, Any]) -> str:
    """The container's name. A Swarm task (`stack_web.1.<task id>`, or
    `stack_agent.<node id>.<task id>` for a global service) is named after its
    service, with its replica number past the first: the task id changes on
    every deploy, the service and its slot do not."""
    labels = row.get("Labels") or {}
    service = str(labels.get("com.docker.swarm.service.name") or "")
    names = [str(n).lstrip("/") for n in row.get("Names") or [] if n]
    if service:
        task = str(labels.get("com.docker.swarm.task.name") or (names[0] if names else ""))
        parts = task.split(".")
        slot = parts[1] if len(parts) >= 3 and parts[1].isdigit() else ""
        return f"{service}.{slot}" if slot and slot != "1" else service
    return names[0] if names else str(row.get("Id") or "")[:12]


def own_networks(
    row: dict[str, Any], nets: dict[str, Network]
) -> list[tuple[str, dict[str, Any], Network]]:
    """The container's networks where it has an address of its own on the LAN
    (macvlan, ipvlan)."""
    out = []
    attached = (row.get("NetworkSettings") or {}).get("Networks") or {}
    for name, settings in sorted(attached.items()):
        net = nets.get(name)
        if not isinstance(settings, dict) or net is None or net.driver not in OWN_ADDRESS:
            continue
        if address(settings.get("IPAddress")) or address(settings.get("GlobalIPv6Address")):
            out.append((name, settings, net))
    return out


def network_ips(settings: dict[str, Any]) -> list[str]:
    """IPv4 then IPv6, with their prefixes."""
    out = []
    for ip_key, len_key in (
        ("IPAddress", "IPPrefixLen"),
        ("GlobalIPv6Address", "GlobalIPv6PrefixLen"),
    ):
        ip = address(settings.get(ip_key))
        if ip:
            plen = counter(settings.get(len_key))
            out.append(f"{ip}/{plen}" if plen else ip)
    return out


def port_services(row: dict[str, Any], own: bool) -> list[Service]:
    """Published ports (host port → container port); a container with its own
    address also listens on the ports it exposes, published or not."""
    seen: dict[tuple[int, str], Service] = {}
    for p in row.get("Ports") or []:
        if not isinstance(p, dict):
            continue
        proto = str(p.get("Type") or "tcp")
        private, public = counter(p.get("PrivatePort")), counter(p.get("PublicPort"))
        if public:
            bind = address(p.get("IP"))
            desc = f"published from container port {private}/{proto}"
            if bind:
                desc += f" on {bind}"
            seen.setdefault(
                (public, proto), Service(name=f"{public}/{proto}", description=desc, running=True)
            )
        elif own and private:
            seen.setdefault(
                (private, proto),
                Service(
                    name=f"{private}/{proto}", description="exposed by the container", running=True
                ),
            )
    return [seen[k] for k in sorted(seen)]


def image_name(image: Any) -> str | None:
    """The image without its digest ("jellyfin:latest@sha256:…" → "jellyfin:latest")."""
    name = str(image or "").split("@", 1)[0].strip()
    return name or None


def container_port(port: int, proto: str, target: int | None) -> Any:
    return ContainerPort(
        port=port,
        protocol=proto if proto in ("tcp", "udp", "sctp") else None,
        target_port=target or None,
    )


def published_ports(row: dict[str, Any]) -> list[Any]:
    """The ports a container publishes on its host's addresses (not on its
    loopback, where nothing else on the network reaches them)."""
    if not CONTAINER:
        return []
    seen: dict[tuple[int, str], Any] = {}
    for p in row.get("Ports") or []:
        if not isinstance(p, dict):
            continue
        public, private = counter(p.get("PublicPort")), counter(p.get("PrivatePort"))
        bind = str(p.get("IP") or "").strip()
        if not public or (bind and is_ip(bind) and ipaddress.ip_address(bind).is_loopback):
            continue
        proto = str(p.get("Type") or "tcp")
        seen.setdefault((public, proto), container_port(public, proto, private))
    return [seen[k] for k in sorted(seen)]


def with_container(**facts: Any) -> dict[str, Any]:
    """Device.container's keyword, on an SDK that has it."""
    if not CONTAINER:
        return {}
    facts = {k: v for k, v in facts.items() if v not in (None, "", [])}
    return {"container": Container(**facts)}


@dataclass
class Host:
    """What a container needs to know about the Docker host it runs on."""

    key: str
    name: str
    address: str | None


def container_device(
    row: dict[str, Any], nets: dict[str, Network], host: Host, env: Environment
) -> Device | None:
    """A running container that can be reached on its own, else None (it is
    then one of the host's services)."""
    own = own_networks(row, nets)
    services = port_services(row, bool(own))
    published = any(
        counter(p.get("PublicPort")) for p in row.get("Ports") or [] if isinstance(p, dict)
    )
    if not own and not published:
        return None
    name = container_name(row)
    interfaces, macs, ips = [], [], []
    for net_name, settings, net in own:
        # ipvlan shares the MAC of the host's interface: never the container's.
        m = mac(settings.get("MacAddress")) if net.driver == "macvlan" else None
        cidrs = network_ips(settings)
        interfaces.append(Interface(name=net_name, type="other", mac=m, ips=cidrs or None))
        if m and m not in macs:
            macs.append(m)
        ips += [c.split("/")[0] for c in cidrs if c.split("/")[0] not in ips]
    ips.sort(key=lambda ip: ":" in ip)  # IPv4 first, then IPv6
    if own:
        net_name, _, net = own[0]
        local, remote = net_name, net.parent or net_name
    else:
        # Published ports on a bridge: its addresses there are private to the
        # host (the same 172.17.0.x on every host) and are not reported.
        attached = (row.get("NetworkSettings") or {}).get("Networks") or {}
        net_name = next(iter(sorted(attached)), None) or str(
            (row.get("HostConfig") or {}).get("NetworkMode") or "bridge"
        )
        net = nets.get(net_name)
        local, remote = net_name, (net.bridge if net and net.bridge else net_name)
    return Device(
        key=macs[0] if macs else f"docker:{env.host}:{name}",
        name=name,
        host=ips[0] if ips else None,
        role="server",
        vendor="Docker",
        model="Docker container",
        os_version=str(row.get("Image") or "") or None,
        macs=macs or None,
        ips=ips or None,
        interfaces=interfaces or None,
        services=services or None,
        neighbors=[
            Neighbor(
                local_port=local,
                protocol="other",
                remote_name=host.name,
                remote_port=remote,
                remote_ip=host.address,
                remote_platform="Docker",
            )
        ],
        **with_container(
            name=name,
            image=image_name(row.get("Image")),
            stack=str((row.get("Labels") or {}).get("com.docker.compose.project") or "") or None,
            state=str(row.get("State") or "") or None,
            ports=published_ports(row),
        ),
    )


def host_service(row: dict[str, Any], running: bool) -> Service:
    mode = str((row.get("HostConfig") or {}).get("NetworkMode") or "")
    desc = str(row.get("Image") or "")
    if running and mode == "host":
        desc += " (host network)"
    return Service(name=container_name(row), description=desc or None, running=running)


# --- Swarm services ----------------------------------------------------------------


def is_manager(info: dict[str, Any]) -> bool:
    swarm = info.get("Swarm") or {}
    return swarm.get("LocalNodeState") == "active" and bool(swarm.get("ControlAvailable"))


@dataclass
class Swarm:
    """What a manager says about the swarm's services."""

    services: list[dict[str, Any]]
    tasks: list[dict[str, Any]]  # desired running
    nodes: dict[str, str]  # node ID → its address


def node_addresses(rows: Any) -> dict[str, str]:
    """The address of each ready node. A manager may give 0.0.0.0 as its
    address: its manager address (the one it listens on for the swarm) then."""
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("ID"):
            continue
        status = row.get("Status") or {}
        if status.get("State") not in (None, "ready"):
            continue
        addr = address(status.get("Addr"))
        if not addr:
            manager = str((row.get("ManagerStatus") or {}).get("Addr") or "")
            addr = address(urlsplit("//" + manager).hostname) if manager else None
        if addr:
            out[str(row["ID"])] = addr
    return out


def read_swarm(c: Client, env: Environment, missing: list[str]) -> Swarm | None:
    """A manager's services (essential to the rest), their running tasks and
    the swarm's nodes (each optional: SERVICES=1, TASKS=1, NODES=1)."""
    services = optional(
        "services",
        missing,
        lambda: c.get(env.prefix + "/services", params={"status": "true"}),
        None,
    )
    if not isinstance(services, list):
        return None
    tasks = optional(
        "tasks",
        missing,
        lambda: c.get(
            env.prefix + "/tasks",
            params={"filters": json.dumps({"desired-state": ["running"]})},
        ),
        [],
    )
    nodes = optional("nodes", missing, lambda: c.get(env.prefix + "/nodes"), [])
    return Swarm(
        services=[s for s in services if isinstance(s, dict)],
        tasks=[t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else [],
        nodes=node_addresses(nodes),
    )


def service_ports(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """The ports a service publishes: what the swarm applied (Endpoint),
    else what its spec asks for."""
    ports = (svc.get("Endpoint") or {}).get("Ports") or (
        (svc.get("Spec") or {}).get("EndpointSpec") or {}
    ).get("Ports")
    return [p for p in ports or [] if isinstance(p, dict) and counter(p.get("PublishedPort"))]


def service_device(
    svc: dict[str, Any], swarm: Swarm, host: Host, env: Environment
) -> Device | None:
    """A service that publishes ports, else None. Through the routing mesh
    (ingress) its ports answer on every node of the swarm; published in host
    mode, on the nodes that run it."""
    spec = svc.get("Spec") or {}
    name = str(spec.get("Name") or "")
    ports = service_ports(svc)
    if not name or not ports:
        return None
    tasks = [t for t in swarm.tasks if t.get("ServiceID") == svc.get("ID")]
    status = svc.get("ServiceStatus") or {}
    mode = spec.get("Mode") or {}
    replicas = counter(((mode.get("Replicated") or {}) or {}).get("Replicas"))
    if replicas is None:
        replicas = counter(status.get("DesiredTasks"))
        if replicas is None and "Global" in mode and swarm.tasks:
            replicas = len(tasks)
    running = counter(status.get("RunningTasks"))
    if running is None and swarm.tasks:
        running = sum(1 for t in tasks if (t.get("Status") or {}).get("State") == "running")
    image = image_name(((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Image"))
    stack = str((spec.get("Labels") or {}).get("com.docker.stack.namespace") or "") or None
    mesh = any(str(p.get("PublishMode") or "ingress") == "ingress" for p in ports)
    if mesh:
        where = set(swarm.nodes.values())
    else:
        nodes = {str(t.get("NodeID")) for t in tasks}
        where = {addr for node, addr in swarm.nodes.items() if node in nodes}
    where.discard(host.address or "")
    published, services = {}, {}
    for p in ports:
        port, target = counter(p.get("PublishedPort")), counter(p.get("TargetPort"))
        proto = str(p.get("Protocol") or "tcp")
        published.setdefault((port, proto), container_port(port, proto, target))
        services.setdefault(
            (port, proto),
            Service(
                name=f"{port}/{proto}",
                description=f"published from container port {target or port}/{proto}"
                + ("" if str(p.get("PublishMode") or "ingress") == "ingress" else " (host mode)"),
                running=running is None or running > 0,
            ),
        )
    return Device(
        key=f"docker:{env.host}:{name}",
        name=name,
        role="server",
        vendor="Docker",
        model="Docker Swarm service",
        os_version=image,
        services=[services[k] for k in sorted(services)],
        neighbors=[
            Neighbor(
                local_port="ingress" if mesh else "host",
                protocol="other",
                remote_name=host.name,
                remote_ip=host.address,
                remote_platform="Docker",
            )
        ],
        **with_container(
            name=name,
            image=image,
            stack=stack,
            state="running" if running else None,
            replicas=replicas,
            running=running,
            ports=[published[k] for k in sorted(published)],
            addresses=sorted(where, key=lambda ip: (":" in ip, ipaddress.ip_address(ip))),
        ),
    )


def service_of(row: dict[str, Any]) -> str:
    return str((row.get("Labels") or {}).get("com.docker.swarm.service.name") or "")


# --- a host and its containers ---------------------------------------------------


def read_containers(c: Client, env: Environment) -> list[dict[str, Any]]:
    """Every container, running or not (all=1). Essential: without it there is
    nothing to report."""
    try:
        rows = c.get(env.prefix + "/containers/json", params={"all": "1"})
    except Forbidden as e:
        if c.source == ENGINE:
            raise PluginError(
                f"{c.base} does not allow reading containers: {SECTIONS['containers']}"
            ) from e
        raise PluginError(
            f"the Portainer user cannot see the containers of {env.name or env.host}"
        ) from e
    except (NotFound, Unavailable) as e:
        raise PluginError(f"could not list the containers of {env.name or env.host}: {e}") from e
    if not isinstance(rows, list):
        raise PluginError(f"{c.base} does not look like a Docker API (no container list)")
    return [r for r in rows if isinstance(r, dict)]


def environment_devices(c: Client, env: Environment, missing: list[str]) -> list[Device]:
    info = optional("info", missing, lambda: c.get(env.prefix + "/info"), {}) or {}
    rows = read_containers(c, env)
    nets = networks(optional("networks", missing, lambda: c.get(env.prefix + "/networks"), []))
    name = str(info.get("Name") or env.name or env.host)
    host = Host(key=f"docker:{env.host}", name=name, address=env.address)
    devices, services = [], []
    # A manager's services that publish ports are devices of their own (read
    # once, from the manager): their tasks here are no containers of their own.
    swarm = read_swarm(c, env, missing) if CONTAINER and is_manager(info) else None
    published: set[str] = set()
    for svc in swarm.services if swarm else []:
        device = service_device(svc, swarm, host, env)
        if device is not None:
            devices.append(device)
            published.add(device.name)
    devices.sort(key=lambda d: d.name)
    for row in sorted(rows, key=container_name):
        if service_of(row) in published:
            continue
        running = row.get("State") == "running"
        device = container_device(row, nets, host, env) if running else None
        if device is not None:
            devices.append(device)
        else:
            services.append(host_service(row, running))
    host_device = Device(
        key=host.key,
        name=name,
        host=env.address,
        role="server",
        vendor="Docker",
        os_version=str(info.get("OperatingSystem") or "") or None,
        cpu_count=counter(info.get("NCPU")) or None,
        mem_total_bytes=counter(info.get("MemTotal")),
        ips=[address(env.host)] if address(env.host) else None,
        services=services or None,
    )
    return [host_device, *devices]


# --- plugin entry points -------------------------------------------------------


def collect(cfg: Config) -> list[Device]:
    c = client_from(cfg)
    try:
        envs = environments(c, cfg)
        missing: list[str] = []
        devices: list[Device] = []
        for env in envs:
            try:
                devices += environment_devices(c, env, missing)
            except PluginError:
                if c.source == ENGINE or len(envs) == 1:
                    raise
                log.warning("could not read environment %s", env.name or env.host)
            except Exception:  # one environment failing never breaks the others
                log.exception("could not read environment %s", env.name or env.host)
        if missing:
            log.warning("not allowed: %s", "; ".join(sorted(set(missing))))
        return devices
    finally:
        c.close()


def portainer_version(c: Client) -> str | None:
    for path in ("/system/status", "/status"):  # 2.18+, then older releases
        try:
            status = c.get(path)
        except (Forbidden, NotFound, Unavailable):
            continue
        if isinstance(status, dict) and status.get("Version"):
            return str(status["Version"])
    return None


def test(cfg: Config) -> str:
    c = client_from(cfg)
    try:
        missing: list[str] = []
        if c.source == ENGINE:
            try:
                version = c.get("/version")
            except Unavailable as e:
                if e.status is None:  # out of reach, or no answer in time
                    raise
                raise PluginError(f"{c.base} does not look like a Docker API (no version)") from e
            except (Forbidden, NotFound) as e:
                raise PluginError(f"{c.base} does not look like a Docker API (no version)") from e
            if not isinstance(version, dict) or not version.get("ApiVersion"):
                raise PluginError(f"{c.base} does not look like a Docker API (no version)")
            label = f"Docker Engine {version.get('Version') or ''}".strip()
        else:
            label = f"Portainer {portainer_version(c) or ''}".strip()
        envs = environments(c, cfg)
        running = own = services = 0
        for env in envs:
            for device in environment_devices(c, env, missing):
                if device.key.startswith("docker:") and device.neighbors is None:
                    running += sum(1 for s in device.services or [] if s.running)
                elif device.model == "Docker Swarm service":
                    services += 1
                else:
                    running += 1
                    own += 1
        where = (
            f"{len(envs)} Docker environment{'s' if len(envs) != 1 else ''}, "
            if c.source == PORTAINER
            else ""
        )
        msg = (
            f"Connected to {label}: {where}{running} running container{'s' if running != 1 else ''}"
            f" ({own} reachable on their own)"
        )
        if services:
            msg += f", {services} Swarm service{'s' if services != 1 else ''} publishing ports"
        if missing:
            msg += ". Not allowed: " + "; ".join(sorted(set(missing)))
        return msg
    finally:
        c.close()
