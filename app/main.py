from __future__ import annotations

import secrets
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.amnezia_devices import device_list, config_for_device, prepare_device, run_device_sync
from app.clash import clash_profile, clash_provider, config_filename, dump_yaml
from app.config import settings
from app.aeza import AezaClient
from app.amnezia import (
    build_amnezia_client_config,
    build_amnezia_vpn_key,
    render_amnezia_client_config,
    render_amnezia_vpn_key,
)
from app.db import (
    public_vpn_version,
    create_client,
    create_operation,
    delete_client,
    fail_incomplete_install_operations,
    get_client_by_id,
    get_client_by_token,
    get_db,
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
from app.vps_provider import (
    provider_name, provider_available, provider_service_id, sprintbox_client, sprintbox_config,
    ip_rotation_available, first_sprintbox_box,
)
from app.sprintbox_review import (
    pending_review, pending_ssh_decision, prepare_ssh_wait, cancel_ssh_wait,
    pending_host_key_retry, prepare_host_key_retry,
)
from app.sprintbox_rotation import run_sprintbox_cleanup
from app.scheduled_cleanup import cleanup_queue, queue_cleanup_now, source_label, retain_queued_box
from app.sprintbox_accounts import (
    list_accounts, save_account, select_account, ensure_legacy_account, account as sprintbox_account,
    current_id as sprintbox_current_account_id,
)
from app.sprintbox import SprintboxClient

from app.anchors import list_anchors, anchor_client, has_anchor_access, set_anchor_access
from app.sprintbox_controls import (anchor_target, prepare_deletion, confirm_deletion, deletion, run_deletion, recheck_deletion)
from app.tuning import PROTOCOLS, FINGERPRINTS, profile_values, update_tuning
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
    hysteria_auth,
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
    from app.scheduled_cleanup import cleanup_loop
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)


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
    "CONFIRM":"Ожидает подтверждения",
    "SUPERSEDED":"Основной сервер изменён",
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
    "PURCHASE_SENDING": "Отправка запроса создания",
    "PURCHASE_SENT": "Ожидание создания",
    "CLEANUP_SCHEDULED": "Удаление назначено на 23:30 МСК",
    "AWAITING_USER_REVIEW": "Ожидает вашей проверки",
    "APPLYING": "Установка VPN",
    "RECONCILED": "Проверено вручную",
    "SSH_UNREACHABLE": "Нужен выбор: SSH недоступен",
    "SSH_WAIT_QUEUED": "Повторное ожидание SSH",
    "WAITING_SSH": "Ожидание SSH",
    "CANCELLED": "Отменено",
    "RETRY_REQUESTED": "Выбран другой бокс",
}


