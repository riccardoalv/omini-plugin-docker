import json

import httpx
import pytest
from omini_sdk import Config, PluginError
from omini_sdk.plugin import Plugin

from omini_docker.client import ENGINE, PORTAINER, base_url
from omini_docker.collect import collect, wait_s
from omini_docker.collect import test as connection_test


def by_name(devices):
    return {d.name: d for d in devices}


def test_reads_the_host(docker, cfg):
    devices = by_name(collect(cfg))
    # Reachable containers only; the others are the host's services.
    assert list(devices) == ["docker1", "adguard", "jellyfin", "pihole"]
    host = devices["docker1"]
    assert host.key == "docker:192.168.1.50"  # the Engine API exposes no MAC
    assert host.role == "server" and host.host == "192.168.1.50"
    assert host.vendor == "Docker" and devices["pihole"].vendor == "Docker"
    assert host.ips == ["192.168.1.50"] and host.macs is None
    assert host.os_version == "Ubuntu 24.04.1 LTS"
    assert (host.cpu_count, host.mem_total_bytes) == (4, 16649183232)
    assert host.storage is None  # /system/df is not read: it is slow


def test_containers_without_an_address_are_host_services(docker, cfg):
    host = by_name(collect(cfg))["docker1"]
    services = {s.name: s for s in host.services}
    assert list(services) == ["app-db-1", "homeassistant", "old-nginx"]
    assert services["app-db-1"].running is True
    assert services["app-db-1"].description == "postgres:16"
    assert services["homeassistant"].description.endswith("(host network)")
    assert services["old-nginx"].running is False  # stopped: never a device


def test_a_macvlan_container_is_keyed_by_its_mac(docker, cfg):
    ct = by_name(collect(cfg))["pihole"]
    assert ct.key == "02:42:c0:a8:01:35"  # normalized
    assert ct.model == "Docker container" and ct.os_version == "pihole/pihole:2024.07.0"
    assert ct.role == "server"
    assert [m.root for m in ct.macs] == ["02:42:c0:a8:01:35"]
    assert ct.ips == ["192.168.1.53", "2001:db8:1::53"]  # IPv6 kept, in its short form
    assert ct.host == "192.168.1.53"
    [iface] = ct.interfaces
    assert (iface.name, iface.mac, iface.ips) == (
        "lan",
        "02:42:c0:a8:01:35",
        ["192.168.1.53/24", "2001:db8:1::53/64"],
    )
    assert [s.name for s in ct.services] == ["53/tcp", "53/udp", "80/tcp"]
    [nb] = ct.neighbors
    assert (nb.protocol, nb.local_port, nb.remote_port) == ("other", "lan", "eth0")
    assert (nb.remote_name, nb.remote_ip, nb.remote_mac) == ("docker1", "192.168.1.50", None)


def test_an_ipvlan_container_has_no_mac_of_its_own(docker, cfg):
    ct = by_name(collect(cfg))["adguard"]
    assert ct.key == "docker:192.168.1.50:adguard"  # it shares the host's MAC
    assert ct.macs is None and ct.ips == ["10.0.30.5"]
    assert ct.neighbors[0].remote_port == "eth0.30"


def test_published_ports_make_a_device_without_bridge_addresses(docker, cfg):
    ct = by_name(collect(cfg))["jellyfin"]
    assert ct.key == "docker:192.168.1.50:jellyfin"
    # 172.17.0.2 and 02:42:ac:11:00:02 exist on every Docker host: never reported.
    assert ct.ips is None and ct.macs is None and ct.host is None and ct.interfaces is None
    services = {s.name: s.description for s in ct.services}
    assert services == {
        "1900/udp": "published from container port 1900/udp on 192.168.1.50",
        "8096/tcp": "published from container port 8096/tcp",  # 0.0.0.0 and :: once
    }
    [nb] = ct.neighbors
    assert (nb.local_port, nb.remote_port, nb.remote_name) == ("bridge", "docker0", "docker1")


def test_without_the_networks_section_only_published_ports_count(docker, cfg):
    docker.errors["/networks"] = 403  # a socket proxy without NETWORKS=1
    devices = by_name(collect(cfg))
    assert list(devices) == ["docker1", "jellyfin"]
    services = [s.name for s in devices["docker1"].services]
    assert "pihole" in services and "adguard" in services
    msg = connection_test(cfg)
    assert "Not allowed: NETWORKS=1 on the socket proxy" in msg


def test_without_the_info_section_the_host_is_named_by_its_address(docker, cfg):
    docker.errors["/info"] = 403
    host = collect(cfg)[0]
    assert host.name == "192.168.1.50" and host.cpu_count is None
    assert host.os_version is None  # unknown, never invented


def test_containers_are_essential(docker, cfg):
    docker.errors["/containers/"] = 403
    with pytest.raises(PluginError, match="CONTAINERS=1"):
        collect(cfg)


