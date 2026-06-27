from __future__ import annotations

import secrets
from io import BytesIO

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.aeza import AezaClient
from app.amnezia import build_amnezia_client_config
from app.db import (
    create_client,
    create_install_operation,
    create_operation,
    delete_client,
    get_client_by_id,
    get_client_by_token,
    get_latest_install_operation,
    get_latest_operation,
    get_setting,
    has_running_install_operation,
    has_running_operation,
    init_db,
    list_clients,
    list_clients_with_stats,
    list_install_operations,
    list_operations,
    set_setting,
    set_client_enabled,
    update_client_name,
)
from app.eu_install import build_eu_install_script, describe_ssh_command, resolve_eu_host, run_eu_install
from app.ip_change import apply_manual_main_ip, run_ip_change
from app.protocol_status import refresh_protocol_statuses
from app.runtime_config import (
    admin_password,
    admin_username,
    aeza_api_base,
    aeza_ipv4_after_purchase_delay_seconds,
    aeza_ipv4_domain,
    aeza_ipv4_payment_method,
    aeza_service_id,
    aeza_token,
    setup_complete,
    eu_ssh_host,
    eu_ssh_key_path,
    eu_ssh_password,
    eu_ssh_port,
    eu_ssh_user,
)
from app.stats import format_bytes, refresh_client_stats
from app.subscriptions import build_subscription

app = FastAPI(title="AutoVPN")
templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()
setup_security = HTTPBasic(auto_error=False)


def aeza_ip_rotation_available() -> bool:
    return bool(aeza_token() and aeza_service_id())


async def fetch_aeza_ipv4_price() -> dict[str, object] | None:
    if not aeza_ip_rotation_available():
        return None
    try:
        client = AezaClient(aeza_api_base(), aeza_token())
        return await client.get_ipv4_price(aeza_service_id())
    except Exception:
        return None


def get_aeza_client() -> AezaClient:
    return AezaClient(aeza_api_base(), aeza_token())


def get_amnezia_obfuscation() -> dict[str, int]:
    return {
        "jc": int(get_setting("amnezia.jc", "5")),
        "jmin": int(get_setting("amnezia.jmin", "40")),
        "jmax": int(get_setting("amnezia.jmax", "1000")),
        "s1": int(get_setting("amnezia.s1", "64")),
        "s2": int(get_setting("amnezia.s2", "128")),
        "h1": int(get_setting("amnezia.h1", "1")),
        "h2": int(get_setting("amnezia.h2", "2")),
        "h3": int(get_setting("amnezia.h3", "3")),
        "h4": int(get_setting("amnezia.h4", "4")),
    }


def get_amnezia_config(client: dict, current_ip: str) -> str:
    return build_amnezia_client_config(
        client,
        current_ip=current_ip,
        server_public_key=get_setting("amnezia.server_public_key"),
        obfuscation=get_amnezia_obfuscation(),
    )


