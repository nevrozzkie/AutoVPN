"""Mihomo profiles and safe, revisioned download names."""
from __future__ import annotations

import re
import unicodedata
import yaml

from app.hysteria_auth import hysteria_auth


def config_filename(kind, client, revision, extension):
    letters = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                       ['a','b','v','g','d','e','e','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','h','ts','ch','sh','sch','','y','','e','yu','ya']))
    name = ''.join(letters.get(c, c) for c in client['name'].lower())
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    name = re.sub(r'[^a-z0-9_-]', '', name)[:48].strip('-_') or f"client{client['id']}"
    return f'{kind}-{name}-{revision}.{extension}'


def clash_provider(config, client):
    proxies = []
    if config.vless.protocol.enabled:
        proxies.append({
            'name': 'VLESS', 'type': 'vless', 'server': config.current_ip,
            'port': config.vless.protocol.port, 'uuid': client['vless_uuid'],
            'network': 'tcp', 'udp': True, 'tls': True, 'flow': 'xtls-rprx-vision',
            'servername': config.vless.server_name, 'client-fingerprint': 'iOS' if config.vless.fingerprint == 'ios' else config.vless.fingerprint,
            'reality-opts': {'public-key': config.vless.public_key, 'short-id': config.vless.short_id},
        })
    if config.hysteria.protocol.enabled:
        proxy = {'name': 'Hysteria', 'type': 'hysteria2', 'server': config.current_ip,
                 'port': config.hysteria.protocol.port, 'password': hysteria_auth(client, config),
                 'sni': config.vless.server_name, 'skip-cert-verify': True}
        if config.hysteria.obfs_password:
            proxy.update({'obfs': 'salamander', 'obfs-password': config.hysteria.obfs_password})
        proxies.append(proxy)
    return {'proxies': proxies}


def clash_profile(config, client, base_url):
    name = config_filename('clash', client, config.revision, 'yaml')[:-5]
    return {'mode': 'rule', 'mixed-port': 7897,
            'proxy-providers': {name: {'type': 'http', 'url': f"{base_url}/clash-provider/{client['token']}",
                                     'path': f'./providers/{name}.yaml', 'interval': 3600, 'proxy': 'DIRECT'}},
            'proxy-groups': [{'name': 'VPN', 'type': 'select', 'use': [name]}],
            'rules': ['MATCH,VPN']}


def dump_yaml(value):
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
