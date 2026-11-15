from app.db import public_vpn_version
import yaml
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from app.main import app
from app.db import init_db, create_client, set_setting, set_client_enabled
from app.vpn_config import capture_vpn_config
from app.clash import config_filename
from app.hysteria_auth import hysteria_auth


def test_clash_profile_provider_and_versioned_downloads():
    init_db()
    set_setting('current_ip','203.0.113.10')
    for protocol in ('vless','hysteria','amnezia'):
        set_setting(f'config.{protocol}_enabled','1')
    user=create_client('Артём Mobile / "test"')
    config=capture_vpn_config()
    with TestClient(app,base_url='https://panel.example') as http:
        response=http.get(f"/clash/{user['token']}")
        assert response.status_code==200
        assert response.headers['cache-control']=='private, no-store'
        assert config_filename('clash',user,public_vpn_version(),'yaml') in response.headers['content-disposition']
        profile=yaml.safe_load(response.text)
        assert profile['mixed-port']==7897 and profile['rules']==['MATCH,VPN']
        provider_name=profile['proxy-groups'][0]['use'][0]
        provider=profile['proxy-providers'][provider_name]
        assert provider['interval']==3600
        assert provider['url']==f"https://panel.example/clash-provider/{user['token']}"
        nodes=yaml.safe_load(http.get(urlsplit(provider['url']).path).text)['proxies']
        assert [p['type'] for p in nodes]==['vless','hysteria2']
        assert nodes[0]['uuid']==user['vless_uuid']
        assert nodes[0]['reality-opts']['public-key']==config.vless.public_key
        assert nodes[1]['password']==hysteria_auth(user,config)
        assert all(p['server']==config.current_ip for p in nodes)
        assert config_filename('amnez',user,public_vpn_version(),'conf') in http.get(f"/amnezia/{user['token']}").headers['content-disposition']
        set_setting('config.vless_enabled','0')
        assert [p['type'] for p in yaml.safe_load(http.get(urlsplit(provider['url']).path).text)['proxies']]==['hysteria2']
        set_setting('config.hysteria_enabled','0')
        assert http.get(f"/clash/{user['token']}").status_code==404
        set_client_enabled(user['id'],False)
        assert http.get(urlsplit(provider['url']).path).status_code==404
        assert http.get('/clash/invalid').status_code==404


def test_filenames_cannot_escape_download_directory_or_headers():
    name=config_filename('clash',{'name':'../ Артём\r\nInjected: yes','id':3},89,'yaml')
    assert name=='clash-arteminjectedyes-89.yaml'
    assert config_filename('amnez',{'name':'  ','id':3},90,'conf')=='amnez-client3-90.conf'
