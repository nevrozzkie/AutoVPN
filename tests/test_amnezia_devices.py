import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, amnezia_devices as devices
from app.amnezia import render_amnezia_server_config
from app.deep_protocol_checks import build_deep_check_script
from app.db import init_db,create_client,set_setting,get_db,get_vpn_state,set_client_enabled
from app.vpn_config import capture_vpn_config,canonical_vpn_config_json,captured_vpn_config_from_json
from app.vpn_state import prepare_install_operation,complete_install_operation
from app.operation_coordinator import recover_incomplete_vps_operations


@pytest.fixture
def device_user(monkeypatch):
    init_db()
    set_setting('current_ip','203.0.113.10')
    set_setting('config.amnezia_enabled','1')
    user=create_client('Artyom')
    other=create_client('Other')
    op=prepare_install_operation('203.0.113.10')
    complete_install_operation(op.operation_id,'Initial install')
    calls=[]
    async def remote(host,command,stdin_data='',timeout=None):
        calls.append(stdin_data)
        assert 'systemctl' not in stdin_data
        assert 'awg set awg0 peer' in stdin_data
        return 0,'ok'
    monkeypatch.setattr(devices,'run_remote_command',remote)
    return user,other,calls


def test_add_export_revoke_preserves_first_identity_and_other_protocols(device_user):
    user,other,calls=device_user
    original=capture_vpn_config()
    with TestClient(main.app,base_url='http://panel.local') as http:
        legacy=http.get(f"/amnezia/{user['token']}").text
        start=http.post(f"/client/{user['token']}/amnezia-devices",data={'name':'desktop'},follow_redirects=False)
        assert start.status_code==303
        device_id=int(start.headers['location'].split('device=')[1].split('#')[0])
        assert len(calls)==1
        first=http.get(f"/amnezia/{user['token']}")
        second=http.get(f"/amnezia/{user['token']}?device={device_id}")
        assert first.text==legacy and second.status_code==200
        assert user['amnezia_private_key'] not in second.text
        assert other['amnezia_private_key'] not in second.text
        assert http.get(f"/amnezia/{other['token']}?device={device_id}").status_code==404
        assert http.get(f"/amnezia-key/{user['token']}?device={device_id}").text.startswith('vpn://')
        assert http.get(f"/client/{user['token']}/amnezia.qr?device={device_id}").status_code==200
        config=capture_vpn_config()
        assert config.clients==original.clients
        assert config.vless==original.vless and config.hysteria==original.hysteria
        assert len(config.amnezia_devices)==1
        assert captured_vpn_config_from_json(canonical_vpn_config_json(config))==config
        assert config.amnezia_devices[0].public_key in render_amnezia_server_config(config)
        assert config.amnezia_devices[0].public_key in build_deep_check_script(config.enabled_clients[0].as_dict(), config.current_ip, config=config)
        assert get_vpn_state()['desired_revision']==get_vpn_state()['applied_revision']
        # Repeated creation selects the existing identity, without another SSH command.
        assert http.post(f"/client/{user['token']}/amnezia-devices",data={'name':'desktop'},follow_redirects=False).status_code==303
        assert len(calls)==1
        assert http.post(f"/client/{other['token']}/amnezia-devices/{device_id}/delete").status_code==409
        assert http.post(f"/client/{user['token']}/amnezia-devices/{device_id}/delete",headers={'Origin':'https://evil.example'}).status_code==403
        deleted=http.post(f"/client/{user['token']}/amnezia-devices/{device_id}/delete",follow_redirects=False)
        assert deleted.status_code==303
        assert http.get(deleted.headers['location']).status_code==200
        assert len(calls)==2 and ' remove' in calls[-1]
        assert http.get(f"/amnezia/{user['token']}?device={device_id}").status_code==404
        assert http.get(f"/amnezia/{user['token']}").text==legacy
        assert capture_vpn_config().amnezia_devices==()


def test_unconfirmed_and_failed_identity_never_exports_legacy_key(device_user,monkeypatch):
    user,_,calls=device_user
    device_id,operation=devices.prepare_device(user['id'],name='laptop')
    http=TestClient(main.app)
    path=f"/amnezia/{user['token']}?device={device_id}"
    assert http.get(path).status_code==409
    async def failure(*args,**kwargs):raise TimeoutError('lost acknowledgement')
    monkeypatch.setattr(devices,'run_remote_command',failure)
    asyncio.run(devices.run_device_sync(operation))
    assert http.get(path).status_code==409
    with pytest.raises(ValueError):devices.prepare_device(user['id'],name='tablet')
    before=len(calls)
    asyncio.run(devices.run_device_sync(operation))
    assert len(calls)==before
    # A later explicit full synchronization can recover the desired peer.
    install=prepare_install_operation('203.0.113.10')
    complete_install_operation(install.operation_id,'Admin recovery')
    assert http.get(path).status_code==200


