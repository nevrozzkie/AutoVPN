from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.aeza import AezaClient
from app.amnezia import (
    build_amnezia_client_config,
    build_amnezia_vpn_key,
    render_amnezia_client_config,
    render_amnezia_vpn_key,
)
from app.db import (
    create_client,
    create_operation,
    delete_client,
    fail_incomplete_install_operations,
    get_client_by_id,
    get_client_by_token,
    get_latest_install_operation,
    get_latest_operation,
    get_operation,
    get_setting,
    get_vpn_state,
    has_running_install_operation,
    has_running_operation,
    init_db,
    list_clients,
    list_clients_with_stats,
    list_install_operations,
    list_operations,
    reset_server_and_aeza_state,
    reconcile_ip_operation,
    set_setting,
    set_settings,
    set_client_enabled,
    update_client_name,
)
from app.eu_install import (
    build_eu_deploy_script,
    describe_ssh_command,
    forget_ssh_known_host,
    resolve_eu_host,
    run_eu_install,
)
from app.ip_change import run_ip_change
from app.operation_coordinator import OperationBusyError
from app.protocol_status import (
    get_client_protocol_statuses,
    get_protocol_statuses,
    refresh_transport_protocol_statuses,
)
from app.readiness import database_is_ready
from app.runtime_config import (
    admin_password,
    admin_username,
    aeza_api_base,
    aeza_ipv4_after_purchase_delay_seconds,
    aeza_ipv4_domain,
    aeza_service_id,
    aeza_token,
    router_api_enabled,
    setup_complete,
    safe_aeza_ip_rotation_enabled,
    transactional_vpn_apply_enabled,
    eu_ssh_host,
    eu_ssh_key_path,
    eu_ssh_password,
    eu_ssh_port,
    eu_ssh_user,
    vless_enabled,
    vless_port,
    vless_port_value,
    hysteria_enabled,
    hysteria_port,
    hysteria_port_value,
    amnezia_enabled as runtime_amnezia_enabled,
    amnezia_port,
    amnezia_port_value,
)
from app.stats import format_bytes, refresh_client_stats
from app.subscriptions import (
    render_sing_box_subscription,
    render_subscription,
)
from app.vpn_config import capture_vpn_config
from app.vpn_state import IpChangeSafetyHoldError, prepare_install_operation
from app.security import (
    SECURITY_HEADERS,
    csrf_failure_detail,
    client_key,
    hash_password,
    is_public_token_path,
    is_same_origin_request,
    login_rate_limiter,
    verify_password,
)
from app.secret_sanitization import sanitize_error
from app.server_operations import (
    create_protocol_refresh_operation,
    create_server_operation,
    get_active_vps_operation,
    get_latest_server_operation,
    get_server_operation,
    list_server_operations,
    run_server_reboot,
    run_server_status,
    run_protocol_refresh,
)
from app.router_api import RouterApiError, router, router_api_error_response
from app.router_credentials import (
    IssuedRouterCredential,
    RouterCredentialError,
    delete_router,
    get_router,
    issue_router_credential,
    list_routers,
    revoke_router_credential,
    rotate_router_credential,
    set_router_enabled,
    update_router_label,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    fail_incomplete_install_operations(
        "AutoVPN was restarted while this VPS operation was still running. Review its state before retrying."
    )
    yield


PACKAGE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="AutoVPN", lifespan=lifespan)
app.add_exception_handler(RouterApiError, router_api_error_response)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
security = HTTPBasic()
setup_security = HTTPBasic(auto_error=False)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CSRF: state-changing requests must come from the panel's own origin.
    if not is_same_origin_request(request):
        response = PlainTextResponse(
            csrf_failure_detail(request),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    else:
        response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if is_public_token_path(request.url.path):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def format_msk(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, UTC)
        else:
            text = str(value)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S МСК")
    except (TypeError, ValueError, OSError):
        return str(value)


def _required_port_value(raw_value: str, label: str) -> int:
    value = raw_value.strip()
    if not value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} port is required")
    try:
        port = int(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} port must be a number")
    if port < 1 or port > 65535:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} port must be between 1 and 65535")
    return port


def _ensure_unique_enabled_protocol_ports(protocol_settings: dict[str, tuple[bool, int]]) -> None:
    seen: dict[int, str] = {}
    for protocol, (enabled, port) in protocol_settings.items():
        if not enabled:
            continue
        if port in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Protocol ports must be unique: {seen[port]} and {protocol} both use {port}",
            )
        seen[port] = protocol


