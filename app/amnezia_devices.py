"""Additional Amnezia peers, preserving all legacy client identities."""
from __future__ import annotations

import json
import shlex
import unicodedata
from dataclasses import replace

from app.db import get_db, now_iso, _mark_vpn_config_updated, _allocate_amnezia_address
from app.amnezia import generate_private_key, generate_public_key, generate_preshared_key
from app.vpn_config import capture_vpn_config, captured_vpn_config_from_json
from app.vpn_state import (_snapshot_payload, _ensure_snapshot, load_install_snapshot,
                           complete_install_operation, mark_install_operation_ambiguous)
from app.operation_coordinator import acquire_vps_lease
from app.eu_install import run_remote_command, resolve_eu_host


def applied_config(db):
    row = db.execute('SELECT s.payload_json FROM vpn_state v JOIN vpn_snapshots s ON s.revision=v.applied_revision WHERE v.singleton=1').fetchone()
    return captured_vpn_config_from_json(row['payload_json']) if row else None


def device_list(client_id):
    with get_db() as db:
        applied = applied_config(db)
        known = {d.id: d.public_key for d in applied.amnezia_devices} if applied else {}
        rows = db.execute('SELECT d.*,o.status AS operation_status FROM amnezia_devices d LEFT JOIN vpn_install_operations o ON o.id=d.operation_id WHERE d.client_id=? ORDER BY d.id', (client_id,)).fetchall()
        result = [{'id': 'mobile', 'name': 'mobile', 'ready': True, 'legacy': True, 'deleted': False}]
        for row in rows:
            if row['deleted_at'] and row['id'] not in known:
                continue
            result.append({'id': row['id'], 'name': row['name'], 'legacy': False,
                           'ready': not row['deleted_at'] and known.get(row['id']) == row['public_key'],
                           'deleted': bool(row['deleted_at']),
                           'failed': row['operation_status'] in {'FAILED','AMBIGUOUS','TIMED_OUT'}})
        if not any(row['name'].casefold() == 'desktop' for row in rows):
            result.insert(1, {'id': 'desktop', 'name': 'desktop', 'placeholder': True, 'ready': False})
        return result


def config_for_device(config, client, device_id):
    if device_id is None:
        return config, client
    with get_db() as db:
        row = db.execute('SELECT id FROM amnezia_devices WHERE id=? AND client_id=? AND deleted_at IS NULL', (device_id, client.id)).fetchone()
        if not row:
            raise LookupError('Устройство не найдено')
        applied = applied_config(db)
    device = next((d for d in applied.amnezia_devices if d.id == device_id and d.client_id == client.id), None) if applied else None
    if not device or applied.current_ip != config.current_ip:
        raise ValueError('Конфигурация устройства ещё не применена')
    return applied, replace(client, name=f'{client.name} {device.name}', amnezia_private_key=device.private_key,
                            amnezia_public_key=device.public_key, amnezia_preshared_key=device.preshared_key,
                            amnezia_ipv4=device.ipv4)


def prepare_device(client_id, *, name=None, delete_id=None):
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 40 or any(unicodedata.category(c).startswith('C') for c in name):
            raise ValueError('Название должно содержать от 1 до 40 символов')
        if name.casefold() == 'mobile':
            raise ValueError('Название mobile закреплено за первым конфигом')
    host = resolve_eu_host()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute('SELECT enabled FROM clients WHERE id=? AND deleted_at IS NULL', (client_id,)).fetchone()
        if not owner or not owner['enabled']:
            raise ValueError('Клиент отключён')
        if name is not None:
            existing = next((r for r in db.execute('SELECT id,name FROM amnezia_devices WHERE client_id=? AND deleted_at IS NULL', (client_id,)) if r['name'].casefold()==name.casefold()), None)
            if existing:
                return existing['id'], None
        state = db.execute('SELECT desired_revision,applied_revision FROM vpn_state WHERE singleton=1').fetchone()
        if state['applied_revision'] != state['desired_revision']:
            raise ValueError('Настройки VPN ожидают применения. Обратитесь к администратору')
        config = capture_vpn_config(db)
        if not host or not config.current_ip or not config.amnezia.protocol.enabled:
            raise ValueError('AmneziaVPN сейчас недоступна')
        applied = applied_config(db)
        if not applied or applied.current_ip != config.current_ip:
            raise ValueError('Текущий сервер ещё не подтверждён администратором')
        # Reserve a standard durable install operation and shared VPS lease in this transaction.
        now = now_iso()
        op = int(db.execute("INSERT INTO vpn_install_operations(status,target_host,current_step,created_at,updated_at) VALUES ('PENDING',?,'device_peer_sync',?,?)", (host,now,now)).lastrowid)
        acquire_vps_lease(db,'INSTALL',op)
        if delete_id is None:
            if db.execute('SELECT count(*) AS n FROM amnezia_devices WHERE client_id=? AND deleted_at IS NULL',(client_id,)).fetchone()['n'] >= 16:
                raise ValueError('Достигнут лимит устройств')
            if db.execute('SELECT count(*) AS n FROM amnezia_devices WHERE client_id=?',(client_id,)).fetchone()['n'] >= 32:
                raise ValueError('Достигнут лимит выпущенных ключей. Обратитесь к администратору')
            try:
                address = _allocate_amnezia_address(db)
            except RuntimeError:
                raise ValueError('Нет свободных адресов для устройств. Обратитесь к администратору') from None
            private = generate_private_key()
            device_id = int(db.execute('INSERT INTO amnezia_devices(client_id,name,private_key,public_key,preshared_key,ipv4,created_at,operation_id) VALUES (?,?,?,?,?,?,?,?)',
                (client_id,name,private,generate_public_key(private),generate_preshared_key(),address,now,op)).lastrowid)
        else:
            device_id = delete_id
            if not db.execute('UPDATE amnezia_devices SET deleted_at=?,operation_id=? WHERE id=? AND client_id=? AND deleted_at IS NULL',(now,op,device_id,client_id)).rowcount:
                raise ValueError('Устройство не найдено или уже удаляется')
        _mark_vpn_config_updated(db)
        config,payload,digest = _snapshot_payload(db)
        _ensure_snapshot(db,config,payload,digest,now,reprepare_failed=False)
        db.execute('UPDATE vpn_install_operations SET revision=? WHERE id=?',(config.revision,op))
        db.execute('INSERT INTO settings(key,value) VALUES (?,?)',(f'install.{op}.amnezia_device',json.dumps({'device_id':device_id,'delete':delete_id is not None})))
        return device_id, op