def test_only_reads(docker, cfg, portainer_cfg):
    collect(cfg)
    connection_test(cfg)
    collect(portainer_cfg)
    connection_test(portainer_cfg)
    assert docker.methods == {"GET"}


def test_connection_test(docker, cfg):
    assert connection_test(cfg) == (
        "Connected to Docker Engine 27.3.1: 5 running containers (3 reachable on their own)"
    )


def test_not_docker(docker, cfg):
    docker.engine.pop("/version")
    with pytest.raises(PluginError, match="does not look like a Docker API"):
        connection_test(cfg)


def test_unreachable_host(cfg, monkeypatch):
    pauses = []
    monkeypatch.setattr("time.sleep", pauses.append)
    cfg["url"] = "http://127.0.0.1:9"
    with pytest.raises(PluginError, match="cannot connect"):
        connection_test(cfg)
    assert pauses == [1.0, 2.0]  # a refused connection is tried twice more


def with_transport(monkeypatch, handler):
    from functools import partial

    import omini_docker.collect as collect_module
    from omini_docker.client import Client

    monkeypatch.setattr(
        collect_module, "Client", partial(Client, transport=httpx.MockTransport(handler))
    )


def test_a_timeout_is_an_error_the_user_can_read(monkeypatch, cfg):
    monkeypatch.setattr("time.sleep", lambda _s: None)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with_transport(monkeypatch, slow)
    with pytest.raises(PluginError, match="did not answer in time"):
        collect(cfg)
    with pytest.raises(PluginError, match="did not answer in time"):
        connection_test(cfg)  # not "does not look like a Docker API"


def test_a_busy_api_is_asked_again(docker, cfg, monkeypatch):
    pauses = []
    monkeypatch.setattr("time.sleep", pauses.append)
    busy = {"left": 1}

    def flaky(request):
        if busy["left"]:
            busy["left"] -= 1
            return httpx.Response(503)
        return docker.handler(request)

    with_transport(monkeypatch, flaky)
    assert by_name(collect(cfg))["docker1"].cpu_count == 4
    assert pauses == [1.0]


def test_an_optional_section_that_fails_is_skipped(docker, cfg):
    docker.errors["/info"] = 500
    docker.errors["/networks"] = 400  # answered otherwise: not a bug of the plugin
    devices = by_name(collect(cfg))
    assert list(devices) == ["192.168.1.50", "jellyfin"]
    assert devices["192.168.1.50"].cpu_count is None
    assert "Not allowed" not in connection_test(cfg)


# --- Portainer -----------------------------------------------------------------


def test_portainer_reads_every_docker_environment(docker, portainer_cfg):
    devices = collect(portainer_cfg)
    keys = [d.key for d in devices]
    assert keys == [
        "docker:192.168.1.50",  # local socket: the host Portainer runs on
        "docker:192.168.1.50:adguard",
        "docker:192.168.1.50:jellyfin",
        "02:42:c0:a8:01:35",
        "docker:192.168.1.60",  # the agent's address
        "docker:192.168.1.60:jellyfin",
    ]
    # Kubernetes (3) and down (4) environments are not read.
    assert not any(c.startswith(("/api/endpoints/3/", "/api/endpoints/4/")) for c in docker.calls)
    nas = by_name(devices[4:])["jellyfin"]
    assert nas.neighbors[0].remote_name == "nas"
    assert nas.neighbors[0].remote_ip == "192.168.1.60"


def test_portainer_one_environment(docker, portainer_cfg):
    portainer_cfg["environment"] = 2
    devices = collect(portainer_cfg)
    assert [d.key for d in devices] == ["docker:192.168.1.60", "docker:192.168.1.60:jellyfin"]
    portainer_cfg["environment"] = 3
    with pytest.raises(PluginError, match="no Docker environment 3"):
        collect(portainer_cfg)


def test_portainer_one_environment_failing_does_not_break_the_others(docker, portainer_cfg):
    docker.errors["/api/endpoints/2/docker/containers/"] = 502
    keys = [d.key for d in collect(portainer_cfg)]
    assert "docker:192.168.1.60" not in keys and "docker:192.168.1.50" in keys


def test_portainer_connection_test(docker, portainer_cfg):
    assert connection_test(portainer_cfg) == (
        "Connected to Portainer 2.21.4: 2 Docker environments, "
        "6 running containers (4 reachable on their own)"
    )


def test_portainer_errors_the_user_can_act_on(docker, portainer_cfg):
    docker.token = "ptr_other"
    with pytest.raises(PluginError, match="rejected the access token") as e:
        connection_test(portainer_cfg)
    assert portainer_cfg["token"] not in str(e.value)
    portainer_cfg["token"] = ""
    with pytest.raises(PluginError, match="needs an access token"):
        collect(portainer_cfg)


