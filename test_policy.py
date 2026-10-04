"""
test_policy.py — Kiểm tra kịch bản 4 (App-Route policy) trên vManage giả lập.
Không cần VPN, không cần API key LLM. Chạy:  python tests/test_policy.py
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
    "SDWAN_POLICY_SITE_LISTS": "All-Sites", "SDWAN_POLICY_VPN_LIST": "Corp-VPN",
    "SDWAN_POLICY_POLL_SEC": "0.01", "SDWAN_POLICY_ACTIVATE_TIMEOUT": "2",
    "SDWAN_POLICY_AFTER_WAIT": "0.05", "SDWAN_POLICY_AFTER_POLL": "0.01",
})

import sdwan_policy_tools as pt  # noqa: E402

_answers = []


def answer(*a):
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


def apply(tc="voice", color="mpls", sla=""):
    return json.loads(pt.apply_policy_text(tc, color, sla))


def remove(tc="voice", color="mpls"):
    return json.loads(pt.remove_policy_text(tc, color))


def writes():
    return [r for r in mock_vmanage.recorded_requests()]


# ---------------------------------------------------------------- đọc ------
st = json.loads(pt.policy_state_text())
check("đọc state: màu overlay", st["overlay_colors"] == ["biz-internet", "mpls"], st["overlay_colors"])
check("đọc state: chưa policy nào active", st["active_policy"] is None)
status = json.loads(pt.policy_status_text("Voice-And-Video"))
check("status: edge chỉ có __all_tunnels__",
      all(v == ["__all_tunnels__"] for v in status["edge_sla_classes"].values()), status["edge_sla_classes"])
pairs = {(c["local_color"], c["remote_color"]): c for c in status["compliance_by_color_pair"]}
check("tunnel MPLS đạt SLA voice, tunnel internet (200ms) không đạt",
      pairs[("mpls", "mpls")]["meeting_sla"] == pairs[("mpls", "mpls")]["tunnels"] > 0
      and pairs[("biz-internet", "biz-internet")]["meeting_sla"] == 0, pairs)

# ----------------------------------------------------- guard: không ghi -----
n0 = len(writes())
r = apply("karaoke", "mpls")
check("traffic_class lạ bị chặn", r["status"] == "blocked" and "karaoke" in r["message"], r)
r = apply("voice", "satellite")
check("màu không có trong overlay bị chặn", r["status"] == "blocked" and "overlay" in r["message"], r)
r = apply("voice", "mpls", "Khong-Ton-Tai")
check("SLA class không tồn tại bị chặn", r["status"] == "blocked", r)
os.environ["SDWAN_POLICY_SITE_LISTS"] = "Khong-Co"
r = apply()
check("thiếu site list -> blocked kèm hướng dẫn .env", r["status"] == "blocked" and "SDWAN_POLICY_SITE_LISTS" in r["message"], r)
os.environ["SDWAN_POLICY_SITE_LISTS"] = "All-Sites"
mock_vmanage.VSMART_POLICIES[0]["isPolicyActivated"] = True
r = apply()
check("đang có policy khác active -> blocked", r["status"] == "blocked" and "Mock-Centralized" in r["message"], r)
mock_vmanage.VSMART_POLICIES[0]["isPolicyActivated"] = False
os.environ["SDWAN_READ_ONLY"] = "true"
r = apply()
check("READ_ONLY chặn trước khi hỏi xác nhận", r["status"] == "blocked" and "READ_ONLY" in r["message"], r)
os.environ["SDWAN_READ_ONLY"] = "false"
check("các case chặn không gửi request ghi nào", len(writes()) == n0)

# ------------------------------------------------------ từ chối từng GĐ -----
r = apply()
check("không có console -> cancelled", r["status"] == "cancelled" and len(writes()) == n0, r)
answer("no")
r = apply()
check("từ chối GĐ1 -> cancelled, chưa ghi", r["status"] == "cancelled" and len(writes()) == n0, r)

answer("yes", "yes")  # GĐ1 đồng ý, GĐ2 gõ sai tên
r = apply()
check("GĐ2 không gõ lại tên -> created_not_activated", r["status"] == "created_not_activated", r)
names = [p["policyName"] for p in mock_vmanage.VSMART_POLICIES]
check("object đã tạo nhưng policy CHƯA active",
      "AI_voice_mpls_pol" in names and not any(p["isPolicyActivated"] for p in mock_vmanage.VSMART_POLICIES))
check("không có request activate nào", not any("/activate/" in w[1] for w in writes()))

# ------------------------------------------------------- activate thật ------
answer("AI_voice_mpls_pol")  # object đã có: chỉ còn hỏi GĐ2
r = apply()
check("activate -> applied", r["status"] == "applied", r)
check("before: edge chưa có SLA class của policy",
      all(v == ["__all_tunnels__"] for v in r["before"]["edge_sla_classes"].values()), r["before"])
check("after: mọi edge giữ thêm Voice-And-Video",
      all("Voice-And-Video" in v for v in r["after"]["edge_sla_classes"].values()), r["after"])
check("plan ghi đúng DSCP/màu/SLA", r["plan"]["dscp"] == "46" and r["plan"]["preferred_color"] == "mpls"
      and r["plan"]["sla_class"]["name"] == "Voice-And-Video", r["plan"])
check("có rollback info", r["rollback"] == {"action": "remove", "traffic_class": "voice", "preferred_color": "mpls"})
check("có ghi chú giới hạn (không đo gói thật)", "limits" in r and "traffic" in r["limits"])

# payload definition do code dựng, đúng khuôn
posted = [w for w in writes() if w[0] == "POST" and w[1] == pt.DEF_PATH]
body = json.loads(posted[0][2])
seq = body["sequences"][0]
check("definition payload đúng khuôn (DSCP 46, slaClass, preferredColor mpls)",
      seq["match"]["entries"] == [{"field": "dscp", "value": "46"}]
      and seq["actions"][0]["parameter"][1] == {"field": "preferredColor", "value": "mpls"}
      and seq["actions"][0]["parameter"][0]["ref"] == "sla-2", body)

n1 = len(writes())
r = apply()
check("apply lại -> already_configured, không gửi gì", r["status"] == "already_configured" and len(writes()) == n1, r)

# ------------------------------------------------------------ gỡ -----------
answer("no")
r = remove()
check("từ chối gỡ -> cancelled", r["status"] == "cancelled")
answer("yes")
r = remove()
check("gỡ policy đang active mà không gõ lại tên -> cancelled", r["status"] == "cancelled")
answer("AI_voice_mpls_pol")
r = remove()
check("gỡ -> applied, object biến mất", r["status"] == "applied" and not r["centralized_policy_still_present"]
      and not r["definition_still_present"], r)
order = [(w[0], w[1].rsplit("/", 2)[-2] if "/" in w[1] else "") for w in writes()[n1:]]
paths = [w[1] for w in writes()[n1:]]
check("thứ tự gỡ: deactivate -> xoá policy -> xoá definition",
      "/deactivate/" in paths[0] and "/vsmart/pol-new" in paths[1] and "/approute/def-new" in paths[2], paths)
check("overlay về lại trạng thái chưa có policy active", not any(p["isPolicyActivated"] for p in mock_vmanage.VSMART_POLICIES))
check("object có sẵn của lab còn nguyên",
      any(p["policyName"] == "Mock-Centralized" for p in mock_vmanage.VSMART_POLICIES)
      and any(d["name"] == "Mock-AppRoute" for d in mock_vmanage.APPROUTE_DEFS))
check("không có DELETE nào chạm object có sẵn (pol-1 / def-1)",
      not any(w[0] == "DELETE" and (w[1].endswith("/pol-1") or w[1].endswith("/def-1")) for w in writes()))
r = remove()
check("gỡ khi không còn gì -> already_configured", r["status"] == "already_configured")

# ------------------- nhánh khó: policyDefinition phải là chuỗi + edge chậm ---
mock_vmanage.MODE["policy_definition_must_be_string"] = True
r = apply("video", "internet")  # alias 'internet' -> public-internet không có trong mock => blocked
check("alias internet chỉ hợp lệ khi overlay có màu public-internet", r["status"] == "blocked", r)
answer("yes", "AI_video_biz-internet_pol")
r = apply("video", "biz-internet")
check("vManage đòi policyDefinition dạng chuỗi -> code tự thử lại dạng chuỗi", r["status"] == "applied", r)
posts = [w for w in writes() if w[0] == "POST" and w[1] == pt.POLICY_PATH]
check("đã gửi 2 lần (object bị từ chối, rồi chuỗi)",
      not isinstance(json.loads(posts[-2][2])["policyDefinition"], str)
      and isinstance(json.loads(posts[-1][2])["policyDefinition"], str))
mock_vmanage.MODE["policy_definition_must_be_string"] = False
answer("AI_video_biz-internet_pol")
check("dọn", remove("video", "biz-internet")["status"] == "applied")

mock_vmanage.MODE["edges_receive_policy"] = False
answer("yes", "AI_critical_mpls_pol")
r = apply("critical", "mpls")
check("edge không giữ SLA class mới -> applied_but_not_verified (không báo khống)",
      r["status"] == "applied_but_not_verified", r["status"])
mock_vmanage.MODE["edges_receive_policy"] = True
answer("AI_critical_mpls_pol")
check("dọn lần cuối", remove("critical", "mpls")["status"] == "applied")

print(f"\n{passed} kiểm tra đều đạt.")
