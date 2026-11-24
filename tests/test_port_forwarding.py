from __future__ import annotations

import asyncio
import json
import shlex

import pytest
from fastapi.testclient import TestClient

from app import eu_install, main, port_forwarding as forwarding, port_forwarding_remote as remote
from app.db import create_client, create_operation, get_db, get_operation, get_setting, init_db, set_setting
from app.operation_coordinator import OperationBusyError, acquire_vps_lease
from app.vpn_config import capture_vpn_config
from test_sprintbox import NEW, OLD, run, scenario  # reusable fake Sprintbox API


def config(client_id, *ports, protocol="UDP"):
    return {
        "enabled": True,
        "groups": [{
            "name": "FarCry4", "enabled": True,
            "rules": [{
                "enabled": True, "protocol": protocol, "external": port,
                "internal": port, "client_id": client_id, "device_id": None,
            } for port in ports],
        }],
    }


def submit(revision, value):
    edit = forwarding.ForwardingEdit.model_validate({"revision": revision, "config": value})
    return asyncio.run(forwarding.save_and_apply(edit))


@pytest.fixture
def panel(monkeypatch):
    init_db()
    for key, value in {
        "current_ip": OLD, "config.amnezia_enabled": "1", "config.amnezia_port": "51820",
        "config.admin_username": "admin", "config.admin_password": "test-password", "setup.complete": "1",
    }.items():
        set_setting(key, value)
    client = create_client("Player")
    requests = []

    async def ssh(host, command, stdin_data, timeout):
        assert host == OLD and command == "bash -s"
        manifest = stdin_data.split("<<'AUTOVPN_PORT_FORWARDING_JSON'\n")[1].splitlines()[0]
        requests.append(json.loads(manifest))
        return 0, "AUTOVPN_PORT_FORWARDING_OK\n"

    monkeypatch.setattr(eu_install, "run_remote_command", ssh)
    return client, requests


def test_admin_creates_reapplies_disables_and_deletes_rules(panel):
    client, requests = panel
    path = "/admin/port-forwarding"
    with TestClient(main.app) as http:
        assert http.get(path).status_code == 401
        auth = ("admin", "test-password")
        page = http.get(path, auth=auth)
        assert page.status_code == 200 and "Проброс портов" in page.text
        assert client["amnezia_private_key"] not in page.text
        value = config(client["id"], 9000, 9103)
        edit = {"revision": 0, "config": value}
        assert http.post(path, auth=auth, json=edit, headers={"Origin": "https://other.example"}).status_code == 403
        response = http.post(path, auth=auth, json=edit)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["state"] == "APPLIED"
        assert requests[0]["public_ip"] == OLD
        assert [(rule["external"], rule["client_ip"], rule["peer"]) for rule in requests[0]["rules"]] == [
            (port, client["amnezia_ipv4"], client["amnezia_public_key"]) for port in (9000, 9103)
        ]
        assert http.post(path, auth=auth, json=edit).status_code == 409
    submit(1, value)
    assert requests[0] == requests[1]
    value["groups"][0]["rules"][0]["enabled"] = False
    submit(2, value)
    assert [rule["external"] for rule in requests[-1]["rules"]] == [9103]
    value["groups"][0]["enabled"] = False
    saved = submit(3, value)
    assert requests[-1]["rules"] == []
    assert len(saved["config"]["groups"][0]["rules"]) == 2
    value["groups"].clear()
    assert submit(4, value)["config"]["groups"] == []
    assert requests[-1]["rules"] == []


def test_conflicts_and_shared_vps_lease(panel):
    client, requests = panel
    with pytest.raises(forwarding.PortForwardingError, match="AmneziaWG"):
        submit(0, config(client["id"], 51820, protocol="UDP + TCP"))
    value = config(client["id"], 9000, 9000)
    value["groups"][0]["rules"][1]["protocol"] = "UDP + TCP"
    with pytest.raises(forwarding.PortForwardingError, match="UDP 9000"):
        submit(0, value)
    assert requests == []
    with get_db() as db:
        acquire_vps_lease(db, "IP_CHANGE", 100)
    with pytest.raises(OperationBusyError):
        submit(0, config(client["id"], 9000))
    assert forwarding.view()["revision"] == 0


