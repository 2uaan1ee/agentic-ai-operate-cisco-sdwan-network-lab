"""
mock_vmanage.py — vManage GIẢ LẬP chạy trên máy local để test code mà không
cần VPN tới sandbox. Dữ liệu thiết bị là dữ liệu mẫu tự đặt, KHÔNG phải dữ
liệu thật của sandbox. Định dạng response bám theo cấu trúc API vManage mà
code sử dụng (khoá 'data', 'host-name', 'peer-type', 'state', ...).

Mô phỏng thêm các hành vi quan trọng của vManage thật:
  - POST /j_security_check trả cookie JSESSIONID khi đúng tài khoản, trả trang
    HTML đăng nhập (HTTP 200) khi sai.
  - GET /dataservice/client/token trả XSRF token.
  - POST/PUT/DELETE bắt buộc có header X-XSRF-TOKEN, thiếu thì 403.

Chạy riêng:  python tests/mock_vmanage.py   (lắng nghe 127.0.0.1:8443 http)
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

USER, PASSWORD, TOKEN, SESSION = "admin", "mockpass", "mock-xsrf-token", "MOCKSESSION123"

DEVICES = [
    {"deviceId": "1.1.1.90", "system-ip": "1.1.1.90", "host-name": "vmanage", "device-type": "vmanage",
     "site-id": "100", "reachability": "reachable", "status": "normal", "version": "20.x-mock"},
    {"deviceId": "1.1.1.70", "system-ip": "1.1.1.70", "host-name": "vsmart", "device-type": "vsmart",
     "site-id": "100", "reachability": "reachable", "status": "normal", "version": "20.x-mock"},
    {"deviceId": "1.1.1.80", "system-ip": "1.1.1.80", "host-name": "vbond", "device-type": "vbond",
     "site-id": "100", "reachability": "reachable", "status": "normal", "version": "20.x-mock"},
    {"deviceId": "10.0.0.1", "system-ip": "10.0.0.1", "host-name": "dc-cedge01", "device-type": "vedge-C8000V",
     "site-id": "100", "reachability": "reachable", "status": "normal", "version": "17.x-mock"},
    {"deviceId": "10.0.0.11", "system-ip": "10.0.0.11", "host-name": "site1-cedge01", "device-type": "vedge-C8000V",
     "site-id": "101", "reachability": "reachable", "status": "normal", "version": "17.x-mock"},
    {"deviceId": "10.0.0.12", "system-ip": "10.0.0.12", "host-name": "site2-cedge01", "device-type": "vedge-C8000V",
     "site-id": "102", "reachability": "reachable", "status": "normal", "version": "17.x-mock"},
    {"deviceId": "10.0.0.13", "system-ip": "10.0.0.13", "host-name": "site3-vedge01", "device-type": "vedge-cloud",
     "site-id": "103", "reachability": "reachable", "status": "normal", "version": "20.x-mock"},
]


def _ctrl(peer_type, ip, state="up", color="default"):
    return {"peer-type": peer_type, "system-ip": ip, "state": state, "local-color": color}


def _bfd(ip, site, color, state="up"):
    return {"system-ip": ip, "site-id": site, "state": state, "color": color, "local-color": color}


# site2 cố ý có 1 BFD session down (màu mpls) để test trạng thái 'warning'.
CONTROL = {
    "10.0.0.1": [_ctrl("vsmart", "1.1.1.70"), _ctrl("vmanage", "1.1.1.90"), _ctrl("vbond", "1.1.1.80", "connect")],
    "10.0.0.11": [_ctrl("vsmart", "1.1.1.70"), _ctrl("vmanage", "1.1.1.90")],
    "10.0.0.12": [_ctrl("vsmart", "1.1.1.70"), _ctrl("vmanage", "1.1.1.90")],
    "10.0.0.13": [_ctrl("vsmart", "1.1.1.70"), _ctrl("vmanage", "1.1.1.90")],
}
BFD = {
    "10.0.0.1": [_bfd("10.0.0.11", "101", "mpls"), _bfd("10.0.0.11", "101", "biz-internet"),
                 _bfd("10.0.0.12", "102", "mpls", "down"), _bfd("10.0.0.12", "102", "biz-internet"),
                 _bfd("10.0.0.13", "103", "mpls"), _bfd("10.0.0.13", "103", "biz-internet")],
    "10.0.0.11": [_bfd("10.0.0.1", "100", "mpls"), _bfd("10.0.0.1", "100", "biz-internet")],
    "10.0.0.12": [_bfd("10.0.0.1", "100", "mpls", "down"), _bfd("10.0.0.1", "100", "biz-internet")],
    "10.0.0.13": [_bfd("10.0.0.1", "100", "mpls"), _bfd("10.0.0.1", "100", "biz-internet")],
}

LISTS = {
    "id-existing-1": {"listId": "id-existing-1", "name": "CORP_LAN", "type": "dataPrefix",
                      "description": "created by lab", "entries": [{"ipPrefix": "172.16.0.0/16"}],
                      "referenceCount": 2, "readOnly": False},
    "id-existing-2": {"listId": "id-existing-2", "name": "AI_IN_USE", "type": "dataPrefix",
                      "description": "AI list referenced by a policy", "entries": [{"ipPrefix": "192.168.50.0/24"}],
                      "referenceCount": 1, "readOnly": False},
}
_state = {"next_id": 1, "requests": []}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", cookie=None):
        raw = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self):
        return f"JSESSIONID={SESSION}" in (self.headers.get("Cookie") or "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._body()
        if path == "/j_security_check":
            form = parse_qs(raw.decode())
            if form.get("j_username") == [USER] and form.get("j_password") == [PASSWORD]:
                return self._send(200, b"", "text/html", cookie=f"JSESSIONID={SESSION}; Path=/")
            return self._send(200, "<html><body>Login</body></html>", "text/html")
        return self._write("POST", path, raw)

    def do_PUT(self):
        self._write("PUT", urlparse(self.path).path, self._body())

    def do_DELETE(self):
        self._write("DELETE", urlparse(self.path).path, self._body())

    def _write(self, method, path, raw):
        if not self._authed():
            return self._send(403, "forbidden")
        if self.headers.get("X-XSRF-TOKEN") != TOKEN:
            return self._send(403, "missing xsrf token")
        base = "/dataservice/template/policy/list/dataprefix"
        with _lock:
            _state["requests"].append((method, path, raw.decode() if raw else ""))
            if method == "POST" and path == base:
                body = json.loads(raw)
                lid = f"id-new-{_state['next_id']}"
                _state["next_id"] += 1
                LISTS[lid] = {**body, "listId": lid, "referenceCount": 0, "readOnly": False}
                return self._send(200, {"listId": lid})
            if path.startswith(base + "/"):
                lid = path.rsplit("/", 1)[1]
                if lid not in LISTS:
                    return self._send(404, {"error": "not found"})
                if method == "PUT":
                    body = json.loads(raw)
                    LISTS[lid].update({"entries": body["entries"], "description": body.get("description", "")})
                    return self._send(200, {"masterTemplatesAffected": []})
                if method == "DELETE":
                    del LISTS[lid]
                    return self._send(200, b"", "application/json")
        self._send(404, {"error": "unknown path"})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/dataservice/client/token":
            if not self._authed():
                return self._send(403, "forbidden")
            return self._send(200, TOKEN, "text/plain")
        if not self._authed():
            return self._send(403, "forbidden")
        dev = (q.get("deviceId") or [None])[0]
        if u.path == "/dataservice/device":
            return self._send(200, {"data": DEVICES})
        if u.path == "/dataservice/device/control/connections":
            return self._send(200, {"data": CONTROL.get(dev, [])})
        if u.path == "/dataservice/device/bfd/sessions":
            return self._send(200, {"data": BFD.get(dev, [])})
        if u.path == "/dataservice/template/policy/list/dataprefix":
            with _lock:
                return self._send(200, list(LISTS.values()))
        self._send(404, {"error": "unknown path"})


def start(port: int = 0):
    """Chạy server trong thread nền, trả về (server, port)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def recorded_requests():
    with _lock:
        return list(_state["requests"])


if __name__ == "__main__":
    srv, p = start(8443)
    print(f"Mock vManage http://127.0.0.1:{p}  user={USER} pass={PASSWORD}")
    threading.Event().wait()