def format_eur_minor_units(value: object) -> str:
    try:
        return f"€{float(value) / 100:.2f}"
    except (TypeError, ValueError):
        return "unknown"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not setup_complete():
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/setup"},
        )
    expected_password = admin_password()
    username_ok = secrets.compare_digest(credentials.username, admin_username())
    password_ok = bool(expected_password) and secrets.compare_digest(
        credentials.password,
        expected_password,
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_setup_access(
    credentials: HTTPBasicCredentials | None = Depends(setup_security),
) -> str | None:
    if not setup_complete():
        return None
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    expected_password = admin_password()
    username_ok = secrets.compare_digest(credentials.username, admin_username())
    password_ok = bool(expected_password) and secrets.compare_digest(
        credentials.password,
        expected_password,
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    if not setup_complete():
        return RedirectResponse("/setup")
    return RedirectResponse("/admin")


@app.get("/setup", response_class=HTMLResponse)
def setup_page(
    request: Request,
    _: str | None = Depends(require_setup_access),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "admin_username": admin_username(),
            "current_ip": get_setting("current_ip"),
            "eu_ssh_host": eu_ssh_host(),
            "eu_ssh_user": eu_ssh_user(),
            "eu_ssh_port": eu_ssh_port(),
            "eu_ssh_key_path": eu_ssh_key_path(),
            "eu_ssh_password_configured": bool(eu_ssh_password()),
            "aeza_token_configured": bool(aeza_token()),
            "aeza_service_id": aeza_service_id(),
            "aeza_ipv4_domain": aeza_ipv4_domain(),
            "setup_complete": setup_complete(),
        },
    )


@app.post("/setup")
def setup_submit(
    _: str | None = Depends(require_setup_access),
    admin_username_value: str = Form("admin"),
    admin_password_value: str = Form(""),
    admin_password_confirm_value: str = Form(""),
    current_ip: str = Form(""),
    eu_ssh_host_value: str = Form(""),
    eu_ssh_user_value: str = Form("root"),
    eu_ssh_port_value: int = Form(22),
    eu_ssh_password_value: str = Form(""),
    eu_ssh_key_path_value: str = Form(""),
    aeza_token_value: str = Form(""),
    aeza_service_id_value: str = Form(""),
    aeza_ipv4_domain_value: str = Form(""),
) -> RedirectResponse:
    password = admin_password_value.strip()
    password_confirm = admin_password_confirm_value.strip()
    if password != password_confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin passwords do not match")
    if not setup_complete() and not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin password is required")

    set_setting("config.admin_username", admin_username_value.strip() or "admin")
    if password:
        set_setting("config.admin_password", password)
    if current_ip.strip():
        set_setting("current_ip", current_ip.strip())

    set_setting("config.eu_ssh_host", eu_ssh_host_value.strip() or current_ip.strip())
    set_setting("config.eu_ssh_user", eu_ssh_user_value.strip() or "root")
    set_setting("config.eu_ssh_port", str(eu_ssh_port_value or 22))
    if eu_ssh_password_value:
        set_setting("config.eu_ssh_password", eu_ssh_password_value)
        set_setting("config.eu_ssh_key_path", "")
    else:
        if not eu_ssh_password() or eu_ssh_key_path_value.strip():
            set_setting("config.eu_ssh_password", "")
        set_setting("config.eu_ssh_key_path", eu_ssh_key_path_value.strip())

    set_setting("config.aeza_api_base", "https://my.aeza.net")
    if aeza_token_value.strip():
        set_setting("config.aeza_token", aeza_token_value.strip())
    set_setting("config.aeza_service_id", aeza_service_id_value.strip())
    set_setting("config.aeza_ipv4_payment_method", "balance")
    set_setting("config.aeza_ipv4_domain", aeza_ipv4_domain_value.strip())
    set_setting("config.aeza_ipv4_after_purchase_delay_seconds", "300")
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    current_ip = get_setting("current_ip")
    protocols = await refresh_protocol_statuses(current_ip)
    clients = list_clients_with_stats()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_ip": current_ip,
            "protocols": protocols,
            "clients": clients,
            "base_url": str(request.base_url).rstrip("/"),
            "latest_operation": get_latest_operation(),
            "latest_install_operation": get_latest_install_operation(),
            "operation_running": has_running_operation(),
            "install_running": has_running_install_operation(),
            "aeza_ip_rotation_available": aeza_ip_rotation_available(),
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "format_eur_minor_units": format_eur_minor_units,
            "format_bytes": format_bytes,
            "stats_last_refresh_at": get_setting("stats.last_refresh_at"),
            "stats_last_error": get_setting("stats.last_error"),
        },
    )


