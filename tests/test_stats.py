from app.stats import format_bytes, parse_amnezia_dump_output, parse_xray_stats_output


def test_parse_xray_stats_output() -> None:
    output = '''
stat: <
  name: "user>>>Alice-1>>>traffic>>>uplink"
  value: 123
>
stat: <
  name: "user>>>Alice-1>>>traffic>>>downlink"
  value: 456
>
'''

    assert parse_xray_stats_output(output) == {
        "Alice-1": {
            "uplink": 123,
            "downlink": 456,
        }
    }


def test_format_bytes() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_parse_amnezia_dump_output() -> None:
    output = """server-private-key\tserver-public-key\t51820\toff
client-public-key-1\tpsk\t198.51.100.10:45000\t10.66.66.2/32\t1710000000\t1234\t5678\toff
client-public-key-2\tpsk\t(none)\t10.66.66.3/32\t0\t0\t0\toff
"""

    assert parse_amnezia_dump_output(output) == {
        "client-public-key-1": {
            "rx": 1234,
            "tx": 5678,
            "latest_handshake": 1710000000,
        },
        "client-public-key-2": {
            "rx": 0,
            "tx": 0,
            "latest_handshake": 0,
        },
    }
