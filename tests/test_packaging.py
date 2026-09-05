from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest


PACKAGING_PYTHON = Path(os.environ.get("AUTOVPN_PACKAGING_PYTHON", sys.executable))


def _normalized_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def test_runtime_constraints_cover_direct_dependencies_and_installers_use_them() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    direct = {_normalized_name(item) for item in project["dependencies"]}
    lines = [
        line.strip()
        for line in Path("constraints-runtime.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    constrained = {_normalized_name(line) for line in lines}
    expected_runtime = {
        "annotated-doc",
        "annotated-types",
        "anyio",
        "bcrypt",
        "certifi",
        "cffi",
        "click",
        "cryptography",
        "fastapi",
        "h11",
        "httpcore",
        "httptools",
        "httpx",
        "idna",
        "invoke",
        "jinja2",
        "markupsafe",
        "paramiko",
        "pillow",
        "pycparser",
        "pydantic",
        "pydantic-core",
        "pynacl",
        "python-dotenv",
        "python-multipart",
        "pyyaml",
        "qrcode",
        "starlette",
        "typing-inspection",
        "typing-extensions",
        "uvicorn",
        "uvloop",
        "watchfiles",
        "websockets",
    }

    assert direct <= constrained
    assert constrained == expected_runtime
    assert all("==" in line.partition(";")[0] for line in lines)
    assert {
        "pytest",
        "pluggy",
        "iniconfig",
        "pygments",
        "setuptools",
        "wheel",
    }.isdisjoint(constrained)
    assert tomllib.loads(Path("pyproject.toml").read_text())["build-system"][
        "requires"
    ] == ["setuptools==84.0.0", "wheel==0.48.0"]
    for installer in ("install.sh", "install-local.sh"):
        script = Path(installer).read_text()
        assert '-c "$APP_DIR/constraints-runtime.txt" -e "$APP_DIR"' in script


def test_wheel_contains_templates_json_and_nested_static_asset(tmp_path: Path) -> None:
    backend = subprocess.run(
        [
            str(PACKAGING_PYTHON),
            "-c",
            "import setuptools, wheel; "
            "assert setuptools.__version__ == '84.0.0'; "
            "assert wheel.__version__ == '0.48.0'",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if backend.returncode != 0:
        pytest.skip(
            "wheel gate requires setuptools 84.0.0 and wheel 0.48.0; "
            "set AUTOVPN_PACKAGING_PYTHON to the prepared Python 3.12 runtime"
        )
    source = tmp_path / "source"
    wheels = tmp_path / "wheels"
    shutil.copytree(
        Path.cwd(),
        source,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", "__pycache__", "*.egg-info", "data"
        ),
    )
    subprocess.run(
        [
            str(PACKAGING_PYTHON),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheels),
            ".",
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
    )
    wheel = next(wheels.glob("autovpn-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "app/templates/client_page.html" in names
        assert "app/static/downloads/amnezia-ru-sites.json" in names
        assert "app/static/guides/amnezia/install-ready.jpg" in names
        assert "app/router_api.py" in names
        record = archive.read("autovpn-0.1.0.dist-info/RECORD")
        assert b"app/static/guides/amnezia/install-ready.jpg" in record

    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(unpacked)
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from fastapi.testclient import TestClient; "
            "import app as package; from app.main import app as application; "
            "assert package.__file__.startswith(os.environ['PYTHONPATH']); "
            "client=TestClient(application); "
            "assert client.get('/healthz').content == b'{\"status\":\"ok\"}'; "
            "assert client.get('/static/downloads/amnezia-ru-sites.json').status_code == 200",
        ],
        cwd=outside_cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PYTHONPATH": str(unpacked),
            "DATABASE_PATH": str(tmp_path / "wheel-smoke.sqlite3"),
        },
    )
    assert smoke.returncode == 0