@app.get("/admin/ip/confirm", response_class=HTMLResponse)
async def confirm_ip_refresh(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    price = await fetch_aeza_ipv4_price()
    return templates.TemplateResponse(
        request,
        "ip_confirm.html",
        {
            "current_ip": get_setting("current_ip"),
            "operation_running": has_running_operation(),
            "aeza_ipv4_price": price,
            "after_purchase_delay_seconds": aeza_ipv4_after_purchase_delay_seconds(),
            "format_eur_minor_units": format_eur_minor_units,
        },
    )


@app.post("/admin/ip/refresh")
def refresh_ip(background_tasks: BackgroundTasks, _: str = Depends(require_admin)) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    if has_running_operation():
        return RedirectResponse("/admin/ip/confirm?already_running=1", status_code=status.HTTP_303_SEE_OTHER)
    operation_id = create_operation()
    background_tasks.add_task(_run_operation_background, operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/ip", response_class=HTMLResponse)
async def admin_ip_manager(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    error = ""
    ipv4_list: list[dict] = []
    try:
        ipv4_list = await get_aeza_client().get_ipv4_list(aeza_service_id())
    except Exception as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "ip_manager.html",
        {
            "current_ip": get_setting("current_ip"),
            "ipv4_list": ipv4_list,
            "error": error,
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "format_eur_minor_units": format_eur_minor_units,
        },
    )


@app.get("/admin/ip/buy/confirm", response_class=HTMLResponse)
async def admin_ip_buy_confirm(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "ip_buy_confirm.html",
        {
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "format_eur_minor_units": format_eur_minor_units,
        },
    )


@app.post("/admin/ip/buy")
async def admin_ip_buy(_: str = Depends(require_admin)) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    await get_aeza_client().add_ipv4(
        aeza_service_id(),
        payment_method=aeza_ipv4_payment_method(),
        domain=aeza_ipv4_domain(),
    )
    return RedirectResponse("/admin/ip", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/ip/{ipv4_id}/make-main")
async def admin_ip_make_main(
    ipv4_id: str,
    ip: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    await get_aeza_client().make_main_ipv4(aeza_service_id(), ipv4_id)
    await apply_manual_main_ip(ip)
    return RedirectResponse("/admin/ip", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/ip/{ipv4_id}/delete")
async def admin_ip_delete(
    ipv4_id: str,
    ip: str = Form(...),
    is_main: str = Form("0"),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    if is_main == "1" or ip == get_setting("current_ip"):
        return RedirectResponse("/admin/ip?cannot_delete_main=1", status_code=status.HTTP_303_SEE_OTHER)
    await get_aeza_client().delete_ipv4(aeza_service_id(), ipv4_id)
    return RedirectResponse("/admin/ip", status_code=status.HTTP_303_SEE_OTHER)


async def _run_operation_background(operation_id: int) -> None:
    try:
        await run_ip_change(operation_id)
    except Exception:
        # The operation runner has already persisted the failure details.
        return


@app.get("/admin/clients", response_class=HTMLResponse)
def admin_clients(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "clients": list_clients(),
            "base_url": str(request.base_url).rstrip("/"),
        },
    )


@app.post("/admin/clients")
def admin_create_client(
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    create_client(name.strip())
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/rename")
def admin_rename_client(
    client_id: int,
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    update_client_name(client_id, name.strip())
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/enable")
def admin_enable_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    set_client_enabled(client_id, True)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/disable")
def admin_disable_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    set_client_enabled(client_id, False)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/delete")
def admin_delete_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    delete_client(client_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/operations", response_class=HTMLResponse)
def admin_operations(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "operations": list_operations(),
            "install_operations": list_install_operations(),
        },
    )


@app.get("/admin/install", response_class=HTMLResponse)
def admin_install(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    host = resolve_eu_host()
    ssh_command = ""
    if host:
        ssh_command = describe_ssh_command(host)
    return templates.TemplateResponse(
        request,
        "install.html",
        {
            "target_host": host,
            "ssh_command": ssh_command,
            "enabled_clients": [client for client in list_clients() if client["enabled"]],
            "latest_install_operation": get_latest_install_operation(),
            "install_running": has_running_install_operation(),
        },
    )


@app.get("/admin/install/script", response_class=PlainTextResponse)
def admin_install_script(_: str = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(build_eu_install_script())


@app.post("/admin/install/run")
def admin_run_install(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if has_running_install_operation():
        return RedirectResponse("/admin?install_already_running=1", status_code=status.HTTP_303_SEE_OTHER)
    host = resolve_eu_host()
    operation_id = create_install_operation(host)
    background_tasks.add_task(_run_install_background, operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _run_install_background(operation_id: int) -> None:
    try:
        await run_eu_install(operation_id)
    except Exception:
        # The install runner has already persisted the failure details.
        return


@app.post("/admin/stats/refresh")
def admin_refresh_stats(background_tasks: BackgroundTasks, _: str = Depends(require_admin)) -> RedirectResponse:
    background_tasks.add_task(_refresh_stats_background)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _refresh_stats_background() -> None:
    try:
        await refresh_client_stats()
    except Exception as exc:
        set_setting("stats.last_error", str(exc)[-4000:])


@app.get("/sub/{token}", response_class=PlainTextResponse)
def subscription(token: str) -> PlainTextResponse:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if not current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(build_subscription(client, current_ip))


@app.get("/client/{token}", response_class=HTMLResponse)
async def client_page(request: Request, token: str) -> HTMLResponse:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if not current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    protocols = await refresh_protocol_statuses(current_ip)
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "client_page.html",
        {
            "client": client,
            "current_ip": current_ip,
            "protocols": protocols,
            "base_url": base_url,
            "subscription_url": f"{base_url}/sub/{client['token']}",
            "amnezia_url": f"{base_url}/amnezia/{client['token']}",
            "amnezia_qr_url": f"{base_url}/client/{client['token']}/amnezia.qr",
        },
    )


@app.get("/client/{token}/subscription.qr")
def client_subscription_qr(request: Request, token: str) -> Response:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    import qrcode

    subscription_url = f"{str(request.base_url).rstrip('/')}/sub/{client['token']}"
    image = qrcode.make(subscription_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/client/{token}/amnezia.qr")
def client_amnezia_qr(token: str) -> Response:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if not current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    import qrcode

    image = qrcode.make(get_amnezia_config(client, current_ip))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/ip/{token}", response_class=PlainTextResponse)
def current_eu_ip(token: str) -> PlainTextResponse:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if not current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(f"{current_ip}\n")


@app.get("/amnezia/{token}", response_class=PlainTextResponse)
def amnezia_config(token: str) -> PlainTextResponse:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if not current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(
        get_amnezia_config(client, current_ip),
        headers={
            "Content-Disposition": f"attachment; filename=amnezia-{client['id']}.conf"
        },
    )