STATUS_LABELS = {
    "OK": "Сетевой probe прошёл",
    "VERIFIED": "OK",
    "SERVICE_ACTIVE": "Сервис активен",
    "TCP_REACHABLE": "TCP-порт доступен",
    "UDP_PACKET_SENT": "UDP-пакет отправлен",
    "FAILED": "Ошибка",
    "UNKNOWN": "Нет данных",
    "NOT_CONFIGURED": "Не настроено",
    "CONFIGURED_UDP": "UDP настроен",
    "PLACEHOLDER": "Пока не заведено",
    "RUNNING": "Выполняется",
    "DONE": "Готово",
    "PENDING": "Ожидает",
    "TIMED_OUT": "Истёк таймаут",
    "AMBIGUOUS": "Требуется проверка",
}


STEP_LABELS = {
    "get_ipv4_list": "получение списка IPv4",
    "find_current_main_ip": "поиск текущего главного IP",
    "create_new_ipv4": "покупка нового IPv4",
    "wait_after_ipv4_purchase": "ожидание после покупки IPv4",
    "wait_new_ipv4": "ожидание появления нового IPv4",
    "make_new_ipv4_main": "назначение нового IP главным",
    "confirm_new_ipv4_main": "подтверждение нового главного IP в Aeza",
    "reboot_service": "перезагрузка VPS",
    "wait_vps_health": "ожидание доступности VPS",
    "server_reachable": "сервер доступен",
    "update_current_ip": "обновление текущего IP",
    "sync_vpn_after_ip_change": "синхронизация VPN после смены IP",
    "refresh_protocol_statuses": "обновление статусов протоколов",
    "delete_old_ipv4": "удаление старого IPv4",
    "ssh_connect": "подключение по SSH",
    "remote_install_running": "удалённая установка выполняется",
    "done": "готово",
    "failed": "ошибка",
    "provider_status": "статус VPS в Aeza",
    "provider_status_before_reboot": "проверка статуса Aeza перед перезагрузкой",
    "ssh_reachability": "проверка SSH",
    "send_reboot": "отправка команды перезагрузки",
    "wait_reboot": "ожидание перезагрузки",
    "verify_reboot": "проверка после перезагрузки",
    "verify_ambiguous_reboot": "безопасная проверка результата",
    "service_health": "проверка сервисов",
    "protocol_health": "проверка протоколов",
    "wait_provider": "ожидание статуса Aeza",
    "wait_ssh": "ожидание SSH",
    "timed_out": "истёк таймаут",
    "ambiguous": "требуется ручная проверка",
    "manual_verification_required": "нужна ручная проверка",
    "verification_complete": "проверка завершена",
}


def status_label(value: object) -> str:
    text = str(value or "")
    return STATUS_LABELS.get(text, text)


def step_label(value: object) -> str:
    text = str(value or "")
    return STEP_LABELS.get(text, text)


templates.env.filters["msk"] = format_msk
templates.env.filters["status_label"] = status_label
templates.env.filters["step_label"] = step_label


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


