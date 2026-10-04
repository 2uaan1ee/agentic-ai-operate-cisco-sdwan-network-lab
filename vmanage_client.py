"""
vmanage_client.py — Lớp kết nối REST tới vManage của Cisco SD-WAN sandbox.

Đóng vai trò giống `_run_raw` trong network_tools.py của repo mẫu (điểm duy
nhất chạm tới thiết bị thật), nhưng thay SSH/Netmiko bằng REST API:

  1. POST /j_security_check  (form j_username / j_password) -> cookie JSESSIONID
  2. GET  /dataservice/client/token                         -> XSRF token
  3. Mọi request sau đó gửi cookie; POST/PUT/DELETE gửi thêm header
     X-XSRF-TOKEN.

Điểm an toàn đặt ở đây (tầng code, không phụ thuộc LLM):
  - Chỉ cho phép đường dẫn bắt đầu bằng /dataservice/.
  - Hàm GET (`vm_get`) và hàm ghi (`vm_write`) tách riêng. Tool đọc chỉ import
    `vm_get`, nên không có đường nào từ tool đọc đi tới POST/PUT/DELETE.
  - Biến SDWAN_READ_ONLY=true chặn mọi lệnh ghi ngay tại `vm_write`.
  - Mọi request đều được ghi vào tool_calls.log (không ghi mật khẩu/token).
"""

import os
import json
import time
import threading

import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALL_LOG_PATH = os.path.join(BASE_DIR, "tool_calls.log")

# Sandbox dùng chứng chỉ tự ký nên mặc định không verify. Đổi
# SDWAN_VERIFY_SSL=true nếu bạn dùng vManage có chứng chỉ hợp lệ.
_VERIFY_SSL = os.environ.get("SDWAN_VERIFY_SSL", "false").strip().lower() == "true"
if not _VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TIMEOUT = 30
_ALLOWED_PREFIX = "/dataservice/"

_lock = threading.Lock()
_session = None  # requests.Session đã đăng nhập, dùng lại giữa các lần gọi
_xsrf_token = None


class VManageError(RuntimeError):
    """Lỗi khi gọi vManage (đăng nhập sai, HTTP lỗi, bị chặn bởi guard...)."""


def _base_url() -> str:
    """URL gốc của vManage, vd https://10.10.20.90:443 — đọc từ .env."""
    host = os.environ.get("VMANAGE_HOST", "").strip()
    port = os.environ.get("VMANAGE_PORT", "443").strip()
    if not host:
        raise VManageError(
            "Thiếu VMANAGE_HOST trong .env (sandbox: 10.10.20.90). Copy "
            ".env.example thành .env rồi điền."
        )
    # VMANAGE_SCHEME chỉ dùng khi test với vManage giả lập chạy http trên máy
    # local (tests/mock_vmanage.py). Sandbox thật luôn là https.
    scheme = os.environ.get("VMANAGE_SCHEME", "").strip().lower() or "https"
    return f"{scheme}://{host}:{port}"