def test_restart_preserves_pending_peer_and_prevents_auto_resend(device_user):
    user,_,calls=device_user
    device_id,operation=devices.prepare_device(user['id'],name='tablet')
    with get_db() as db:recover_incomplete_vps_operations(db,'Restart')
    asyncio.run(devices.run_device_sync(operation))
    assert calls==[]
    assert not next(d for d in devices.device_list(user['id']) if d['id']==device_id)['ready']


def test_device_inputs_disabled_owner_and_ip_address_reservations(device_user):
    user,other,_=device_user
    with pytest.raises(ValueError):devices.prepare_device(user['id'],name='mobile')
    with pytest.raises(ValueError):devices.prepare_device(user['id'],name='\n')
    first,operation=devices.prepare_device(user['id'],name='desktop')
    asyncio.run(devices.run_device_sync(operation))
    second,operation=devices.prepare_device(other['id'],name='desktop')
    asyncio.run(devices.run_device_sync(operation))
    config=capture_vpn_config()
    addresses=[c.amnezia_ipv4 for c in config.clients]+[d.ipv4 for d in config.amnezia_devices]
    assert len(set(addresses))==len(addresses)
    assert first!=second
    extra = create_client('New client after device allocation')
    assert extra['amnezia_ipv4'] not in addresses
    set_client_enabled(user['id'],False)
    assert all(d.client_id!=user['id'] for d in capture_vpn_config().amnezia_devices)
    with pytest.raises(ValueError):devices.prepare_device(user['id'],name='another')


def test_peer_script_changes_only_selected_peer_and_persists_without_restart(tmp_path):
    config_dir = tmp_path / 'amnezia'
    config_dir.mkdir()
    live = config_dir / 'awg0.conf'
    original = '[Interface]\nPrivateKey = unchanged\nAddress = 10.66.66.1/24\nJc = 7\n\n[Peer]\nPublicKey = first-peer\nAllowedIPs = 10.66.66.2/32\n'
    live.write_text(original)
    state = tmp_path / 'peers.json'
    state.write_text(json.dumps({'first-peer': '10.66.66.2/32'}))
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    # Execute the generated Bash/Python, replacing only privileged paths and tools.
    (binaries / 'awg').write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['PEER_STATE']);d=json.loads(p.read_text());a=sys.argv[1:]
if a==['show','awg0','public-key']: print(os.environ.get('SERVER_KEY','server-key'))
elif a==['show','awg0','peers']: print('\\n'.join(d))
elif a[:3]==['set','awg0','peer']:
 if a[4:] == ['remove']: d.pop(a[3],None)
 else:
  assert a[4]=='preshared-key' and Path(a[5]).read_text()=='secret-psk'
  assert a[6]=='allowed-ips'; d[a[3]]=a[7]
 p.write_text(json.dumps(d))
else: raise SystemExit(1)
''')
    (binaries / 'awg-quick').write_text('#!/bin/sh\n[ "$1" = strip ] && test -s "$2"\n')
    (binaries / 'flock').write_text('#!/bin/sh\nexit 0\n')
    for binary in binaries.iterdir(): binary.chmod(0o700)
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'], PEER_STATE=str(state))
    device = {'public_key': 'second-peer', 'preshared_key': 'secret-psk', 'ipv4': '10.66.66.3'}

    def run(delete=False, **extra_env):
        script = devices.peer_script(device, delete, 'server-key')
        script = script.replace('/etc/amnezia/amneziawg', str(config_dir)).replace('/run/autovpn-device.lock', str(tmp_path / 'lock'))
        assert subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True).returncode == 0
        return subprocess.run(['bash', '-s'], input=script, text=True, capture_output=True, env=env | extra_env)

    assert run(SERVER_KEY='another-server').returncode != 0
    assert live.read_text() == original
    assert run().returncode == 0
    assert json.loads(state.read_text()) == {'first-peer': '10.66.66.2/32', 'second-peer': '10.66.66.3/32'}
    assert live.read_text().startswith(original)
    assert live.stat().st_mode & 0o777 == 0o600
    # Retrying a known idempotent script does not duplicate persisted peers.
    assert run().returncode == 0
    assert live.read_text().count('PublicKey = second-peer') == 1
    assert run(delete=True).returncode == 0
    assert json.loads(state.read_text()) == {'first-peer': '10.66.66.2/32'}
    assert live.read_text() == original
