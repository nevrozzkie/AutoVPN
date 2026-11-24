"""Standalone IPv4 forwarding reconciler installed on the managed VPN box."""

import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

MARK = "autovpn-port-forwarding"
TABLES = (
    ("filter", "FORWARD", "AUTOVPN-PF-FWD"),
    ("nat", "PREROUTING", "AUTOVPN-PF-IN"),
)
DIRECTORY = Path("/etc/autovpn")
CONFIG = DIRECTORY / "port-forwarding.json"
HELPER = Path("/usr/local/libexec/autovpn-port-forwarding.py")
SERVICE = "autovpn-port-forwarding.service"
UNIT = Path("/etc/systemd/system") / SERVICE
LOCK = "/run/autovpn-port-forwarding.lock"
TAG = ["-m", "comment", "--comment", MARK]


def run(*args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError(f"{args[0]}: {result.stderr.strip() or result.stdout.strip() or result.returncode}")
    return result.stdout


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as handle:
        tmp = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def interfaces(manifest):
    public = manifest["public_ip"]
    uplinks = [
        link["ifname"] for link in json.loads(run("ip", "-j", "-4", "addr", "show", "scope", "global"))
        if any(addr.get("local") == public for addr in link.get("addr_info", []))
    ]
    if len(uplinks) != 1:
        raise ValueError(f"Публичный IPv4 {public} должен быть назначен ровно одному интерфейсу бокса.")
    tunnels = [
        interface for interface in run("awg", "show", "interfaces").split()
        if run("awg", "show", interface, "public-key").strip() == manifest["server_key"]
    ]
    if len(tunnels) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", tunnels[0]):
        raise ValueError("Не найден активный интерфейс AmneziaWG с ключом этого сервера.")
    if uplinks[0] == tunnels[0]:
        raise ValueError("Внешний и туннельный интерфейсы совпадают.")
    if Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1":
        raise ValueError("На VPN-боксе выключен IPv4 forwarding.")
    return uplinks[0], tunnels[0]


def check_peers(rules, tunnel):
    owners = {}
    for line in run("awg", "show", tunnel, "allowed-ips").splitlines():
        peer, *networks = line.replace(",", " ").split()
        for network in networks:
            owners.setdefault(network, set()).add(peer)
    checked = set()
    for rule in rules:
        address = rule["client_ip"]
        peer = rule["peer"]
        if (address, peer) in checked:
            continue
        if owners.get(f"{address}/32") != {peer}:
            raise ValueError(f"{rule['label']}: IPv4 {address} не закреплён за нужным peer в {tunnel}.")
        routes = json.loads(run("ip", "-j", "-4", "route", "get", address))
        if not routes or routes[0].get("dev") != tunnel:
            raise ValueError(f"{rule['label']}: маршрут к {address} проходит вне {tunnel}.")
        checked.add((address, peer))


def check_sockets(rules, public):
    listeners = set()
    for line in run("ss", "-H", "-lntu").splitlines():
        protocol, _, _, _, local, *_ = line.split()
        address, _, port = local.rpartition(":")
        if port.isdigit() and address in {public, "*", "0.0.0.0", "[::]", "::"}:
            listeners.add((protocol, int(port)))
    for rule in rules:
        if (rule["protocol"], rule["external"]) in listeners:
            raise ValueError(f"{rule['label']}: порт занят локальной службой VPN-бокса.")


def option(args, *names):
    for index, token in enumerate(args[:-1]):
        if token in names:
            return args[index + 1], index > 0 and args[index - 1] == "!"
    return None, False


def possible_nat_conflict(args, public, uplink, rule):
    target, _ = option(args, "-j", "--jump")
    if target not in {"DNAT", "REDIRECT", "NETMAP"}:
        return False
    protocol, negated = option(args, "-p", "--protocol")
    if protocol and protocol != "all" and (protocol == rule["protocol"]) == negated:
        return False
    ingress, negated = option(args, "-i", "--in-interface")
    if ingress:
        matches = uplink.startswith(ingress[:-1]) if ingress.endswith("+") else ingress == uplink
        if matches == negated:
            return False
    destination, negated = option(args, "-d", "--destination")
    if destination:
        try:
            if (ipaddress.IPv4Address(public) in ipaddress.IPv4Network(destination, strict=False)) == negated:
                return False
        except ValueError:
            pass
    ports, negated = option(args, "--dport", "--dports", "--destination-port", "--destination-ports")
    if ports:
        try:
            ranges = [item.split(":") for item in ports.split(",")]
            matches = any(int(parts[0] or 0) <= rule["external"] <= int(parts[-1] or 65535) for parts in ranges)
            if matches == negated:
                return False
        except ValueError:
            pass
    return True


def snapshot(table, parent, chain):
    dump = run("iptables-save", "-t", table)
    rules = [shlex.split(line)[1:] for line in dump.splitlines() if line.startswith("-A ")]
    chain_present = any(line.startswith(f":{chain} ") for line in dump.splitlines())
    hooks = []
    own_rules = []
    others = []
    for source, *args in rules:
        target, _ = option(args, "-j", "--jump", "-g", "--goto")
        comment, _ = option(args, "--comment")
        if target == chain:
            if source != parent or comment != MARK:
                raise ValueError(f"Чужое правило ссылается на цепочку {chain}.")
            hooks.append(args)
        elif source == chain:
            if comment != MARK:
                raise ValueError(f"В управляемой цепочке {chain} обнаружено чужое правило.")
            own_rules.append(args)
        else:
            others.append((source, args))
    if chain_present and not hooks:
        raise ValueError(f"Имя цепочки {chain} уже занято или отсутствует её метка AutoVPN.")
    return chain_present, hooks, own_rules, others


def replace_table(table, parent, chain, current, hooks, rules):
    exists, old_hooks, _, _ = current
    if not (exists or hooks):
        return
    statements = [["-D", parent, *args] for args in old_hooks]
    if exists:
        statements.append(["-F", chain])
    if hooks:
        if not exists:
            statements.append(["-N", chain])
        statements.extend(["-A", chain, *args] for args in rules)
        statements.extend(["-A", parent, *args] for args in hooks)
    else:
        statements.append(["-X", chain])
    # iptables-restore reads xtables quoting, not shell quoting.
    body = "\n".join(" ".join(json.dumps(arg) if any(c.isspace() for c in arg) else arg for arg in line)
                     for line in statements)
    run("iptables-restore", "-w", "10", "--noflush", data=f"*{table}\n{body}\nCOMMIT\n")


def check_table(table, parent, chain, current, hooks, rules):
    exists, live_hooks, live_rules, _ = current
    if exists != bool(hooks) or len(live_hooks) != len(hooks) or len(live_rules) != len(rules):
        raise ValueError(f"Проверка цепочки {chain} не подтвердила ожидаемый набор правил.")
    for source, entries in ((parent, hooks), (chain, rules)):
        for args in entries:
            run("iptables", "-w", "10", "-t", table, "-C", source, *args)


def prerouting_rules(rules):
    chains = {}
    for source, args in rules:
        chains.setdefault(source, []).append(args)
    pending = ["PREROUTING"]
    visited = set()
    while pending:
        chain = pending.pop()
        if chain in visited:
            continue
        visited.add(chain)
        for args in chains.get(chain, ()):
            yield args
            target, _ = option(args, "-j", "--jump", "-g", "--goto")
            if target in chains:
                pending.append(target)


def desired_rules(manifest, uplink, tunnel):
    public = manifest["public_ip"]
    forwarding = []
    nat = []
    for rule in manifest["rules"]:
        protocol = rule["protocol"]
        external = str(rule["external"])
        internal = str(rule["internal"])
        address = rule["client_ip"]
        nat.append(["-i", uplink, "-d", public, "-p", protocol, "--dport", external,
                    *TAG, "-j", "DNAT", "--to-destination", f"{address}:{internal}"])
        original = ["--ctorigdst", public, "--ctorigdstport", external, "--ctproto", protocol]
        forwarding.extend((
            ["-i", uplink, "-o", tunnel, "-d", address, "-p", protocol, "--dport", internal,
             "-m", "conntrack", "--ctstate", "DNAT", "--ctdir", "ORIGINAL", *original, *TAG, "-j", "ACCEPT"],
            ["-i", tunnel, "-o", uplink, "-s", address, "-p", protocol, "--sport", internal,
             "-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY", *original, *TAG, "-j", "ACCEPT"],
        ))
    return forwarding, nat


def reconcile(manifest, uplink="", tunnel="", *, persist=None):
    previous = [snapshot(*table) for table in TABLES]
    for args in prerouting_rules(previous[1][3]):
        for rule in manifest["rules"]:
            if possible_nat_conflict(args, manifest["public_ip"], uplink, rule):
                raise ValueError(f"{rule['label']}: пересечение с существующим правилом DNAT/REDIRECT бокса.")
    desired = desired_rules(manifest, uplink, tunnel)
    changed = []
    try:
        for table_info, current, rules in zip(TABLES, previous, desired):
            _, _, chain = table_info
            hooks = [[*TAG, "-j", chain]] if rules else []
            try:
                check_table(*table_info, current, hooks, rules)
            except ValueError:
                pass
            else:
                continue
            replace_table(*table_info, current, hooks, rules)
            changed.append((table_info, current))
            check_table(*table_info, snapshot(*table_info), hooks, rules)
        if persist is not None:
            persist()
    except Exception as error:
        for table_info, current in reversed(changed):
            try:
                replace_table(*table_info, snapshot(*table_info), current[1], current[2])
            except Exception as rollback:
                raise ValueError(f"{error} Не удалось восстановить предыдущие правила: {rollback}") from error
        raise


def install_service(tunnel):
    awg = f"awg-quick@{tunnel}.service"
    run("systemctl", "is-active", "--quiet", awg)
    text = f"""[Unit]
Description=AutoVPN IPv4 port forwarding
After=network-online.target {awg}
Wants=network-online.target
BindsTo={awg}
PartOf={awg}
ConditionPathExists={CONFIG}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 {HELPER} restore
ExecStop=/usr/bin/python3 {HELPER} stop

[Install]
WantedBy=multi-user.target {awg}
"""
    current = UNIT.read_text() if UNIT.exists() else ""
    if current and f"ExecStart=/usr/bin/python3 {HELPER} restore" not in current:
        raise ValueError(f"Имя сервиса {SERVICE} уже используется.")
    write(HELPER, Path(__file__).read_text(), 0o644)
    if current != text:
        write(UNIT, text, 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "reenable", SERVICE)
    else:
        run("systemctl", "enable", SERVICE)


def apply(manifest, install=False):
    rules = manifest["rules"]
    if not rules:
        reconcile(manifest, persist=(lambda: CONFIG.unlink(missing_ok=True)) if install else None)
        return
    uplink, tunnel = interfaces(manifest)
    check_peers(rules, tunnel)
    check_sockets(rules, manifest["public_ip"])
    if install:
        install_service(tunnel)
    reconcile(manifest, uplink, tunnel,
              persist=(lambda: write(CONFIG, json.dumps(manifest, ensure_ascii=True))) if install else None)


def main():
    import fcntl

    command = sys.argv[1]
    with open(LOCK, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "apply":
            manifest = json.loads(Path(sys.argv[2]).read_text())
            apply(manifest, install=True)
        elif command == "restore":
            apply(json.loads(CONFIG.read_text()))
        elif command == "stop":
            reconcile({"public_ip": "", "rules": []})
        else:
            raise ValueError("Неизвестное действие для сервиса проброса.")
    if command == "apply":
        # Starting the unit can take the same lock, so run it after releasing ours.
        if manifest["rules"]:
            run("systemctl", "start", SERVICE)
            run("systemctl", "is-active", "--quiet", SERVICE)
        print("AUTOVPN_PORT_FORWARDING_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(str(exc))
