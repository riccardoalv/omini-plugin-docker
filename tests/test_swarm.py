"""A Swarm manager (the maintainer's "master" VM, anonymized): its services
are read once, from the manager, with the ports they publish."""

import json
from pathlib import Path

import pytest
from omini_sdk import Config

import omini_docker.collect as collect_module
from omini_docker.collect import collect
from omini_docker.collect import test as connection_test

SWARM = Path(__file__).parent / "fixtures" / "swarm"

needs_container = pytest.mark.skipif(
    not collect_module.CONTAINER, reason="the SDK has no Device.container yet"
)


@pytest.fixture
def swarm(docker):
    """GET /info of a manager, /services, /tasks, /nodes and the containers
    that run on it (Swarm tasks and one Compose container)."""
    for name in ("info", "services", "tasks", "nodes"):
        docker.engine["/" + name] = json.loads((SWARM / f"{name}.json").read_text())
    docker.engine["/containers/json"] = json.loads((SWARM / "containers.json").read_text())
    docker.engine["/networks"] = []
    return docker


@pytest.fixture
def master():
    return Config({"source": "Docker Engine", "url": "http://192.168.1.42:2375"})


def by_name(devices):
    return {d.name: d for d in devices}


@needs_container
def test_a_service_that_publishes_ports_is_one_device(swarm, master):
    devices = by_name(collect(master))
    assert list(devices) == [
        "master",
        "homelab_alloy",
        "homelab_flaresolverr",
        "homelab_jellyfin",
        "homelab_nginx-proxy-manager",
        "homelab_portainer",
        "openspeedtest",
    ]
    jf = devices["homelab_jellyfin"]
    assert jf.key == "docker:192.168.1.42:homelab_jellyfin"
    assert jf.model == "Docker Swarm service" and jf.vendor == "Docker"
    assert jf.host is None and jf.ips is None and jf.macs is None  # its host's addresses
    [nb] = jf.neighbors
    assert (nb.protocol, nb.local_port, nb.remote_name, nb.remote_ip) == (
        "other",
        "ingress",
        "master",
        "192.168.1.42",
    )
    assert [(s.name, s.description, s.running) for s in jf.services] == [
        ("8096/tcp", "published from container port 8096/tcp", True)
    ]
    ct = jf.container
    assert ct.name == "homelab_jellyfin" and ct.stack == "homelab"
    assert ct.image == "lscr.io/linuxserver/jellyfin:latest"  # without its digest
    assert (ct.state, ct.running, ct.replicas) == ("running", 1, 1)
    assert [(p.port, p.protocol, p.target_port) for p in ct.ports] == [(8096, "tcp", 8096)]
    # The routing mesh: every ready node (the manager's 0.0.0.0 is its own address).
    assert ct.addresses == ["192.168.1.43"]


@needs_container
def test_routing_mesh_tasks_are_no_containers_of_their_own(swarm, master):
    devices = by_name(collect(master))
    host = devices["master"]
    # A service without published ports: its task stays the host's service,
    # named after its service.
    assert [s.name for s in host.services] == ["homelab_redis"]
    assert not any("." in name for name in devices)  # no task names


@needs_container
def test_replicas_come_from_the_tasks_without_service_status(swarm, master):
    devices = by_name(collect(master))
    portainer = devices["homelab_portainer"].container
    assert (portainer.running, portainer.replicas) == (1, 1)
    assert [p.port for p in portainer.ports] == [8000, 9000]
    alloy = devices["homelab_alloy"].container  # global, published in host mode
    assert (alloy.running, alloy.replicas) == (2, 2)
    assert alloy.addresses == ["192.168.1.43"]  # the nodes that run it
    assert [(p.port, p.protocol) for p in alloy.ports] == [(1514, "udp"), (1515, "tcp")]
    assert devices["homelab_alloy"].services[0].description == (
        "published from container port 1514/udp (host mode)"
    )
    npm = devices["homelab_nginx-proxy-manager"].container
    assert npm.addresses is None  # host mode, runs only on the manager
    assert devices["homelab_nginx-proxy-manager"].neighbors[0].local_port == "host"


@needs_container
def test_a_service_with_no_replica_running(swarm, master):
    fs = by_name(collect(master))["homelab_flaresolverr"]
    assert (fs.container.running, fs.container.replicas, fs.container.state) == (0, 1, None)
    assert fs.services[0].running is False


@needs_container
def test_a_compose_container_says_what_it_publishes(swarm, master):
    ct = by_name(collect(master))["openspeedtest"]
    assert ct.key == "docker:192.168.1.42:openspeedtest" and ct.model == "Docker container"
    c = ct.container
    assert (c.name, c.image, c.stack, c.state) == (
        "openspeedtest",
        "openspeedtest/latest",
        "speed",
        "running",
    )
    # 3002 is published on the host's loopback only: no one else reaches it.
    assert [(p.port, p.protocol, p.target_port) for p in c.ports] == [(3000, "tcp", 3000)]


@needs_container
def test_services_are_optional(swarm, master):
    swarm.errors["/services"] = 403  # a socket proxy without SERVICES=1
    devices = by_name(collect(master))
    # As without Swarm: tasks with ports on this node are containers of their own.
    assert "homelab_jellyfin" not in devices
    assert devices["homelab_nginx-proxy-manager"].model == "Docker container"
    assert "homelab_jellyfin" in [s.name for s in devices["master"].services]
    assert "Not allowed: SERVICES=1 on the socket proxy" in connection_test(master)


@needs_container
def test_tasks_and_nodes_are_optional(swarm, master):
    swarm.errors["/tasks"] = 403
    swarm.errors["/nodes"] = 403
    devices = by_name(collect(master))
    portainer = devices["homelab_portainer"].container
    assert portainer.running is None and portainer.replicas == 1  # unknown, never invented
    assert devices["homelab_jellyfin"].container.running == 1  # from the service's status
    assert devices["homelab_jellyfin"].container.addresses is None
    msg = connection_test(master)
    assert "TASKS=1 on the socket proxy" in msg and "NODES=1 on the socket proxy" in msg


@needs_container
def test_a_worker_lists_no_services(swarm, master):
    swarm.engine["/info"]["Swarm"]["ControlAvailable"] = False
    collect(master)
    assert "/services" not in swarm.calls


@needs_container
def test_connection_test_counts_services(swarm, master):
    assert connection_test(master) == (
        "Connected to Docker Engine 27.3.1: 2 running containers (1 reachable on their own),"
        " 5 Swarm services publishing ports"
    )


def test_an_older_sdk_reads_no_services(swarm, master, monkeypatch):
    monkeypatch.setattr(collect_module, "CONTAINER", False)
    devices = by_name(collect(master))
    assert "/services" not in swarm.calls
    assert "homelab_jellyfin" not in devices
    assert all(getattr(d, "container", None) is None for d in devices.values())


@needs_container
def test_through_portainer(swarm, portainer_cfg):
    swarm.environments["1"] = swarm.engine
    devices = collect(portainer_cfg)
    jf = by_name(devices)["homelab_jellyfin"]
    assert jf.key == "docker:192.168.1.50:homelab_jellyfin"
    assert "/api/endpoints/1/docker/services" in swarm.calls
