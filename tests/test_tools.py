"""
test_tools.py — Kiểm tra tool đọc/ghi trên vManage giả lập (không cần VPN, không
cần API key LLM). Chạy:  python tests/test_tools.py
"""

import os
import sys
import json
import builtins

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_vmanage  # noqa: E402

server, port = mock_vmanage.start(0)
os.environ.update({
    "VMANAGE_HOST": "127.0.0.1", "VMANAGE_PORT": str(port), "VMANAGE_SCHEME": "http",
    "VMANAGE_USERNAME": mock_vmanage.USER, "VMANAGE_PASSWORD": mock_vmanage.PASSWORD,
    "SDWAN_READ_ONLY": "false",
})

import vmanage_client  # noqa: E402
import sdwan_read_tools as rd  # noqa: E402
import sdwan_write_tools as wr  # noqa: E402

_answers = []


def answer(*a):
    """Đặt sẵn câu trả lời cho các lần input() kế tiếp."""
    _answers.extend(a)


def _fake_input(prompt=""):
    if not _answers:
        raise EOFError
    return _answers.pop(0)


builtins.input = _fake_input

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    passed += 1
    print(f"ok   {name}")


def run(action, name, prefixes=None):
    return json.loads(wr.change_prefix_list_text(action, name, prefixes))


# ---------------------------------------------------------------- ĐỌC ------
d = json.loads(rd.list_sdwan_devices_text())
check("list devices", d["total"] == 7 and any(x["hostname"] == "dc-cedge01" for x in d["devices"]))

r = json.loads(rd.check_overlay_health_text("site1"))  # khớp một phần
check("health site1 (partial name) ok", r["overall_status"] == "ok" and r["bfd"]["up"] == 2, r)

r = json.loads(rd.check_overlay_health_text("site2-cedge01"))
check("health site2 warning (1 BFD down)", r["overall_status"] == "warning" and r["bfd"]["down"] == 1, r)

r = json.loads(rd.check_overlay_health_text("dc-cedge01"))
check("vBond control 'connect' bị bỏ qua, dc vẫn ok control", r["control_connections"]["status"] == "ok", r)
check("dc bfd warning (site2 mpls down)", r["bfd"]["status"] == "warning")

r = json.loads(rd.check_overlay_health_text("vsmart"))
check("controller chỉ đánh giá reachability", r["overall_status"] == "ok" and "bfd" not in r, r)

r = json.loads(rd.check_overlay_health_text("khong-ton-tai"))
check("thiết bị không tồn tại -> error rõ", "error" in r and "Thiết bị có sẵn" in r["error"])

r = json.loads(rd.check_all_edges_health_text())
check("all edges: 4 edge, 2 warning (dc + site2), 2 ok",
      r["edge_count"] == 4 and r["status_counts"]["warning"] == 2 and r["status_counts"]["ok"] == 2, r["status_counts"])

# ------------------------------------------------------------ GHI: guard ---
n0 = len(mock_vmanage.recorded_requests())

r = run("create", "BRANCH_LAN", ["10.10.21.0/24"])
check("tên không có tiền tố AI_ bị chặn", r["status"] == "error" and "AI_" in r["error"])
r = run("create", "AI_X", ["10.10.20.0/24"])
check("prefix chồng dải quản trị bị chặn", r["status"] == "error" and "bị chặn" in r["error"])
r = run("create", "AI_X", ["0.0.0.0/0"])
check("default route bị chặn", r["status"] == "error")
r = run("create", "AI_X", ["10.0.0.0/7"])
check("prefix quá rộng bị chặn", r["status"] == "error" and "quá rộng" in r["error"])
r = run("create", "AI_X", ["abc"])
check("prefix sai định dạng bị chặn", r["status"] == "error")
r = run("create", "AI_X", ["2001:db8::/32"])
check("IPv6 bị chặn", r["status"] == "error")
r = run("hack", "AI_X", ["10.1.1.0/24"])
check("action lạ bị chặn", r["status"] == "error")
r = run("add", "CORP_LAN", ["10.1.1.0/24"])
check("không sửa list không có tiền tố AI_", r["status"] == "error")
r = run("add", "AI_IN_USE", ["10.1.1.0/24"])
check("list đang được policy tham chiếu bị chặn", r["status"] == "blocked" and "tham chiếu" in r["message"], r)
r = run("add", "AI_MISSING", ["10.1.1.0/24"])
check("add vào list chưa có -> blocked", r["status"] == "blocked")
check("các case trên không gửi request ghi nào", len(mock_vmanage.recorded_requests()) == n0)

# ------------------------------------------------------- GHI: luồng đủ -----
answer("no")
r = run("create", "AI_BRANCH_LAN", ["10.10.21.5/24", "10.10.22.0/24"])
check("từ chối xác nhận -> cancelled, không ghi", r["status"] == "cancelled" and len(mock_vmanage.recorded_requests()) == n0)

r = run("create", "AI_BRANCH_LAN", ["10.10.21.0/24"])
check("không có console (EOF) -> cancelled", r["status"] == "cancelled")