def test_device_owner_and_remote_errors(panel, monkeypatch):
    client, requests = panel
    value = config(client["id"], 9000)
    value["groups"][0]["rules"][0]["device_id"] = 999
    result = submit(0, value)["result"]
    assert result["state"] == "PARTIAL"
    assert "устройство отсутствует" in result["rules"]["0.0"]
    assert requests == []

    async def ssh(*args, **kwargs):
        return 1, "IPv4 не закреплён за нужным peer"

    monkeypatch.setattr(eu_install, "run_remote_command", ssh)
    result = submit(1, config(client["id"], 9000))["result"]
    assert result["state"] == "ERROR" and "peer" in result["error"]


@pytest.mark.parametrize("failure", [False, True])
def test_sprintbox_applies_forwarding_before_publishing(scenario, monkeypatch, failure):
    set_setting("config.amnezia_enabled", "1")
    client = capture_vpn_config().enabled_clients[0]
    value = config(client.id, 9000, 9103)
    set_setting(forwarding.CONFIG_KEY, json.dumps({"revision": 1, "config": value}))
    calls = []

    async def ssh(host, command, stdin_data, timeout):
        assert host == NEW
        assert get_setting("current_ip") == OLD
        assert scenario.events[-2:] == ["deploy", "health"]
        manifest = stdin_data.split("<<'AUTOVPN_PORT_FORWARDING_JSON'\n")[1].splitlines()[0]
        calls.append(json.loads(manifest))
        if failure:
            return 1, "UDP 9103: порт занят"
        return 0, "AUTOVPN_PORT_FORWARDING_OK\n"

    monkeypatch.setattr(eu_install, "run_remote_command", ssh)
    operation = create_operation()
    run(operation)
    assert len(calls) == 1
    assert calls[0]["public_ip"] == NEW
    assert {rule["external"] for rule in calls[0]["rules"]} == {9000, 9103}
    if failure:
        assert get_setting("current_ip") == OLD
        assert "UDP 9103: порт занят" in get_operation(operation)["error_message"]
        assert not forwarding.view()["result"]
    else:
        assert get_setting("current_ip") == NEW
        assert forwarding.view()["result"]["state"] == "APPLIED"
    assert "delete" not in scenario.events


class Firewall:
    def __init__(self):
        self.tables = {
            "filter": {"FORWARD": [["-p", "icmp", "-j", "DROP"]]},
            "nat": {"PREROUTING": [], "OUTPUT": [], "POSTROUTING": [["-j", "MASQUERADE"]]},
        }
        self.commits = 0
        self.fail_table = None

    def __call__(self, *args, data=None):
        if args[0] == "iptables-save":
            chains = self.tables[args[-1]]
            lines = [f":{name} - [0:0]" for name in chains]
            lines.extend(shlex.join(["-A", name, *rule]) for name, rules in chains.items() for rule in rules)
            return "\n".join(lines)
        if args[0] == "iptables":
            tables = self.tables[args[4]]
            assert args[5] == "-C"
            if list(args[7:]) not in tables.get(args[6], []):
                raise ValueError("No matching rule")
            return ""
        assert args[0] == "iptables-restore" and args[-1] == "--noflush"
        table_name, *statements, commit = data.splitlines()
        assert commit == "COMMIT"
        if self.fail_table == table_name:
            raise ValueError("apply failed")
        chains = self.tables[table_name[1:]]
        for statement in statements:
            command, chain, *rule = shlex.split(statement)
            if command == "-N":
                assert chain not in chains
                chains[chain] = []
            elif command == "-F":
                chains[chain].clear()
            elif command == "-X":
                del chains[chain]
            elif command == "-A":
                chains[chain].append(rule)
            elif command == "-D":
                chains[chain].remove(rule)
            else:
                pytest.fail(statement)
        self.commits += 1
        return ""