def redirect_with_error(path: str, error: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?error={quote(error)}", status_code=status.HTTP_303_SEE_OTHER)


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
    port = amnezia_port()
    if port is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AmneziaWG is disabled")
    return build_amnezia_client_config(
        client,
        current_ip=current_ip,
        server_public_key=get_setting("amnezia.server_public_key"),
        obfuscation=get_amnezia_obfuscation(),
        endpoint_port=port,
    )


def get_amnezia_vpn_key(client: dict, current_ip: str) -> str:
    port = amnezia_port()
    if port is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AmneziaWG is disabled")
    return build_amnezia_vpn_key(
        client,
        current_ip=current_ip,
        server_public_key=get_setting("amnezia.server_public_key"),
        obfuscation=get_amnezia_obfuscation(),
        endpoint_port=port,
    )


def format_eur_minor_units(value: object) -> str:
    try:
        return f"€{float(value) / 100:.2f}"
    except (TypeError, ValueError):
        return "unknown"


def _verify_admin_credentials(request: Request, credentials: HTTPBasicCredentials) -> bool:
    """Constant-time check of admin credentials with login rate limiting."""
    key = client_key(request)
    retry_after = login_rate_limiter.retry_after(key)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    # Always evaluate both checks to keep timing roughly constant.
    username_ok = secrets.compare_digest(credentials.username, admin_username())
    password_ok = verify_password(credentials.password, admin_password())
    if username_ok and password_ok:
        login_rate_limiter.register_success(key)
        return True
    login_rate_limiter.register_failure(key)
    return False


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    if not setup_complete():
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/admin/setup"},
        )
    if not _verify_admin_credentials(request, credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_setup_access(
    request: Request,
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
    if not _verify_admin_credentials(request, credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    if not setup_complete():
        return RedirectResponse("/admin/setup")
    return RedirectResponse("/admin")


@app.get("/setup", response_class=HTMLResponse)
def setup_page_redirect(_: str | None = Depends(require_setup_access)) -> RedirectResponse:
    return RedirectResponse("/admin/setup")


@app.get("/admin/setup", response_class=HTMLResponse)
def admin_setup_page(
    request: Request,
    _: str | None = Depends(require_setup_access),
) -> HTMLResponse:
    current_ip = get_setting("current_ip")
    configured_ssh_host = eu_ssh_host()
    ssh_host_form_value = ""
    if configured_ssh_host and configured_ssh_host != current_ip:
        ssh_host_form_value = configured_ssh_host
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "admin_username": admin_username(),
            "current_ip": current_ip,
            "eu_ssh_host": ssh_host_form_value,
            "configured_eu_ssh_host": configured_ssh_host,
            "eu_ssh_user": eu_ssh_user(),
            "eu_ssh_port": eu_ssh_port(),
            "eu_ssh_key_path": eu_ssh_key_path(),
            "eu_ssh_password_configured": bool(eu_ssh_password()),
            "vless_enabled": vless_enabled(),
            "vless_port": vless_port_value(),
            "hysteria_enabled": hysteria_enabled(),
            "hysteria_port": hysteria_port_value(),
            "amnezia_enabled": runtime_amnezia_enabled(),
            "amnezia_port": amnezia_port_value(),
            "aeza_token_configured": bool(aeza_token()),
            "aeza_service_id": aeza_service_id(),
            "aeza_ipv4_domain": aeza_ipv4_domain(),
            "transactional_vpn_apply_enabled": transactional_vpn_apply_enabled(),
            "safe_aeza_ip_rotation_enabled": safe_aeza_ip_rotation_enabled(),
            "router_api_enabled": router_api_enabled(),
            "setup_complete": setup_complete(),
            "install_running": has_running_install_operation(),
            "operation_running": has_running_operation(),
        },
    )


@app.post("/setup")
@app.post("/admin/setup")
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
    vless_enabled_value: str | None = Form(None),
    vless_port_value: str = Form(""),
    hysteria_enabled_value: str | None = Form(None),
    hysteria_port_value: str = Form(""),
    amnezia_enabled_value: str | None = Form(None),
    amnezia_port_value: str = Form(""),
    autovpn2_settings_present: str | None = Form(None),
    transactional_vpn_apply_enabled_value: str | None = Form(None),
    safe_aeza_ip_rotation_enabled_value: str | None = Form(None),
    router_api_enabled_value: str | None = Form(None),
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

    current_ip_value = current_ip.strip()
    ssh_host_value = eu_ssh_host_value.strip()
    if ssh_host_value == current_ip_value:
        ssh_host_value = ""
    ssh_port = _required_port_value(str(eu_ssh_port_value), "SSH")
    existing_ssh_password = eu_ssh_password()
    ssh_key_path = eu_ssh_key_path_value.strip()
    protocol_settings = {
        "vless": (vless_enabled_value == "on", _required_port_value(vless_port_value, "VLESS")),
        "hysteria": (hysteria_enabled_value == "on", _required_port_value(hysteria_port_value, "Hysteria")),
        "amnezia": (amnezia_enabled_value == "on", _required_port_value(amnezia_port_value, "AmneziaWG")),
    }
    _ensure_unique_enabled_protocol_ports(protocol_settings)
    feature_updates: dict[str, str] = {}
    if autovpn2_settings_present == "1":
        transactional_apply_enabled = transactional_vpn_apply_enabled_value == "on"
        safe_rotation_enabled = safe_aeza_ip_rotation_enabled_value == "on"
        if safe_rotation_enabled and not transactional_apply_enabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Safe Aeza IP rotation requires transactional VPN apply",
            )
        feature_updates = {
            "config.enable_transactional_vpn_apply": "1" if transactional_apply_enabled else "0",
            "config.enable_safe_aeza_ip_rotation": "1" if safe_rotation_enabled else "0",
            "config.enable_router_api": "1" if router_api_enabled_value == "on" else "0",
        }

    updates = {
        "config.admin_username": admin_username_value.strip() or "admin",
        "config.eu_ssh_host": ssh_host_value,
        "config.eu_ssh_user": eu_ssh_user_value.strip() or "root",
        "config.eu_ssh_port": str(ssh_port),
        "config.aeza_api_base": "https://my.aeza.net",
        "config.aeza_service_id": aeza_service_id_value.strip(),
        "config.aeza_ipv4_payment_method": "balance",
        "config.aeza_ipv4_domain": aeza_ipv4_domain_value.strip(),
        "config.aeza_ipv4_after_purchase_delay_seconds": "120",
        **feature_updates,
    }
    if password:
        updates["config.admin_password"] = hash_password(password)
    if current_ip_value:
        updates["current_ip"] = current_ip_value
    for protocol, (enabled, port) in protocol_settings.items():
        updates[f"config.{protocol}_enabled"] = "1" if enabled else "0"
        updates[f"config.{protocol}_port"] = str(port)
    if eu_ssh_password_value:
        updates["config.eu_ssh_password"] = eu_ssh_password_value
        updates["config.eu_ssh_key_path"] = ""
    else:
        if not existing_ssh_password or ssh_key_path:
            updates["config.eu_ssh_password"] = ""
        updates["config.eu_ssh_key_path"] = ssh_key_path
    if aeza_token_value.strip():
        updates["config.aeza_token"] = aeza_token_value.strip()
    set_settings(updates, mark_vpn_config_updated=True)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    if database_is_ready(settings.database_path):
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    current_ip = get_setting("current_ip")
    clients = list_clients()
    target_host = resolve_eu_host()
    latest_install_operation = get_latest_install_operation()
    latest_server_operation = get_latest_server_operation()
    active_vps_operation = get_active_vps_operation()
    vpn_state = get_vpn_state()
    latest_install_error = latest_install_operation["error_message"] if latest_install_operation else ""
    ssh_host_key_changed = bool(
        latest_install_error
        and (
            "REMOTE HOST IDENTIFICATION HAS CHANGED" in latest_install_error
            or "Host key verification failed" in latest_install_error
        )
    )
    protocol_status_available = bool(
        current_ip
        and target_host
        and latest_install_operation
        and latest_install_operation["status"] == "DONE"
        and latest_install_operation["target_host"] == target_host
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_ip": current_ip,
            "target_host": target_host,
            "ssh_command": describe_ssh_command(target_host) if target_host else "",
            "enabled_clients_count": len([client for client in clients if client["enabled"]]),
            "protocols": get_protocol_statuses() if protocol_status_available else [],
            "protocol_status_available": protocol_status_available,
            "base_url": str(request.base_url).rstrip("/"),
            "latest_operation": get_latest_operation(),
            "latest_install_operation": latest_install_operation,
            "latest_server_operation": latest_server_operation,
            "active_vps_operation": active_vps_operation,
            "vpn_state": vpn_state,
            "vpn_config_dirty": vpn_state["applied_revision"] is None
            or vpn_state["desired_revision"] != vpn_state["applied_revision"],
            "ssh_host_key_changed": ssh_host_key_changed,
            "operation_running": has_running_operation(),
            "install_running": has_running_install_operation(),
            "auto_refresh": has_running_operation()
            or has_running_install_operation()
            or active_vps_operation is not None,
            "aeza_ip_rotation_available": aeza_ip_rotation_available(),
            "safe_aeza_ip_rotation_enabled": safe_aeza_ip_rotation_enabled(),
            "transactional_vpn_apply_enabled": transactional_vpn_apply_enabled(),
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "format_eur_minor_units": format_eur_minor_units,
        },
    )