def peer_script(device, delete, server_public_key):
    # Credentials stay in SSH stdin and root-only files, never in command arguments or logs.
    data = json.dumps({'public':device['public_key'],'psk':device['preshared_key'],'ip':device['ipv4'],'delete':delete})
    return '''set -euo pipefail
umask 077
exec 9>/run/autovpn-device.lock
flock -x 9
[ "$(awg show awg0 public-key)" = '''+shlex.quote(server_public_key)+''' ]
WORK=$(mktemp -d /etc/amnezia/amneziawg/.device-XXXXXX)
trap 'rm -rf "$WORK"' EXIT
LIVE=/etc/amnezia/amneziawg/awg0.conf
test -f "$LIVE" && test ! -L "$LIVE"
cp "$LIVE" "$WORK/original.conf"
cat >"$WORK/device.json" <<'DEVICE'
'''+data+'''
DEVICE
python3 - "$WORK" <<'PYDEVICE'
import json,re,sys
from pathlib import Path
work=Path(sys.argv[1]);d=json.loads((work/'device.json').read_text())
source=(work/'original.conf').read_text()
parts=re.split(r'(?m)(?=^\\[Peer\\]\\s*$)',source)
kept=[p for p in parts if not re.search(r'(?m)^PublicKey\\s*=\\s*'+re.escape(d['public'])+r'\\s*$',p)]
text=''.join(kept).rstrip()+'\\n'
if not d['delete']:
 text+='\\n[Peer]\\nPublicKey = '+d['public']+'\\nPresharedKey = '+d['psk']+'\\nAllowedIPs = '+d['ip']+'/32\\n'
(work/'awg0.conf').write_text(text)
(work/'psk').write_text(d['psk'])
(work/'public').write_text(d['public'])
(work/'ip').write_text(d['ip'])
PYDEVICE
awg-quick strip "$WORK/awg0.conf" >/dev/null
PUBLIC=$(cat "$WORK/public")
'''+('awg set awg0 peer "$PUBLIC" remove\n' if delete else 'awg set awg0 peer "$PUBLIC" preshared-key "$WORK/psk" allowed-ips "$(cat "$WORK/ip")/32"\n')+'''
# Keep the change across reboot; the other peers and interface remain untouched.
cp "$WORK/original.conf" "$LIVE.autovpn-device-backup"
chmod 600 "$WORK/awg0.conf" "$LIVE.autovpn-device-backup"
mv "$WORK/awg0.conf" "$LIVE"
'''+('PEERS=$(awg show awg0 peers)\n! printf \'%s\\n\' "$PEERS" | grep -Fxq "$PUBLIC"\n' if delete else 'PEERS=$(awg show awg0 peers)\nprintf \'%s\\n\' "$PEERS" | grep -Fxq "$PUBLIC"\n')


async def run_device_sync(operation_id):
    with get_db() as db:
        if not db.execute("UPDATE vpn_install_operations SET status='RUNNING' WHERE id=? AND status='PENDING'",(operation_id,)).rowcount:
            return
    try:
        with get_db() as db:
            binding=json.loads(db.execute('SELECT value FROM settings WHERE key=?',(f'install.{operation_id}.amnezia_device',)).fetchone()['value'])
            device=db.execute('SELECT * FROM amnezia_devices WHERE id=?',(binding['device_id'],)).fetchone()
        operation,config=load_install_snapshot(operation_id)
        code,_=await run_remote_command(operation['target_host'],'bash -s',stdin_data=peer_script(device,binding['delete'],config.amnezia.server_public_key),timeout=45)
        if code:
            raise RuntimeError('peer sync failed')
        complete_install_operation(operation_id,'Amnezia device peer synchronized without restarting VPN services')
    except Exception:
        # Do not retry an SSH mutation automatically after a missing acknowledgement.
        mark_install_operation_ambiguous(operation_id,'Изменение устройства не подтверждено. Нужна проверка и синхронизация администратором.')