@pytest.fixture
def manifest(panel):
    client, _ = panel
    vpn = capture_vpn_config()
    value = forwarding.ForwardingConfig.model_validate(config(client["id"], 9000, 9103))
    return forwarding._manifest(value, vpn)[0]


def test_remote_idempotence_cleanup_conflict_and_rollback(manifest, monkeypatch):
    firewall = Firewall()
    monkeypatch.setattr(remote, "run", firewall)
    manual = ["-p", "udp", "--dport", "9100:9110", "-j", "DNAT", "--to-destination", "10.0.0.2"]
    firewall.tables["nat"]["PREROUTING"].append(manual)
    with pytest.raises(ValueError, match="DNAT/REDIRECT"):
        remote.reconcile(manifest, "enp1s0", "vpn0")
    assert firewall.commits == 0
    firewall.tables["nat"]["PREROUTING"].clear()
    firewall.tables["nat"]["OUTPUT"].append(manual)
    firewall.fail_table = "*nat"
    with pytest.raises(ValueError, match="apply failed"):
        remote.reconcile(manifest, "enp1s0", "vpn0")
    assert firewall.tables["filter"] == {"FORWARD": [["-p", "icmp", "-j", "DROP"]]}
    firewall.fail_table = None
    remote.reconcile(manifest, "enp1s0", "vpn0")
    after_first = firewall.commits
    remote.reconcile(manifest, "enp1s0", "vpn0")
    assert firewall.commits == after_first
    assert len(firewall.tables["nat"]["AUTOVPN-PF-IN"]) == 2
    assert len(firewall.tables["filter"]["AUTOVPN-PF-FWD"]) == 4
    remote.reconcile({**manifest, "rules": []})
    assert firewall.tables["nat"] == {
        "PREROUTING": [], "OUTPUT": [manual], "POSTROUTING": [["-j", "MASQUERADE"]],
    }


def test_remote_checks_peer_and_restores_persisted_rules(manifest, monkeypatch, tmp_path):
    firewall = Firewall()
    commands = []
    verified_peers = {}
    for rule in manifest["rules"]:
        verified_peers[rule["peer"]] = rule["client_ip"]
    monkeypatch.setattr(remote, "CONFIG", tmp_path / "port-forwarding.json")
    monkeypatch.setattr(remote, "HELPER", tmp_path / "helper.py")
    monkeypatch.setattr(remote, "UNIT", tmp_path / remote.SERVICE)
    monkeypatch.setattr(remote, "LOCK", str(tmp_path / "lock"))
    monkeypatch.setattr(remote, "interfaces", lambda _: ("enp1s0", "vpn0"))

    def fake_run(*args, data=None):
        if args[0] == "awg":
            return "\n".join(f"{peer}\t{address}/32" for peer, address in verified_peers.items())
        if args[0] == "ip":
            return '[{"dev":"vpn0"}]'
        if args[0] == "ss":
            return ""
        if args[0] == "systemctl":
            commands.append(args)
            return ""
        return firewall(*args, data=data)

    monkeypatch.setattr(remote, "run", fake_run)
    remote.apply(manifest, install=True)
    unit = remote.UNIT.read_text()
    assert "PartOf=awg-quick@vpn0.service" in unit
    assert "WantedBy=multi-user.target awg-quick@vpn0.service" in unit
    assert ("systemctl", "reenable", remote.SERVICE) in commands

    for action in ("stop", "restore"):
        monkeypatch.setattr(remote.sys, "argv", ["helper", action])
        remote.main()
        assert ("AUTOVPN-PF-IN" in firewall.tables["nat"]) == (action == "restore")

    verified_peers.clear()
    with pytest.raises(ValueError, match="не закреплён за нужным peer"):
        remote.apply(manifest)