answer("yes")
r = run("create", "AI_BRANCH_LAN", ["10.10.21.5/24", "10.10.22.0/24"])
check("create applied + verify", r["status"] == "applied", r)
check("prefix được chuẩn hoá 10.10.21.5/24 -> 10.10.21.0/24", r["entries_after"] == ["10.10.21.0/24", "10.10.22.0/24"], r)
last = mock_vmanage.recorded_requests()[-1]
check("POST đúng URL + payload do code dựng",
      last[0] == "POST" and last[1].endswith("/dataservice/template/policy/list/dataprefix")
      and json.loads(last[2])["type"] == "dataPrefix" and json.loads(last[2])["entries"][0] == {"ipPrefix": "10.10.21.0/24"})

n1 = len(mock_vmanage.recorded_requests())
r = run("create", "AI_BRANCH_LAN", ["10.10.21.0/24", "10.10.22.0/24"])
check("create lại đúng nội dung -> already_configured", r["status"] == "already_configured" and len(mock_vmanage.recorded_requests()) == n1)
r = run("create", "AI_BRANCH_LAN", ["10.99.0.0/24"])
check("create trùng tên khác nội dung -> blocked", r["status"] == "blocked")

answer("yes")
r = run("add", "AI_BRANCH_LAN", ["10.10.24.0/24"])
check("add applied", r["status"] == "applied" and "10.10.24.0/24" in r["entries_after"], r)
check("PUT đúng listId", mock_vmanage.recorded_requests()[-1][0] == "PUT")
rb_add = r["rollback"]
check("rollback của add là remove đúng prefix", rb_add == {"action": "remove", "list_name": "AI_BRANCH_LAN", "prefixes": ["10.10.24.0/24"]}, rb_add)

r = run("add", "AI_BRANCH_LAN", ["10.10.24.0/24"])
check("add prefix đã có -> already_configured", r["status"] == "already_configured")

answer("yes")
r = run("remove", "AI_BRANCH_LAN", ["10.10.22.0/24"])
check("remove applied", r["status"] == "applied" and "10.10.22.0/24" not in r["entries_after"], r)

r = run("remove", "AI_BRANCH_LAN", ["10.77.0.0/24"])
check("remove prefix không có -> already_configured", r["status"] == "already_configured")
r = run("remove", "AI_BRANCH_LAN", ["10.10.21.0/24", "10.10.24.0/24"])
check("remove hết prefix -> blocked (dùng delete)", r["status"] == "blocked" and "delete" in r["message"])

answer("yes")
r = run("remove", rb_add["list_name"], rb_add["prefixes"])
check("hoàn tác add qua rollback info", r["status"] == "applied" and r["entries_after"] == ["10.10.21.0/24"], r)

# delete cần gõ lại tên list
answer("yes")
r = run("delete", "AI_BRANCH_LAN")
check("delete với 'yes' (không gõ lại tên) -> cancelled", r["status"] == "cancelled")
answer("AI_BRANCH_LAN")
r = run("delete", "AI_BRANCH_LAN")
check("delete gõ đúng tên -> applied, list biến mất", r["status"] == "applied" and r["entries_after"] is None, r)
check("DELETE request đã gửi", mock_vmanage.recorded_requests()[-1][0] == "DELETE")
r = run("delete", "AI_BRANCH_LAN")
check("delete list đã không còn -> already_configured", r["status"] == "already_configured")

# --------------------------------------------------- công tắc READ_ONLY ----
os.environ["SDWAN_READ_ONLY"] = "true"
n_ro = len(mock_vmanage.recorded_requests())
r = run("create", "AI_RO", ["10.5.5.0/24"])  # không đặt sẵn câu trả lời: nếu bị hỏi xác nhận sẽ thành 'cancelled'
check("SDWAN_READ_ONLY=true chặn ghi TRƯỚC khi hỏi xác nhận",
      r["status"] == "blocked" and "READ_ONLY" in r["message"], r)
check("READ_ONLY không gửi request ghi nào", len(mock_vmanage.recorded_requests()) == n_ro)
try:
    vmanage_client.vm_write("POST", "/dataservice/template/policy/list/dataprefix", {})
    check("vm_write tự chặn khi READ_ONLY", False)
except vmanage_client.VManageError:
    check("vm_write tự chặn khi READ_ONLY", True)
os.environ["SDWAN_READ_ONLY"] = "false"

# ------------------------------------------------- client: path & XSRF -----
try:
    vmanage_client.vm_get("/etc/passwd")
    check("client chặn đường dẫn ngoài /dataservice/", False)
except vmanage_client.VManageError:
    check("client chặn đường dẫn ngoài /dataservice/", True)

# Session hết hạn: client phải tự đăng nhập lại
vmanage_client._reset_session()
check("tự đăng nhập lại khi chưa có session", json.loads(rd.list_sdwan_devices_text())["total"] == 7)

# Sai mật khẩu
vmanage_client._reset_session()
os.environ["VMANAGE_PASSWORD"] = "sai"
r = json.loads(rd.list_sdwan_devices_text())
check("sai mật khẩu -> lỗi đăng nhập rõ ràng", "error" in r and "Đăng nhập" in r["error"], r)
os.environ["VMANAGE_PASSWORD"] = mock_vmanage.PASSWORD
vmanage_client._reset_session()

print(f"\n{passed} kiểm tra đều đạt.")