def _log_call(method: str, path: str, status: str, elapsed: float, detail: str = "") -> None:
    """Ghi 1 dòng JSON vào tool_calls.log. Lỗi ghi log không được làm hỏng
    luồng chính."""
    try:
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": path,
            "status": status,
            "elapsed_sec": round(elapsed, 2),
            "detail": detail[:200],
        }
        with open(CALL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _login() -> None:
    """Đăng nhập, lấy JSESSIONID và XSRF token. Gọi lại khi session hết hạn."""
    global _session, _xsrf_token
    user = os.environ.get("VMANAGE_USERNAME", "").strip()
    password = os.environ.get("VMANAGE_PASSWORD", "")
    if not user or not password:
        raise VManageError("Thiếu VMANAGE_USERNAME/VMANAGE_PASSWORD trong .env.")

    base = _base_url()
    s = requests.Session()
    s.verify = _VERIFY_SSL
    t0 = time.monotonic()
    try:
        r = s.post(
            f"{base}/j_security_check",
            data={"j_username": user, "j_password": password},
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        _log_call("POST", "/j_security_check", "error", time.monotonic() - t0, str(e))
        raise VManageError(
            f"Không kết nối được vManage {base}: {e}. Kiểm tra VPN tới sandbox "
            "đã bật chưa và VMANAGE_HOST có đúng không."
        ) from e

    # Đăng nhập sai: vManage trả 200 kèm trang HTML đăng nhập, không có
    # JSESSIONID mới. Đăng nhập đúng: có cookie JSESSIONID, body rỗng.
    if r.status_code >= 400 or "JSESSIONID" not in s.cookies.get_dict() or "<html" in r.text.lower():
        _log_call("POST", "/j_security_check", "error", time.monotonic() - t0, f"HTTP {r.status_code}")
        raise VManageError(
            "Đăng nhập vManage thất bại — kiểm tra VMANAGE_USERNAME/VMANAGE_PASSWORD."
        )

    # Token XSRF (vManage 19.2 trở lên). Bản cũ hơn không có endpoint này.
    token = None
    try:
        tr = s.get(f"{base}/dataservice/client/token", timeout=_TIMEOUT)
        if tr.status_code == 200 and tr.text and "<html" not in tr.text.lower():
            token = tr.text.strip()
    except requests.RequestException:
        token = None

    _session, _xsrf_token = s, token
    _log_call("POST", "/j_security_check", "ok", time.monotonic() - t0, "login ok")


def _ensure_session() -> None:
    if _session is None:
        _login()


def _check_path(path: str) -> str:
    if not path.startswith(_ALLOWED_PREFIX):
        raise VManageError(
            f"Đường dẫn '{path}' bị từ chối — chỉ cho phép {_ALLOWED_PREFIX}*"
        )
    return path


def _request(method: str, path: str, params: dict = None, body: dict = None):
    """Gửi request, tự đăng nhập lại 1 lần nếu session hết hạn (401/403 hoặc
    nhận về trang HTML đăng nhập)."""
    path = _check_path(path)
    with _lock:
        for attempt in (1, 2):
            _ensure_session()
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            # Gửi token cho MỌI request: vài endpoint GET (policy/vsmart,
            # definition/approute) cũng bị SessionTokenFilter chặn 403 nếu thiếu.
            if _xsrf_token:
                headers["X-XSRF-TOKEN"] = _xsrf_token
            t0 = time.monotonic()
            try:
                r = _session.request(
                    method, _base_url() + path, params=params,
                    json=body if body is not None else None,
                    headers=headers, timeout=_TIMEOUT,
                )
            except requests.RequestException as e:
                _log_call(method, path, "error", time.monotonic() - t0, str(e))
                raise VManageError(f"Lỗi mạng khi gọi {method} {path}: {e}") from e

            expired = r.status_code in (401, 403) or "<html" in r.text[:200].lower()
            if expired and attempt == 1:
                _reset_session()
                continue
            if r.status_code >= 400:
                _log_call(method, path, "error", time.monotonic() - t0, f"HTTP {r.status_code} {r.text[:120]}")
                raise VManageError(f"{method} {path} trả HTTP {r.status_code}: {r.text[:300]}")

            _log_call(method, path, "ok", time.monotonic() - t0, f"HTTP {r.status_code}")
            if not r.text.strip():
                return {}
            try:
                return r.json()
            except ValueError:
                raise VManageError(f"{method} {path} trả nội dung không phải JSON: {r.text[:200]}")
    raise VManageError(f"{method} {path} thất bại sau khi đăng nhập lại.")


def _reset_session() -> None:
    global _session, _xsrf_token
    _session, _xsrf_token = None, None


def vm_get(path: str, params: dict = None):
    """GET — chỉ đọc. Trả về JSON đã parse. Phần lớn endpoint vManage bọc dữ
    liệu trong khoá 'data'; dùng `vm_get_data` để lấy luôn phần đó."""
    return _request("GET", path, params=params)


def vm_get_data(path: str, params: dict = None) -> list:
    """GET và trả về danh sách trong khoá 'data' (nếu không có thì trả list
    rỗng; nếu response là list thuần thì trả nguyên)."""
    res = vm_get(path, params)
    if isinstance(res, list):
        return res
    data = res.get("data", [])
    return data if isinstance(data, list) else [data]


READ_ONLY_MESSAGE = (
    "SDWAN_READ_ONLY=true trong .env, mọi lệnh ghi bị chặn. Đặt false nếu "
    "muốn cho phép ghi."
)


def is_read_only() -> bool:
    return os.environ.get("SDWAN_READ_ONLY", "false").strip().lower() == "true"


def vm_write(method: str, path: str, body: dict = None):
    """POST/PUT/DELETE — chỉ module sdwan_write_tools.py được gọi hàm này.
    Bị chặn hoàn toàn khi SDWAN_READ_ONLY=true (kiểm tra lại ở đây dù tool ghi
    đã kiểm tra sớm, để không có đường nào lách qua)."""
    method = method.upper()
    if method not in ("POST", "PUT", "DELETE"):
        raise VManageError(f"Phương thức ghi không hợp lệ: {method}")
    if is_read_only():
        raise VManageError(READ_ONLY_MESSAGE)
    return _request(method, path, body=body)