@app.get("/admin/ip/confirm", response_class=HTMLResponse)
async def confirm_ip_refresh(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    safe_rotation_enabled = safe_aeza_ip_rotation_enabled()
    transactional_apply_enabled = transactional_vpn_apply_enabled()
    price = await fetch_aeza_ipv4_price() if safe_rotation_enabled else None
    return templates.TemplateResponse(
        request,
        "ip_confirm.html",
        {
            "current_ip": get_setting("current_ip"),
            "operation_running": has_running_operation(),
            "safe_rotation_enabled": safe_rotation_enabled,
            "transactional_apply_enabled": transactional_apply_enabled,
            "safety_hold": request.query_params.get("safety_hold") == "1",
            "aeza_ipv4_price": price,
            "after_purchase_delay_seconds": aeza_ipv4_after_purchase_delay_seconds(),
            "format_eur_minor_units": format_eur_minor_units,
        },
    )


@app.post("/admin/ip/refresh")
def refresh_ip(background_tasks: BackgroundTasks, _: str = Depends(require_admin)) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    if not safe_aeza_ip_rotation_enabled():
        return RedirectResponse(
            "/admin/ip/confirm?safe_rotation_required=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not transactional_vpn_apply_enabled():
        return RedirectResponse(
            "/admin/ip/confirm?transactional_apply_required=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        operation_id = create_operation()
    except IpChangeSafetyHoldError:
        return RedirectResponse(
            "/admin/ip/confirm?safety_hold=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except OperationBusyError:
        return RedirectResponse("/admin/ip/confirm?already_running=1", status_code=status.HTTP_303_SEE_OTHER)
    background_tasks.add_task(_run_operation_background, operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/ip", response_class=HTMLResponse)
async def admin_ip_manager(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    error = request.query_params.get("error", "")
    ipv4_list: list[dict] = []
    current_ip = get_setting("current_ip")
    try:
        client = get_aeza_client()
        ipv4_list = await client.get_ipv4_list(aeza_service_id())
        service = await client.get_service(aeza_service_id())
        aeza_main_ip = service.get("ip", "")
        if aeza_main_ip:
            for item in ipv4_list:
                if item.get("ip") == aeza_main_ip:
                    item["is_main"] = True
    except Exception as exc:
        error = error or str(exc)
    return templates.TemplateResponse(
        request,
        "ip_manager.html",
        {
            "current_ip": current_ip,
            "ipv4_list": ipv4_list,
            "error": error,
            "synced_main_ip": "",
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "format_eur_minor_units": format_eur_minor_units,
            "safe_rotation_enabled": safe_aeza_ip_rotation_enabled(),
        },
    )


@app.get("/admin/ip/buy/confirm", response_class=HTMLResponse)
async def admin_ip_buy_confirm(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    domain = aeza_ipv4_domain()
    return templates.TemplateResponse(
        request,
        "ip_buy_confirm.html",
        {
            "aeza_ipv4_price": await fetch_aeza_ipv4_price(),
            "aeza_ipv4_domain": domain,
            "error": request.query_params.get("error", ""),
            "format_eur_minor_units": format_eur_minor_units,
            "safe_rotation_enabled": safe_aeza_ip_rotation_enabled(),
        },
    )


@app.post("/admin/ip/buy")
async def admin_ip_buy(_: str = Depends(require_admin)) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        "/admin/ip/confirm?manual_disabled=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/ip/{ipv4_id}/make-main")
async def admin_ip_make_main(
    ipv4_id: str,
    ip: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    del ipv4_id, ip
    return RedirectResponse(
        "/admin/ip/confirm?manual_disabled=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/admin/ip/{ipv4_id}/delete")
async def admin_ip_delete(
    ipv4_id: str,
    ip: str = Form(...),
    is_main: str = Form("0"),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    del ipv4_id, ip, is_main
    return RedirectResponse(
        "/admin/ip/confirm?manual_disabled=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _run_operation_background(operation_id: int) -> None:
    try:
        await run_ip_change(operation_id)
    except Exception:
        # The operation runner has already persisted the failure details.
        return


@app.get(
    "/admin/ip/operations/{operation_id}/reconcile/confirm",
    response_class=HTMLResponse,
)
def admin_ip_reconcile_confirm(
    request: Request,
    operation_id: int,
    _: str = Depends(require_admin),
) -> HTMLResponse:
    operation = get_operation(operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    is_hold = operation["action_state"] != "RECONCILED" and (
        operation["status"] == "AMBIGUOUS"
        or operation["action_state"]
        in {"CLEANUP_AMBIGUOUS", "PUBLISHED_AWAITING_AWG_CHECK"}
    )
    if operation["status"] in {"PENDING", "RUNNING"} or not is_hold:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This IP operation does not have a terminal safety hold",
        )
    return templates.TemplateResponse(
        request,
        "ip_reconcile_confirm.html",
        {"operation": operation},
    )


@app.post("/admin/ip/operations/{operation_id}/reconcile")
def admin_ip_reconcile(
    operation_id: int,
    confirm: str = Form(""),
    note: str = Form(""),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if confirm.strip() != "RECONCILE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type RECONCILE after manually checking the IPv4 state in Aeza",
        )
    if not note.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A short reconciliation note is required",
        )
    try:
        reconcile_ip_operation(operation_id, note)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None
    return RedirectResponse(
        "/admin/operations#ip-operations",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/admin/clients", response_class=HTMLResponse)
def admin_clients(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "clients": list_clients_with_stats(),
            "base_url": str(request.base_url).rstrip("/"),
            "format_bytes": format_bytes,
            "hysteria_password": get_setting("hysteria.password"),
            "stats_last_refresh_at": get_setting("stats.last_refresh_at"),
            "stats_last_error": get_setting("stats.last_error"),
        },
    )


@app.post("/admin/clients")
def admin_create_client(
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    create_client(name.strip())
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/rename")
def admin_rename_client(
    client_id: int,
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    update_client_name(client_id, name.strip())
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/enable")
def admin_enable_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    set_client_enabled(client_id, True)
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/disable")
def admin_disable_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    set_client_enabled(client_id, False)
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/clients/{client_id}/delete")
def admin_delete_client(client_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
    delete_client(client_id)
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


def _router_token_response(
    request: Request,
    *,
    token: str,
    credential_id: str,
    router_id: str,
    router_label: str,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "router_token_once.html",
        {
            "token": token,
            "credential_id": credential_id,
            "router_id": router_id,
            "router_label": router_label,
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _issued_router_token_response(
    request: Request,
    issued: IssuedRouterCredential,
    router_label: str,
) -> HTMLResponse:
    return _router_token_response(
        request,
        token=issued.token,
        credential_id=issued.credential_id,
        router_id=issued.router_id,
        router_label=router_label,
    )


def _require_router_entry(router_id: str) -> dict:
    try:
        router_entry = get_router(router_id)
    except RouterCredentialError:
        router_entry = None
    if router_entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Router does not exist",
        )
    return router_entry


def _router_for_credential(credential_id: str) -> dict:
    for router_entry in list_routers():
        if any(
            credential["credential_id"] == credential_id
            for credential in router_entry["credentials"]
        ):
            return router_entry
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Router credential does not exist",
    )


@app.get("/admin/routers", response_class=HTMLResponse)
def admin_routers(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    clients = list_clients()
    routers = list_routers()
    assigned_client_ids = {int(router["client_id"]) for router in routers}
    return templates.TemplateResponse(
        request,
        "routers.html",
        {
            "routers": routers,
            "clients": clients,
            "available_clients": [
                client
                for client in clients
                if int(client["id"]) not in assigned_client_ids
            ],
            "clients_by_id": {int(client["id"]): client for client in clients},
        },
    )


@app.post("/admin/routers", response_class=HTMLResponse)
def admin_create_router(
    request: Request,
    name: str = Form(...),
    client_id: int = Form(...),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    router_label = name.strip()
    if not router_label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Router name is required",
        )
    try:
        issued = issue_router_credential(
            client_id,
            ("snapshot:read", "apply:write"),
            label=router_label,
        )
    except RouterCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    return _issued_router_token_response(request, issued, router_label)


@app.post("/admin/routers/{router_id}/rename")
def admin_rename_router(
    router_id: str,
    name: str = Form(...),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_router_entry(router_id)
    if not name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Router name is required",
        )
    try:
        update_router_label(router_id, name)
    except RouterCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    return RedirectResponse("/admin/routers", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/routers/{router_id}/enable")
def admin_enable_router(
    router_id: str,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_router_entry(router_id)
    set_router_enabled(router_id, True)
    return RedirectResponse("/admin/routers", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/routers/{router_id}/disable")
def admin_disable_router(
    router_id: str,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_router_entry(router_id)
    set_router_enabled(router_id, False)
    return RedirectResponse("/admin/routers", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/routers/{router_id}/delete")
def admin_delete_router(
    router_id: str,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    _require_router_entry(router_id)
    delete_router(router_id)
    return RedirectResponse("/admin/routers", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/routers/{router_id}/credentials", response_class=HTMLResponse)
def admin_issue_router_credential(
    request: Request,
    router_id: str,
    label: str = Form("replacement"),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    try:
        router_entry = _require_router_entry(router_id)
        issued = issue_router_credential(
            int(router_entry["client_id"]),
            ("snapshot:read", "apply:write"),
            label=label.strip(),
            router_id=router_id,
        )
    except RouterCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    return _issued_router_token_response(
        request,
        issued,
        str(router_entry["label"]),
    )


@app.post("/admin/router-credentials/{credential_id}/rotate", response_class=HTMLResponse)
def admin_rotate_router_credential(
    request: Request,
    credential_id: str,
    _: str = Depends(require_admin),
) -> HTMLResponse:
    router_entry = _router_for_credential(credential_id)
    try:
        token = rotate_router_credential(credential_id)
    except RouterCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    return _router_token_response(
        request,
        token=token,
        credential_id=credential_id,
        router_id=str(router_entry["router_id"]),
        router_label=str(router_entry["label"]),
    )


@app.post("/admin/router-credentials/{credential_id}/revoke")
def admin_revoke_router_credential(
    credential_id: str,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    try:
        revoke_router_credential(credential_id)
    except RouterCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    return RedirectResponse("/admin/routers", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/operations", response_class=HTMLResponse)
def admin_operations(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    operation_running = has_running_operation()
    install_running = has_running_install_operation()
    server_running = get_active_vps_operation() is not None
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "operations": list_operations(),
            "install_operations": list_install_operations(),
            "server_operations": list_server_operations(),
            "auto_refresh": operation_running or install_running or server_running,
        },
    )


@app.get("/admin/server", response_class=HTMLResponse)
def admin_server(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "server.html",
        {
            "latest_operation": get_latest_server_operation(),
            "active_operation": get_active_vps_operation(),
            "aeza_available": aeza_ip_rotation_available(),
            "service_id": aeza_service_id(),
            "current_ip": get_setting("current_ip"),
            "target_host": resolve_eu_host(),
            "aeza_required": request.query_params.get("aeza_required") == "1",
            "operation_busy": request.query_params.get("operation_busy") == "1",
        },
    )


@app.post("/admin/server/status/refresh")
def admin_server_status_refresh(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if not aeza_ip_rotation_available():
        return RedirectResponse(
            "/admin/server?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER
        )
    try:
        operation_id = create_server_operation("STATUS")
    except OperationBusyError:
        return RedirectResponse(
            "/admin/server?operation_busy=1", status_code=status.HTTP_303_SEE_OTHER
        )
    background_tasks.add_task(_run_server_status_background, operation_id)
    return RedirectResponse(
        f"/admin/server/operations/{operation_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/admin/server/reboot/confirm", response_class=HTMLResponse)
def admin_server_reboot_confirm(
    request: Request,
    _: str = Depends(require_admin),
) -> Response:
    if not aeza_ip_rotation_available():
        return RedirectResponse(
            "/admin/server?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER
        )
    return templates.TemplateResponse(
        request,
        "server_reboot_confirm.html",
        {
            "service_id": aeza_service_id(),
            "current_ip": get_setting("current_ip"),
            "latest_operation": get_latest_server_operation(),
            "active_operation": get_active_vps_operation(),
        },
    )


@app.post("/admin/server/reboot")
def admin_server_reboot(
    background_tasks: BackgroundTasks,
    confirm: str = Form(""),
    _: str = Depends(require_admin),
) -> RedirectResponse:
    if confirm != "REBOOT":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type REBOOT exactly to confirm server reboot",
        )
    if not aeza_ip_rotation_available():
        return RedirectResponse(
            "/admin/server?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER
        )
    try:
        operation_id = create_server_operation("REBOOT")
    except OperationBusyError:
        return RedirectResponse(
            "/admin/server?operation_busy=1", status_code=status.HTTP_303_SEE_OTHER
        )
    background_tasks.add_task(_run_server_reboot_background, operation_id)
    return RedirectResponse(
        f"/admin/server/operations/{operation_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/admin/server/operations/{operation_id}", response_class=HTMLResponse)
def admin_server_operation(
    request: Request,
    operation_id: int,
    _: str = Depends(require_admin),
) -> HTMLResponse:
    operation = get_server_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "server_operation.html",
        {
            "operation": operation,
            "auto_refresh": operation["status"] in {"PENDING", "RUNNING"},
        },
    )


async def _run_server_status_background(operation_id: int) -> None:
    await run_server_status(operation_id)


async def _run_server_reboot_background(operation_id: int) -> None:
    await run_server_reboot(operation_id)


@app.get("/admin/install")
def admin_install_redirect(_: str = Depends(require_admin)) -> RedirectResponse:
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/install/script", response_class=PlainTextResponse)
def admin_install_script(_: str = Depends(require_admin)) -> PlainTextResponse:
    return PlainTextResponse(build_eu_deploy_script())


@app.post("/admin/ssh/known-host/forget")
def admin_forget_ssh_known_host(_: str = Depends(require_admin)) -> RedirectResponse:
    if has_running_install_operation():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot change SSH known_hosts while install is running",
    )
    host = resolve_eu_host()
    forget_ssh_known_host(host)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/install/run")
def admin_run_install(
    background_tasks: BackgroundTasks,
    _: str = Depends(require_admin),
) -> RedirectResponse:
    host = resolve_eu_host()
    try:
        prepared = prepare_install_operation(host)
    except OperationBusyError:
        return RedirectResponse("/admin?install_already_running=1", status_code=status.HTTP_303_SEE_OTHER)
    background_tasks.add_task(_run_install_background, prepared.operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _run_install_background(operation_id: int) -> None:
    try:
        await run_eu_install(operation_id)
    except Exception:
        # The install runner has already persisted the failure details.
        return


@app.post("/admin/protocols/refresh")
def admin_refresh_protocols(background_tasks: BackgroundTasks, _: str = Depends(require_admin)) -> RedirectResponse:
    current_ip = get_setting("current_ip")
    target_host = resolve_eu_host()
    latest_install_operation = get_latest_install_operation()
    if (
        current_ip
        and target_host
        and latest_install_operation
        and latest_install_operation["status"] == "DONE"
        and latest_install_operation["target_host"] == target_host
    ):
        try:
            operation_id = create_protocol_refresh_operation()
        except OperationBusyError:
            return RedirectResponse(
                "/admin?operation_busy=1",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        background_tasks.add_task(_run_protocol_refresh_background, operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


async def _run_protocol_refresh_background(operation_id: int) -> None:
    await run_protocol_refresh(operation_id)


@app.post("/admin/stats/refresh")
def admin_refresh_stats(background_tasks: BackgroundTasks, _: str = Depends(require_admin)) -> RedirectResponse:
    background_tasks.add_task(_refresh_stats_background)
    return RedirectResponse("/admin/clients", status_code=status.HTTP_303_SEE_OTHER)


async def _refresh_stats_background() -> None:
    try:
        await refresh_client_stats()
    except Exception as exc:
        set_setting(
            "stats.last_error",
            sanitize_error(exc, aeza_token(), eu_ssh_password()),
        )


@app.post("/admin/reset-server")
def admin_reset_server(_: str = Depends(require_admin)) -> RedirectResponse:
    try:
        reset_server_and_aeza_state()
    except OperationBusyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot reset server settings while an operation is running",
        )
    return RedirectResponse("/admin/setup", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/sub/{token}", response_class=PlainTextResponse)
def subscription(token: str) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(render_subscription(config, client.as_dict()))


@app.get("/sing-box/{token}", response_class=JSONResponse)
def sing_box_subscription(token: str) -> JSONResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse(render_sing_box_subscription(config, client.as_dict()))


@app.get("/client/{token}", response_class=HTMLResponse)
async def client_page(request: Request, token: str) -> HTMLResponse:
    config = capture_vpn_config()
    captured_client = config.client_by_token(token)
    if not captured_client or not captured_client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    client = captured_client.as_dict()
    base_url = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "client_page.html",
        {
            "client": client,
            "current_ip": config.current_ip,
            "config_updated_at": config.config_updated_at,
            "protocols": get_client_protocol_statuses(),
            "protocol_status_available": bool(
                get_setting("protocol.vless.last_checked_at")
                or get_setting("protocol.vless.transport_last_checked_at")
                or get_setting("protocol.hysteria_quic.transport_last_checked_at")
                or get_setting("protocol.amnezia.transport_last_checked_at")
            ),
            "base_url": base_url,
            "subscription_url": f"{base_url}/sub/{client['token']}",
            "sing_box_subscription_url": f"{base_url}/sing-box/{client['token']}",
            "vless_enabled": config.vless.protocol.enabled,
            "hysteria_enabled": config.hysteria.protocol.enabled,
            "sing_box_enabled": config.vless.protocol.enabled or config.hysteria.protocol.enabled,
            "amnezia_enabled": config.amnezia.protocol.enabled,
            "amnezia_url": f"{base_url}/amnezia/{client['token']}",
            "amnezia_vpn_key": render_amnezia_vpn_key(config, captured_client)
            if config.amnezia.protocol.enabled
            else "",
            "amnezia_qr_url": f"{base_url}/client/{client['token']}/amnezia.qr",
        },
    )


@app.post("/client/{token}/protocols/refresh")
async def client_refresh_protocols(token: str) -> RedirectResponse:
    client = get_client_by_token(token)
    if not client or not client["enabled"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    current_ip = get_setting("current_ip")
    if current_ip:
        await refresh_transport_protocol_statuses(current_ip)
    return RedirectResponse(f"/client/{token}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/client/{token}/subscription.qr")
def client_subscription_qr(request: Request, token: str) -> Response:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    import qrcode

    subscription_url = f"{str(request.base_url).rstrip('/')}/sub/{client.token}"
    image = qrcode.make(subscription_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/client/{token}/amnezia.qr")
def client_amnezia_qr(token: str) -> Response:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    import qrcode

    image = qrcode.make(render_amnezia_vpn_key(config, client))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@app.get("/ip/{token}", response_class=PlainTextResponse)
def current_eu_ip(token: str) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(f"{config.current_ip}\n")


@app.get("/amnezia/{token}", response_class=PlainTextResponse)
def amnezia_config(token: str) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(
        render_amnezia_client_config(config, client),
        headers={
            "Content-Disposition": f"attachment; filename=amnezia-{client.id}.conf"
        },
    )


@app.get("/amnezia-key/{token}", response_class=PlainTextResponse)
def amnezia_vpn_key(token: str) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(render_amnezia_vpn_key(config, client) + "\n")