STEP_LABELS = {
    "anchor_queued":"переключение на якорь ожидает запуска",
    "inspect_anchor":"проверка якорного бокса",
    "configure_anchor":"применение текущих настроек к якорю",
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
    sprintbox_steps = {
        "inspect_old_box": "проверка старого бокса",
        "create_new_box": "создание нового бокса",
        "wait_new_box": "ожидание создания бокса",
        "identify_new_box": "определение нового IP",
        "install_new_box": "установка VPN на новый бокс",
        "delete_old_box": "удаление старого бокса",
        "wait_old_box_deletion": "ожидание удаления старого бокса",
        "awaiting_user_review": "ожидание вашей проверки VPN",
        "cleanup_scheduled": "удаление старых серверов в 23:30 МСК",
        "cleanup_queued": "ожидание удаления старых боксов",
        "ssh_decision": "SSH недоступен — выберите действие",
        "cancelled": "попытка отменена",
    }
    label = sprintbox_steps.get(text, STEP_LABELS.get(text, text))
    return label.replace("Aeza", "SprintBOX") if provider_name() == "sprintbox" else label


def format_recent_msk(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = (datetime.now(UTC) - parsed).total_seconds()
        if 0 <= seconds < 86400:
            if seconds < 60:
                return 'только что'
            count = int(seconds // (3600 if seconds >= 3600 else 60))
            forms = ('час', 'часа', 'часов') if seconds >= 3600 else ('минуту', 'минуты', 'минут')
            word = forms[2] if 11 <= count % 100 <= 14 else forms[0] if count % 10 == 1 else forms[1] if 2 <= count % 10 <= 4 else forms[2]
            return f'{count} {word} назад'
    except (ValueError, TypeError):
        pass
    return format_msk(value)


templates.env.filters["recent_msk"] = format_recent_msk
templates.env.filters["msk"] = format_msk
templates.env.filters["status_label"] = status_label
templates.env.filters["step_label"] = step_label
templates.env.globals["ip_change_source"] = source_label
templates.env.globals["vps_provider"] = provider_name
templates.env.globals["first_sprintbox_box"] = first_sprintbox_box
templates.env.globals["provider_label"] = lambda: "SprintBOX" if provider_name() == "sprintbox" else "Aeza"


def aeza_ip_rotation_available() -> bool:
    # Kept as a compatibility name for existing templates and callers.
    return ip_rotation_available()


async def fetch_aeza_ipv4_price() -> dict[str, object] | None:
    if provider_name() == "sprintbox" or not aeza_ip_rotation_available():
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
            "sprintbox": {key: value for key, value in sprintbox_config().items() if key != "key"},
            "sprintbox_key_configured": bool(sprintbox_config()["key"]),
            "sprintbox_accounts_managed": bool(list_accounts()),
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
    vps_provider_value: str | None = Form(None),
    sprintbox_login_value: str = Form(""),
    sprintbox_key_value: str = Form(""),
    sprintbox_box_name_value: str = Form(""),
    sprintbox_service_value: str = Form(""),
    sprintbox_distributive_value: str = Form(""),
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
    if vps_provider_value is not None:
        if vps_provider_value not in {"aeza", "sprintbox"}:
            raise HTTPException(status_code=400, detail="Unknown VPS provider")
        if get_active_vps_operation():
            raise HTTPException(status_code=409, detail="Wait for the active VPS operation before changing provider settings")
        updates["config.vps_provider"] = vps_provider_value
        if list_accounts():
            if any((sprintbox_login_value, sprintbox_key_value, sprintbox_box_name_value,
                    sprintbox_service_value, sprintbox_distributive_value)):
                raise HTTPException(status_code=400, detail="Настройте аккаунты на странице /admin/sprintbox/accounts")
        else:
            for key, value in {
                "login": sprintbox_login_value, "box_name": sprintbox_box_name_value,
                "service": sprintbox_service_value, "distributive": sprintbox_distributive_value,
            }.items():
                if "\n" in value or "\r" in value or len(value) > 256:
                    raise HTTPException(status_code=400, detail="Invalid SprintBOX setting")
                updates[f"config.sprintbox_{key}"] = value.strip()
            if sprintbox_key_value.strip():
                if "\n" in sprintbox_key_value or "\r" in sprintbox_key_value:
                    raise HTTPException(status_code=400, detail="Invalid SprintBOX key")
                updates["config.sprintbox_key"] = sprintbox_key_value.strip()
    try:
        set_settings(updates, mark_vpn_config_updated=True, require_vps_idle=vps_provider_value is not None)
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            ensure_legacy_account(db)
    except OperationBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
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
            "cleanup_queue": cleanup_queue(),
            "sprintbox_review": pending_review() if provider_name() == "sprintbox" else None,
            "sprintbox_ssh_decision": pending_ssh_decision(),
            "sprintbox_host_key_retry": pending_host_key_retry(),
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
            "sprintbox_needs_client": provider_name() == "sprintbox" and not any(client["enabled"] for client in list_clients()),
            "sprintbox": {key: value for key, value in sprintbox_config().items() if key != "key"},
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


@app.post("/admin/sprintbox/{operation_id}/accept")
def accept_sprintbox(operation_id: int,
                     keep_boxes: list[str] = Form([]),
                     _: str = Depends(require_admin)) -> RedirectResponse:
    try:
        from app.scheduled_cleanup import schedule_cleanup
        schedule_cleanup(operation_id, keep_boxes=keep_boxes)
    except (ValueError, OperationBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/sprintbox/{operation_id}/keep-anchor')
def keep_queued_anchor(operation_id: int, box: str = Form(...), _: str = Depends(require_admin)):
    try:
        retain_queued_box(operation_id, box)
    except (ValueError, OperationBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse('/admin', status_code=303)


@app.post('/admin/sprintbox/{operation_id}/cleanup-now')
def cleanup_sprintbox_now(operation_id: int, background_tasks: BackgroundTasks,
                          _: str = Depends(require_admin)) -> RedirectResponse:
    try:
        queue_cleanup_now(operation_id)
    except (ValueError, OperationBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(run_sprintbox_cleanup, operation_id)
    return RedirectResponse('/admin', status_code=303)


def _sprintbox_accounts_page(request: Request, *, error: str = "", checked: dict | None = None,
                             status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, "sprintbox_accounts.html", {
        "accounts": list_accounts(), "error": error, "checked": checked,
        "busy": get_active_vps_operation() is not None,
    }, status_code=status_code, headers={"Cache-Control": "private, no-store"})


@app.get("/admin/sprintbox/accounts", response_class=HTMLResponse)
async def admin_sprintbox_accounts(request: Request, check: int | None = None,
                                   _: str = Depends(require_admin)) -> HTMLResponse:
    checked = None
    error = ""
    if check is not None:
        with get_db() as db:
            try:
                cfg = sprintbox_account(db, check)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from None
        try:
            client = SprintboxClient(cfg['login'], cfg['key'])
            boxes, services, distributives = await asyncio.gather(
                client.list_boxes(), client._request('services/list'), client._request('distributives/list'))
            if not isinstance(services, dict) or not isinstance(distributives, dict):
                raise ValueError('SprintBOX вернул некорректный список тарифов или образов')
            def options(items):
                return [{'name': name, 'description': str(value.get('description', name)), 'fee': value.get('fee')}
                        for name, value in items.items() if isinstance(value, dict)]
            checked = {'id': check, 'boxes_count': len(boxes), 'services': options(services),
                       'distributives': options(distributives), 'service_available': cfg['service'] in services}
        except Exception as exc:
            error = sanitize_error(exc, cfg['key'], cfg['login'])
    return _sprintbox_accounts_page(request, error=error, checked=checked)


@app.post("/admin/sprintbox/accounts")
def admin_add_sprintbox_account(request: Request, label: str = Form(...), login: str = Form(...),
                                api_key: str = Form(...), service: str = Form(""),
                                distributive: str = Form("ubuntu2204"), _: str = Depends(require_admin)):
    try:
        owner = save_account(label=label, login=login, key=api_key, service=service, distributive=distributive)
    except (ValueError, OperationBusyError) as exc:
        return _sprintbox_accounts_page(request, error=str(exc), status_code=409 if isinstance(exc, OperationBusyError) else 400)
    return RedirectResponse(f"/admin/sprintbox/accounts?check={owner}", status_code=303)


@app.post("/admin/sprintbox/accounts/{account_id}/save")
def admin_save_sprintbox_account(request: Request, account_id: int, label: str = Form(...),
                                 api_key: str = Form(""), service: str = Form(""),
                                 distributive: str = Form(""), _: str = Depends(require_admin)):
    try:
        with get_db() as db:
            cfg = sprintbox_account(db, account_id)
        save_account(account_id=account_id, label=label, login=cfg['login'], key=api_key,
                     service=service, distributive=distributive)
    except (ValueError, OperationBusyError) as exc:
        return _sprintbox_accounts_page(request, error=str(exc), status_code=409 if isinstance(exc, OperationBusyError) else 400)
    return RedirectResponse('/admin/sprintbox/accounts', status_code=303)


@app.post("/admin/sprintbox/accounts/{account_id}/select")
def admin_select_sprintbox_account(request: Request, account_id: int, _: str = Depends(require_admin)):
    try:
        select_account(account_id)
    except (ValueError, OperationBusyError) as exc:
        return _sprintbox_accounts_page(request, error=str(exc), status_code=409)
    return RedirectResponse('/admin/sprintbox/accounts', status_code=303)


@app.get("/admin/anchors", response_class=HTMLResponse)
def admin_anchors(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(request, "anchors.html", {
        "anchors": list_anchors(), "busy":get_active_vps_operation() is not None,
    }, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@app.post('/admin/anchors/{anchor_id}/access')
def admin_anchor_access(anchor_id: str, client_ids: list[int] = Form(default=[]), _: str = Depends(require_admin)):
    try:
        set_anchor_access(anchor_id, client_ids)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse('/admin/anchors', status_code=303)


@app.get("/admin/anchors/{anchor_id}/promote/confirm", response_class=HTMLResponse)
def confirm_anchor_promotion(request: Request, anchor_id: str, _: str = Depends(require_admin)):
    try:
        with get_db() as db:
            target = anchor_target(db, anchor_id)
            owner = sprintbox_account(db, target['account_id'])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return templates.TemplateResponse(request, 'anchor_promote_confirm.html', {
        'target': {k:target[k] for k in ('anchor_id','box_name','ip')},
        'account_label': owner['label'], 'current_ip': get_setting('current_ip'),
        'busy': get_active_vps_operation() is not None, 'ssh_decision':pending_ssh_decision(),
    }, headers={'Cache-Control':'private, no-store'})


@app.post("/admin/anchors/{anchor_id}/promote")
def promote_anchor(anchor_id: str, background_tasks: BackgroundTasks, confirm_ip: str = Form(''),
                   _: str = Depends(require_admin)):
    from app.vpn_state import prepare_ip_change_operation
    try:
        with get_db() as db:
            target = anchor_target(db, anchor_id)
        if confirm_ip.strip() != target['ip']:
            raise ValueError('Введите IP якоря для подтверждения переключения')
        prepared = prepare_ip_change_operation(anchor_id=anchor_id)
    except (ValueError, OperationBusyError, IpChangeSafetyHoldError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(_run_operation_background, prepared.operation_id)
    return RedirectResponse('/admin', status_code=303)


def list_deletions() -> list[dict]:
    with get_db() as db:
        return db.execute("SELECT d.id,d.box_name,d.status,d.error_message,a.label AS account_label "
                          "FROM sprintbox_deletions d JOIN sprintbox_accounts a ON a.id=d.account_id "
                          "WHERE d.status!='CONFIRM' ORDER BY d.id DESC LIMIT 30").fetchall()


@app.get('/admin/sprintbox/accounts/{account_id}/boxes/{box_name}/delete/confirm')
async def confirm_box_deletion(account_id: int, box_name: str, _: str = Depends(require_admin)):
    try:
        operation_id = await prepare_deletion(account_id, box_name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        with get_db() as db: cfg=sprintbox_account(db,account_id)
        raise HTTPException(status_code=502, detail=sanitize_error(exc,cfg['login'],cfg['key'])) from None
    return RedirectResponse(f'/admin/sprintbox/deletions/{operation_id}', status_code=303)


@app.get('/admin/sprintbox/deletions/{operation_id}', response_class=HTMLResponse)
def show_box_deletion(request: Request, operation_id: int, _: str = Depends(require_admin)):
    try:
        operation=deletion(operation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return templates.TemplateResponse(request,'box_delete_confirm.html', {
        'operation':operation, 'auto_refresh':operation['status'] in {'PENDING','RUNNING'},
        'busy':get_active_vps_operation() is not None,
    }, headers={'Cache-Control':'private, no-store'})


@app.post('/admin/sprintbox/deletions/{operation_id}/confirm')
def delete_box(operation_id: int, background_tasks: BackgroundTasks, confirm_name: str = Form(''),
               delete_anchor: str = Form(''), _: str = Depends(require_admin)):
    try:
        confirm_deletion(operation_id,confirm_name,delete_anchor=='on')
    except (ValueError,OperationBusyError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from None
    background_tasks.add_task(run_deletion,operation_id)
    return RedirectResponse(f'/admin/sprintbox/deletions/{operation_id}',status_code=303)


@app.post('/admin/sprintbox/deletions/{operation_id}/recheck')
async def recheck_box_deletion(operation_id: int, _: str = Depends(require_admin)):
    try:
        await recheck_deletion(operation_id)
    except (ValueError,OperationBusyError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from None
    except Exception as exc:
        row=deletion(operation_id)
        with get_db() as db: cfg=sprintbox_account(db,row['account_id'])
        raise HTTPException(status_code=502,detail=sanitize_error(exc,cfg['login'],cfg['key'])) from None
    return RedirectResponse(f'/admin/sprintbox/deletions/{operation_id}',status_code=303)


@app.get("/admin/tuning", response_class=HTMLResponse)
def admin_tuning(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    return _tuning_page(request)


def _tuning_page(request: Request, *, error: str = "", submitted_protocol: str = "",
                 submitted: dict | None = None, code: int = 200) -> HTMLResponse:
    config = capture_vpn_config()
    profiles = {name: profile_values(config, name) for name in PROTOCOLS}
    if submitted_protocol in profiles and submitted is not None:
        profiles[submitted_protocol].update({key: submitted[key] for key in profiles[submitted_protocol] if key in submitted})
    return templates.TemplateResponse(request, "tuning.html", {
        "profiles": profiles, "fingerprints": FINGERPRINTS, "error": error,
        "vpn_state": get_vpn_state(), "busy": get_active_vps_operation() is not None,
    }, status_code=code, headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@app.post("/admin/tuning/{protocol}", response_class=HTMLResponse)
async def save_tuning(request: Request, protocol: str, _: str = Depends(require_admin)):
    form = await request.form()
    submitted = {key: str(value).strip() for key, value in form.items()}
    try:
        update_tuning(protocol, submitted.get("action", "save"), submitted)
    except ValueError as exc:
        return _tuning_page(request, error=str(exc), submitted_protocol=protocol, submitted=submitted, code=400)
    except OperationBusyError:
        return _tuning_page(request, error="Дождитесь завершения текущей операции с VPS", code=409)
    return RedirectResponse("/admin/tuning?saved=1", status_code=303)


@app.post("/admin/sprintbox/{operation_id}/retry")
def retry_sprintbox(operation_id: int, background_tasks: BackgroundTasks,
                    _: str = Depends(require_admin)) -> RedirectResponse:
    if not safe_aeza_ip_rotation_enabled() or not transactional_vpn_apply_enabled():
        raise HTTPException(status_code=409, detail="Enable safe rotation and transactional VPN apply first")
    from app.vpn_state import prepare_ip_change_operation
    try:
        prepared = prepare_ip_change_operation(review_operation_id=operation_id)
    except (ValueError, OperationBusyError, IpChangeSafetyHoldError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(_run_operation_background, prepared.operation_id)
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/sprintbox/{operation_id}/ssh/{decision}")
def decide_sprintbox_ssh(operation_id: int, decision: str, background_tasks: BackgroundTasks,
                         _: str = Depends(require_admin)) -> RedirectResponse:
    if decision not in {"wait", "retry", "cancel", "host-key"}:
        raise HTTPException(status_code=404)
    if decision != "cancel" and (not safe_aeza_ip_rotation_enabled() or not transactional_vpn_apply_enabled()):
        raise HTTPException(status_code=409, detail="Enable safe rotation and transactional VPN apply first")
    try:
        if decision == "cancel":
            cancel_ssh_wait(operation_id)
        elif decision == "host-key":
            prepare_host_key_retry(operation_id)
            background_tasks.add_task(_run_operation_background, operation_id)
        elif decision == "wait":
            prepare_ssh_wait(operation_id)
            background_tasks.add_task(_run_operation_background, operation_id)
        else:
            from app.vpn_state import prepare_ip_change_operation
            prepared = prepare_ip_change_operation(ssh_decision_id=operation_id)
            background_tasks.add_task(_run_operation_background, prepared.operation_id)
    except (ValueError, OperationBusyError, IpChangeSafetyHoldError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/ip", response_class=HTMLResponse)
async def admin_ip_manager(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    if provider_name() != "sprintbox" and not aeza_ip_rotation_available():
        return RedirectResponse("/admin?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER)
    error = request.query_params.get("error", "")
    ipv4_list: list[dict] = []
    current_ip = get_setting("current_ip")
    if provider_name() == "sprintbox":
        inventories = []
        accounts = list_accounts()
        configs = []
        if accounts:
            with get_db() as db:
                active_owner = sprintbox_current_account_id(db)
                configs = [sprintbox_account(db, item['id']) for item in accounts]
        else:
            active_owner = ''
            configs = [sprintbox_config(current=True)]
        for cfg in configs:
            inventory = {'label': cfg.get('label', cfg['login']), 'boxes': {}, 'error': '',
                         'current': cfg.get('account_id', '') == active_owner, 'account_id':cfg.get('account_id')}
            try:
                raw = await SprintboxClient(cfg['login'], cfg['key']).list_boxes()
                inventory['boxes'] = {name: {key: box.get(key, []) if key == 'ipv4' else box.get(key, '')
                                            for key in ('ipv4', 'state_description')} for name, box in raw.items()}
            except Exception as exc:
                inventory['error'] = sanitize_error(exc, cfg['key'], cfg['login'])
            inventories.append(inventory)
        return templates.TemplateResponse(request, 'sprintbox_boxes.html', {
            'inventories': inventories, 'current_box': provider_service_id(), 'current_ip': current_ip, 'error': error,
            'busy':get_active_vps_operation() is not None, 'deletions':list_deletions(),
        }, headers={'Cache-Control': 'private, no-store'})
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
    routers_by_client_id = {
        int(router_entry["client_id"]): router_entry
        for router_entry in list_routers()
    }
    clients = list_clients_with_stats()
    for client in clients:
        client["router"] = routers_by_client_id.get(int(client["id"]))
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "clients": clients,
            "base_url": str(request.base_url).rstrip("/"),
            "format_bytes": format_bytes,
            "hysteria_auth": hysteria_auth,
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
            "deletions": list_deletions(),
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
            "aeza_available": provider_available(),
            "service_id": provider_service_id(),
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
    if not provider_available():
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
    if not provider_available():
        return RedirectResponse(
            "/admin/server?aeza_required=1", status_code=status.HTTP_303_SEE_OTHER
        )
    return templates.TemplateResponse(
        request,
        "server_reboot_confirm.html",
        {
            "service_id": provider_service_id(),
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
    if not provider_available():
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
    return JSONResponse(render_sing_box_subscription(config, client.as_dict()), headers={"Content-Disposition": f'attachment; filename="{config_filename("singbox", client.as_dict(), public_vpn_version(), "json")}"'})


def user_ip_change_state(client_id: int) -> dict:
    enabled = (provider_name() == 'sprintbox' and safe_aeza_ip_rotation_enabled()
               and transactional_vpn_apply_enabled() and bool(get_setting('current_ip')))
    with get_db() as db:
        row = db.execute("SELECT o.* FROM ip_change_operations o JOIN settings s "
                         "ON s.key='ip_change.' || o.id || '.client_id' "
                         "WHERE s.value=? ORDER BY o.id DESC LIMIT 1", (str(client_id),)).fetchone()
        from app.sprintbox_review import has_pending_review
        pending = has_pending_review(db)
        due = None
        if row:
            from app.sprintbox_review import context
            due = context(db, row['id']).get('cleanup_at')
    return {'enabled': enabled, 'operation': row, 'cleanup_at': due,
            'busy': get_active_vps_operation() is not None, 'pending_review': bool(pending)}


def require_vpn_user(token: str) -> dict:
    client = get_client_by_token(token)
    if not client or not client['enabled']:
        raise HTTPException(status_code=404)
    return client


@app.get('/client/service-worker.js')
def client_service_worker() -> Response:
    return Response((PACKAGE_DIR / 'static/pwa/service-worker.js').read_text(),
                    media_type='text/javascript', headers={'Cache-Control': 'private, no-store',
                                                          'Service-Worker-Allowed': '/client/'})


@app.get('/client/{token}/manifest.webmanifest')
def client_manifest(token: str) -> JSONResponse:
    require_vpn_user(token)
    start = f'/client/{token}'
    return JSONResponse({
        'id': start, 'name': 'AutoVPN', 'short_name': 'AutoVPN', 'lang': 'ru',
        'description': 'Личная страница AutoVPN: подключения и смена IP',
        'start_url': start, 'scope': '/client/', 'display': 'standalone',
        'background_color': '#0d1117', 'theme_color': '#0d1117',
        'icons': [{'src': f'/static/pwa/icon-{size}.png', 'sizes': f'{size}x{size}',
                   'type': 'image/png', 'purpose': 'any maskable'} for size in (192, 512)],
    }, media_type='application/manifest+json', headers={'Cache-Control': 'private, no-store'})


@app.post('/client/{token}/ip-change')
def start_user_ip_change(token: str, background_tasks: BackgroundTasks) -> RedirectResponse:
    client = require_vpn_user(token)
    if not user_ip_change_state(client['id'])['enabled']:
        raise HTTPException(status_code=409, detail='Смена IP сейчас недоступна')
    from app.vpn_state import prepare_ip_change_operation
    try:
        prepared = prepare_ip_change_operation(user_client_id=client['id'])
    except (ValueError, OperationBusyError, IpChangeSafetyHoldError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(_run_operation_background, prepared.operation_id)
    return RedirectResponse(f'/client/{token}', status_code=303)


@app.post('/client/{token}/ip-change/{operation_id}/{decision}')
def decide_user_ip_change(token: str, operation_id: int, decision: str,
                          background_tasks: BackgroundTasks) -> RedirectResponse:
    client = require_vpn_user(token)
    from app.scheduled_cleanup import require_owner, schedule_cleanup
    from app.vpn_state import prepare_ip_change_operation
    try:
        with get_db() as db:
            require_owner(db, operation_id, client['id'])
        if decision == 'accept':
            schedule_cleanup(operation_id, client_id=client['id'])
        elif decision in {'retry', 'ssh-retry', 'ssh-wait', 'ssh-cancel'}:
            if decision != 'ssh-cancel' and not user_ip_change_state(client['id'])['enabled']:
                raise ValueError('Смена IP сейчас недоступна')
            if decision == 'ssh-cancel':
                cancel_ssh_wait(operation_id)
            elif decision == 'ssh-wait':
                prepare_ssh_wait(operation_id)
                background_tasks.add_task(_run_operation_background, operation_id)
            else:
                args = {'review_operation_id' if decision == 'retry' else 'ssh_decision_id': operation_id}
                prepared = prepare_ip_change_operation(user_client_id=client['id'], **args)
                background_tasks.add_task(_run_operation_background, prepared.operation_id)
        else:
            raise HTTPException(status_code=404)
    except (ValueError, OperationBusyError, IpChangeSafetyHoldError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return RedirectResponse(f'/client/{token}', status_code=303)


def client_protocol_overview() -> tuple[list[dict], str | None]:
    protocols = get_client_protocol_statuses()
    checks = []
    for protocol in protocols:
        state = protocol['status']
        # Amnezia's deep probe verifies peer configuration, not a VPN handshake.
        protocol['client_status'] = (
            'OK' if state == 'VERIFIED' and protocol['key'] != 'amnezia'
            else 'FAILED' if state == 'FAILED' else 'UNKNOWN'
        )
        for key in ('last_checked_at', 'transport_last_checked_at'):
            value = protocol.get(key)
            if value:
                try:
                    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                    checks.append((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC), str(value)))
                except ValueError:
                    pass
    return protocols, max(checks, key=lambda item: item[0])[1] if checks else None


@app.get('/clash/{token}')
@app.get('/clash-provider/{token}')
def client_clash_config(request: Request, token: str):
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=404)
    if not config.current_ip:
        raise HTTPException(status_code=503)
    if not (config.vless.protocol.enabled or config.hysteria.protocol.enabled):
        raise HTTPException(status_code=404)
    provider = request.url.path.startswith('/clash-provider/')
    value = clash_provider(config, client.as_dict()) if provider else clash_profile(config, client.as_dict(), str(request.base_url).rstrip('/'))
    filename = config_filename('clash', client.as_dict(), public_vpn_version(), 'yaml')
    return Response(dump_yaml(value), media_type='application/yaml', headers={
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Cache-Control': 'private, no-store', 'profile-update-interval': '1',
    })


@app.post('/client/{token}/amnezia-devices')
def create_user_amnezia_device(token: str, background_tasks: BackgroundTasks, name: str = Form(...)):
    client = require_vpn_user(token)
    try:
        device_id, operation_id = prepare_device(client['id'], name=name)
    except (ValueError, OperationBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if operation_id is not None:
        background_tasks.add_task(run_device_sync, operation_id)
    return RedirectResponse(f'/client/{token}?device={device_id}#amnezia-section',status_code=303)


@app.post('/client/{token}/amnezia-devices/{device_id}/delete')
def delete_user_amnezia_device(token: str, device_id: int, background_tasks: BackgroundTasks):
    client = require_vpn_user(token)
    try:
        _, operation_id = prepare_device(client['id'], delete_id=device_id)
    except (ValueError, OperationBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    background_tasks.add_task(run_device_sync,operation_id)
    return RedirectResponse(f'/client/{token}#amnezia-section',status_code=303)


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
    protocols, protocols_checked_at = client_protocol_overview()
    devices = device_list(client['id'])
    device_vpn_state = get_vpn_state()
    device_management_available = (device_vpn_state['applied_revision'] == device_vpn_state['desired_revision'] and get_active_vps_operation() is None)
    selected_id = request.query_params.get('device','mobile')
    selected_device = next((item for item in devices if str(item['id']) == selected_id),None)
    if selected_device is None:
        raise HTTPException(status_code=404)
    device_config, device_client = config, captured_client
    if not selected_device['legacy'] and selected_device['ready']:
        try:
            device_config,device_client = config_for_device(config,captured_client,int(selected_id))
        except (LookupError,ValueError):
            selected_device['ready'] = False
    suffix = '' if selected_device['legacy'] else f'?device={selected_id}'

    return templates.TemplateResponse(
        request,
        "client_page.html",
        {
            "client": client,
            "amnezia_devices": devices, "selected_device": selected_device,
            "user_anchors": list_anchors(token),
            "amnezia_device_pending": any(item.get("pending_deletion") or (not item["legacy"] and not item["ready"] and not item["failed"]) for item in devices),
            "device_management_available": device_management_available,
            "current_ip": config.current_ip,
            "config_updated_at": get_setting("vpn.public_updated_at", config.config_updated_at),
            "protocols": protocols,
            "protocols_checked_at": protocols_checked_at,
            "base_url": base_url,
            "vpn_revision": public_vpn_version(),
            "clash_url": f"{base_url}/clash/{client['token']}",
            "subscription_url": f"{base_url}/sub/{client['token']}",
            "neko_import_url": "sn://subscription?" + urlencode({
                "url": f"{base_url}/sub/{client['token']}",
                "name": f"AutoVPN · {client['name']}",
            }),
            "sing_box_subscription_url": f"{base_url}/sing-box/{client['token']}",
            "vless_enabled": config.vless.protocol.enabled,
            "hysteria_enabled": config.hysteria.protocol.enabled,
            "sing_box_enabled": config.vless.protocol.enabled or config.hysteria.protocol.enabled,
            "amnezia_enabled": config.amnezia.protocol.enabled,
            "amnezia_url": f"{base_url}/amnezia/{client['token']}{suffix}",
            "amnezia_vpn_key": render_amnezia_vpn_key(device_config, device_client)
            if config.amnezia.protocol.enabled and selected_device["ready"]
            else "",
            "amnezia_qr_url": f"{base_url}/client/{client['token']}/amnezia.qr{suffix}",
            "ip_change": user_ip_change_state(client["id"]),
        },
    )


@app.get("/anchor/{anchor_id}/{kind}/{token}")
def anchor_resource(request: Request, anchor_id: str, kind: str, token: str, credentials: HTTPBasicCredentials | None = Depends(setup_security)):
    if not has_anchor_access(anchor_id, token):
        if credentials is None:
            raise HTTPException(status_code=401, headers={'WWW-Authenticate': 'Basic'})
        require_admin(request, credentials)
    if kind not in {"client", "sub", "sing-box", "clash", "clash-provider", "amnezia", "amnezia-key", "subscription.qr", "amnezia.qr"}:
        raise HTTPException(status_code=404)
    bound = anchor_client(anchor_id, token)
    if bound is None:
        raise HTTPException(status_code=404)
    anchor, config, client = bound
    client = replace(client, name=f"{client.name} · Якорь {anchor['ip']}")
    subscription_enabled = config.vless.protocol.enabled or config.hysteria.protocol.enabled
    if kind in {"amnezia", "amnezia-key", "amnezia.qr"} and not config.amnezia.protocol.enabled:
        raise HTTPException(status_code=404)
    if kind in {"sub", "sing-box", "clash", "clash-provider", "subscription.qr"} and not subscription_enabled:
        raise HTTPException(status_code=404)
    prefix = f"{str(request.base_url).rstrip('/')}/anchor/{anchor_id}"
    if kind == "client":
        return templates.TemplateResponse(request, "anchor_client.html", {
            "anchor": anchor, "client": client, "amnezia_enabled": config.amnezia.protocol.enabled,
            "prefix": prefix, "subscription_enabled": subscription_enabled,
            "vless_enabled": config.vless.protocol.enabled, "hysteria_enabled": config.hysteria.protocol.enabled,
            "import_url": 'sn://subscription?' + urlencode({'url': f'{prefix}/sub/{token}', 'name': client.name}),
            "amnezia_key": render_amnezia_vpn_key(config, client) if config.amnezia.protocol.enabled else "",
        })
    if kind in {'clash', 'clash-provider'}:
        value = clash_provider(config, client.as_dict()) if kind == 'clash-provider' else clash_profile(config, client.as_dict(), str(request.base_url).rstrip('/'))
        if kind == 'clash':
            for provider in value['proxy-providers'].values():
                provider['url'] = f'{prefix}/clash-provider/{token}'
        return Response(dump_yaml(value), media_type='application/yaml', headers={'Content-Disposition': f'attachment; filename="{config_filename("anchor-clash", client.as_dict(), config.revision, "yaml")}"'})
    if kind == "sub":
        return PlainTextResponse(render_subscription(config, client.as_dict()))
    if kind == "sing-box":
        return JSONResponse(render_sing_box_subscription(config, client.as_dict()), headers={"Content-Disposition": f'attachment; filename="{config_filename("anchor-singbox", client.as_dict(), config.revision, "json")}"'})
    if kind == "amnezia":
        return PlainTextResponse(render_amnezia_client_config(config, client), headers={
            "Content-Disposition": f'attachment; filename="{config_filename("anchor-amnez", client.as_dict(), config.revision, "conf")}"',
        })
    if kind == "amnezia-key":
        return PlainTextResponse(render_amnezia_vpn_key(config, client) + "\n")
    import qrcode
    payload = f"{prefix}/sub/{token}" if kind == "subscription.qr" else render_amnezia_vpn_key(config, client)
    buffer = BytesIO()
    qrcode.make(payload).save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


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
def client_amnezia_qr(token: str, device: int | None = None) -> Response:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        config,client = config_for_device(config,client,device)
    except LookupError:
        raise HTTPException(status_code=404) from None
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from None
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
def amnezia_config(token: str, device: int | None = None) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        config,client = config_for_device(config,client,device)
    except LookupError:
        raise HTTPException(status_code=404) from None
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from None
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(
        render_amnezia_client_config(config, client),
        headers={
            "Content-Disposition": f'attachment; filename="{config_filename("amnez", client.as_dict(), public_vpn_version(), "conf")}"'
        },
    )


@app.get("/amnezia-key/{token}", response_class=PlainTextResponse)
def amnezia_vpn_key(token: str, device: int | None = None) -> PlainTextResponse:
    config = capture_vpn_config()
    client = config.client_by_token(token)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        config,client = config_for_device(config,client,device)
    except LookupError:
        raise HTTPException(status_code=404) from None
    except ValueError as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from None
    if not config.current_ip:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return PlainTextResponse(render_amnezia_vpn_key(config, client) + "\n")