def test_portainer_names_the_environment_a_user_cannot_read(docker, portainer_cfg):
    docker.errors["/api/endpoints/2/docker/networks"] = 403
    msg = connection_test(portainer_cfg)
    assert msg.endswith(
        "Not allowed: access to Portainer environment 2 (Environments → Manage access)"
    )


def test_an_edge_agent_without_an_address_is_keyed_by_its_id(docker, portainer_cfg):
    from omini_docker.collect import environment_host

    edge = {"Id": 7, "Name": "Office NAS", "URL": ""}
    assert environment_host(edge, "192.168.1.50") == "portainer-environment-7"


def test_portainer_user_without_access(docker, portainer_cfg):
    docker.errors["/api/endpoints/1/docker/containers/"] = 403
    portainer_cfg["environment"] = 1
    with pytest.raises(PluginError, match="cannot see the containers of local"):
        collect(portainer_cfg)


# --- form and protocol -------------------------------------------------------------


def test_addresses():
    assert base_url("192.168.1.50", ENGINE) == "http://192.168.1.50:2375"
    assert base_url("https://192.168.1.50", ENGINE) == "https://192.168.1.50:2376"
    assert base_url("tcp://192.168.1.50:2375", ENGINE) == "http://192.168.1.50:2375"
    assert base_url("[fd00::50]", ENGINE) == "http://[fd00::50]:2375"
    assert base_url("192.168.1.50", PORTAINER) == "https://192.168.1.50:9443"
    assert base_url("https://portainer.lan/api/", PORTAINER) == "https://portainer.lan:9443"
    with pytest.raises(PluginError, match="http:// or https://"):
        base_url("ftp://x", ENGINE)


def test_an_ipv6_host(docker, cfg):
    cfg["url"] = "http://[fd00::50]:2375"
    devices = by_name(collect(cfg))
    assert devices["docker1"].key == "docker:fd00::50" and devices["docker1"].host == "fd00::50"
    assert devices["pihole"].neighbors[0].remote_ip == "fd00::50"


def test_required_fields(docker, cfg):
    cfg["url"] = ""
    with pytest.raises(PluginError, match="required"):
        collect(cfg)


def test_waits_as_long_as_asked(cfg):
    assert wait_s(cfg) == 15  # the SDK's default, as in every plugin
    assert wait_s(Config({"timeout_s": 90}, None)) == 90
    assert wait_s(Config({"timeout_s": 1}, None)) == 5
    assert wait_s(Config({"timeout_s": 999}, None)) == 120


def test_the_sdk_accepts_the_devices(docker, monkeypatch):
    """The whole request/response cycle, with the SDK's model validation."""
    import omini_docker.collect as collect_module

    p = Plugin()
    p.collect(collect_module.collect)
    request = json.dumps(
        {
            "protocol": 1,
            "action": "collect",
            "config": {"source": "Docker Engine", "url": "http://192.168.1.50:2375"},
            "state_dir": "/tmp/omini-docker-test",
        }
    )
    response, code = p.handle(request)
    assert code == 0 and "error" not in response
    assert len(response["devices"]) == 4


def test_manifest_groups_the_advanced_fields():
    from pathlib import Path

    manifest = (Path(__file__).parent.parent / "plugin.yaml").read_text()
    assert "\nchanges:" not in manifest and "\nbackup:" not in manifest  # read-only
    fields = manifest.split("\nfields:", 1)[1].split("\n  - key: ")[1:]
    groups = {f.split("\n", 1)[0]: "group: Advanced" in f for f in fields}
    assert groups == {
        "source": False,
        "url": False,
        "token": False,
        "environment": False,
        "verify_tls": True,
        "timeout_s": True,
    }


def test_swarm_tasks_are_named_after_their_service():
    from omini_docker.collect import container_name

    def task(name, service):
        return {
            "Names": [f"/{name}"],
            "Labels": {
                "com.docker.swarm.service.name": service,
                "com.docker.swarm.task.name": name,
            },
        }

    # A replicated service's first replica, its second, and a global service.
    assert (
        container_name(task("homelab_portainer.1.eg4nowpx304rxxvt23mybj4zv", "homelab_portainer"))
        == "homelab_portainer"
    )
    assert (
        container_name(task("homelab_web.2.4r0zt3ko9yhetqnc53kzedm7o", "homelab_web"))
        == "homelab_web.2"
    )
    assert (
        container_name(
            task(
                "homelab_alloy.qqccdqlxxpeel4xstprdsfnnq.mjxqj1561hw6e66rp4265oz5s", "homelab_alloy"
            )
        )
        == "homelab_alloy"
    )
    # Not a Swarm task: its own name.
    assert container_name({"Names": ["/jellyfin"], "Labels": {}}) == "jellyfin"
