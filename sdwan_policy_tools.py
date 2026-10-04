"""
sdwan_policy_tools.py — Kịch bản 4: từ yêu cầu tự nhiên ("ưu tiên MPLS cho
voice") tới một App-Route policy được kích hoạt trên vSmart, có đối chiếu
trước/sau.

Đây là thao tác ghi RỦI RO CAO hơn kịch bản 2: kích hoạt centralized policy làm
vSmart đẩy policy xuống các edge và đổi quyết định chọn đường. Vì vậy:

  - Tạo hai giai đoạn, mỗi giai đoạn một lần xác nhận:
      GĐ1  tạo App-Route definition + Centralized policy (CHƯA activate).
           Chưa vSmart nào nhận gì, data plane không đổi. Dừng lại được để
           mở vManage (Configuration > Policies) xem tận mắt.
      GĐ2  activate. Phải gõ lại đúng tên policy để xác nhận.
  - Agent KHÔNG cầm tool ghi và KHÔNG đưa được JSON/URL. LLM chỉ chọn 3 tham số
    (traffic_class, preferred_color, sla_class). Payload do code dựng theo khuôn
    của chính định nghĩa App-Route có sẵn trong sandbox (Outlook-AAR-Policy).
  - Chỉ đụng object tên AI_*. Không bao giờ sửa/xoá/activate Corp-vSmart-Policy
    hay Outlook-AAR-Policy có sẵn. Chỉ được activate khi KHÔNG có centralized
    policy nào khác đang chạy (vManage chỉ cho 1 policy active; không ghi đè).
  - Hoàn tác = deactivate rồi xoá (policy trước, definition sau), đưa overlay
    về đúng trạng thái "chưa có policy" như lúc bắt đầu.

Đối chiếu trước/sau (đã biết giới hạn của sandbox: DPI tắt, gần như không có
traffic dữ liệu, các máy trex không SSH được):
  - Tên SLA class mà MỖI edge đang giữ (GET device/app-route/sla-class). Trước
    khi có policy chỉ có '__all_tunnels__'; sau khi policy tới edge phải thấy
    thêm SLA class của policy. Đây là bằng chứng edge đã NHẬN policy.
  - Mỗi cặp màu tunnel có đạt ngưỡng SLA của policy hay không, tính bằng CODE từ
    số đo trung bình của app-route statistics. Đây là ước tính của code, không
    phải kết luận do router đưa ra.
  KHÔNG đo được chuyển dịch gói tin thật giữa MPLS và Internet vì không có
  traffic dữ liệu; báo cáo phải nói rõ điều này.

Các phần CHƯA kiểm chứng trên sandbox thật (xem README): dạng payload POST
policy/vsmart (code thử object trước, nếu bị từ chối thì thử chuỗi JSON), body
của lệnh activate, tên trường của device/action/status.
"""

import os
import re
import json
import time

from vmanage_client import (
    vm_get, vm_get_data, vm_write, is_read_only, READ_ONLY_MESSAGE, VManageError,
)
from sdwan_write_tools import _confirm_with_admin, _audit

DEF_PATH = "/dataservice/template/policy/definition/approute"
POLICY_PATH = "/dataservice/template/policy/vsmart"
LIST_PATH = "/dataservice/template/policy/list"
CONTROLLER_TYPES = {"vmanage", "vsmart", "vbond"}
NAME_RE = re.compile(r"^AI_[A-Za-z0-9_-]{1,28}$")

# LLM chỉ được chọn trong bảng này. DSCP là chuẩn QoS thông dụng; match theo DSCP
# không cần DPI (DPI đang tắt trên sandbox nên không match theo tên ứng dụng).
TRAFFIC_CLASSES = {
    "voice": {"dscp": "46", "sla": "Voice-And-Video", "label": "Voice (DSCP 46, EF)"},
    "video": {"dscp": "34", "sla": "Voice-And-Video", "label": "Video (DSCP 34, AF41)"},
    "critical": {"dscp": "26", "sla": "Business-Critical", "label": "Business-critical (DSCP 26, AF31)"},
}
COLOR_ALIASES = {"internet": "public-internet", "inet": "public-internet", "public": "public-internet"}


class Blocked(Exception):
    """Điều kiện an toàn không đạt: trả status 'blocked', không ghi gì."""


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _site_list_names() -> list:
    raw = os.environ.get("SDWAN_POLICY_SITE_LISTS", "Hub-list,Spokes-list")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _vpn_list_name() -> str:
    return os.environ.get("SDWAN_POLICY_VPN_LIST", "Corp-VPN-List").strip()


# ---------------------------------------------------------------------------
# Đọc trạng thái
# ---------------------------------------------------------------------------


def _edges() -> list:
    out = []
    for d in vm_get_data("/dataservice/device"):
        if str(d.get("device-type", "")).lower() in CONTROLLER_TYPES:
            continue
        out.append({"hostname": d.get("host-name"), "ip": d.get("deviceId") or d.get("system-ip")})
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, *keys):
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return None


def _sla_thresholds(sla_row: dict) -> dict:
    e = (sla_row.get("entries") or [{}])[0]
    return {"latency": _num(e.get("latency")), "loss": _num(e.get("loss")), "jitter": _num(e.get("jitter"))}


def policy_state() -> dict:
    """Trạng thái policy hiện có + các object cần để dựng policy mới."""
    policies = []
    for p in vm_get_data(POLICY_PATH):
        try:
            pdef = json.loads(p["policyDefinition"]) if isinstance(p.get("policyDefinition"), str) \
                else (p.get("policyDefinition") or {})
        except ValueError:
            pdef = {}
        policies.append({
            "id": p.get("policyId"), "name": p.get("policyName"),
            "activated": bool(p.get("isPolicyActivated")),
            "elements": [str(a.get("type")) for a in (pdef.get("assembly") or []) if isinstance(a, dict)],
            "definition_ids": [a.get("definitionId") for a in (pdef.get("assembly") or []) if isinstance(a, dict)],
        })
    defs = [{"id": d.get("definitionId"), "name": d.get("name"), "references": d.get("referenceCount")}
            for d in vm_get_data(DEF_PATH)]
    sla = [{"id": r.get("listId"), "name": r.get("name"), **_sla_thresholds(r)}
           for r in vm_get_data(f"{LIST_PATH}/sla")]
    sites = [{"id": r.get("listId"), "name": r.get("name"),
              "entries": [str(next(iter(e.values()), "")) for e in (r.get("entries") or []) if isinstance(e, dict)]}
             for r in vm_get_data(f"{LIST_PATH}/site")]
    vpns = [{"id": r.get("listId"), "name": r.get("name"),
             "entries": [str(next(iter(e.values()), "")) for e in (r.get("entries") or []) if isinstance(e, dict)]}
            for r in vm_get_data(f"{LIST_PATH}/vpn")]
    colors = set()
    for e in _edges():
        try:
            for s in vm_get_data("/dataservice/device/bfd/sessions", {"deviceId": e["ip"]}):
                for k in ("color", "local-color"):
                    if s.get(k):
                        colors.add(s[k])
        except VManageError:
            pass
    active = [p["name"] for p in policies if p["activated"]]
    return {
        "centralized_policies": policies, "approute_definitions": defs, "sla_classes": sla,
        "site_lists": sites, "vpn_lists": vpns, "overlay_colors": sorted(colors),
        "active_policy": active[0] if active else None,
    }


def policy_state_text() -> str:
    try:
        return json.dumps(policy_state(), ensure_ascii=False, indent=2)
    except VManageError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def edge_sla_classes() -> dict:
    """Tên các SLA class mà từng edge đang giữ. None nếu không đọc được."""
    out = {}
    for e in _edges():
        try:
            rows = vm_get_data("/dataservice/device/app-route/sla-class", {"deviceId": e["ip"]})
            out[e["hostname"]] = sorted({str(r.get("name")) for r in rows if r.get("name")})
        except VManageError:
            out[e["hostname"]] = None
    return out


def sla_compliance(sla: dict) -> list:
    """Với mỗi cặp màu (local -> remote): bao nhiêu tunnel đạt ngưỡng SLA, tính
    từ trung bình các mẫu app-route statistics. ƯỚC TÍNH của code."""
    th = {"latency": sla.get("latency"), "loss": sla.get("loss"), "jitter": sla.get("jitter")}
    tunnels = {}
    for e in _edges():
        try:
            rows = vm_get_data("/dataservice/device/app-route/statistics", {"deviceId": e["ip"]})
        except VManageError:
            continue
        for r in rows:
            key = (e["hostname"], r.get("remote-system-ip"), r.get("local-color"), r.get("remote-color"))
            t = tunnels.setdefault(key, {"lat": [], "loss": [], "jit": []})
            for slot, keys in (("lat", ("average-latency", "mean-latency")),
                               ("loss", ("loss", "mean-loss")),
                               ("jit", ("average-jitter", "mean-jitter"))):
                v = _pick(r, *keys)
                if v is not None:
                    t[slot].append(v)

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    pairs = {}
    for (host, peer, lc, rc), t in tunnels.items():
        a = {"lat": avg(t["lat"]), "loss": avg(t["loss"]), "jit": avg(t["jit"])}
        meets = all(a[s] is not None and th[k] is not None and a[s] <= th[k]
                    for s, k in (("lat", "latency"), ("loss", "loss"), ("jit", "jitter")))
        p = pairs.setdefault((lc, rc), {"tunnels": 0, "meeting": 0, "lat": [], "loss": [], "jit": []})
        p["tunnels"] += 1
        p["meeting"] += 1 if meets else 0
        for s in ("lat", "loss", "jit"):
            if a[s] is not None:
                p[s].append(a[s])
    out = []
    for (lc, rc), p in sorted(pairs.items(), key=lambda kv: str(kv[0])):
        out.append({
            "local_color": lc, "remote_color": rc, "tunnels": p["tunnels"], "meeting_sla": p["meeting"],
            "avg_latency_ms": round(avg(p["lat"]), 1) if p["lat"] else None,
            "avg_loss_pct": round(avg(p["loss"]), 2) if p["loss"] else None,
            "avg_jitter_ms": round(avg(p["jit"]), 1) if p["jit"] else None,
        })
    return out


def snapshot(sla: dict) -> dict:
    return {"time": time.strftime("%H:%M:%S"), "edge_sla_classes": edge_sla_classes(),
            "compliance_by_color_pair": sla_compliance(sla)}


# ---------------------------------------------------------------------------
# Dựng payload (code dựng, agent không chạm tới)
# ---------------------------------------------------------------------------


def _names(tc: str, color: str):
    base = f"AI_{tc}_{color}"
    return base, f"{base}_pol"  # (tên definition, tên policy)


def _def_payload(name: str, tc: str, color: str, sla_id: str) -> dict:
    """Khuôn lấy từ Outlook-AAR-Policy có sẵn của sandbox: match -> slaClass +
    preferredColor. Chỉ khác cách match (DSCP thay vì appList)."""
    return {
        "name": name, "type": "appRoute",
        "description": f"AI-managed: {TRAFFIC_CLASSES[tc]['label']} prefer {color}",
        "sequences": [{
            "sequenceId": 1, "sequenceName": "App Route", "sequenceType": "appRoute", "sequenceIpType": "ipv4",
            "match": {"entries": [{"field": "dscp", "value": TRAFFIC_CLASSES[tc]["dscp"]}]},
            "actions": [{"type": "slaClass", "parameter": [
                {"field": "name", "ref": sla_id},
                {"field": "preferredColor", "value": color},
            ]}],
        }],
    }


def _policy_payload(name: str, desc: str, def_id: str, site_ids: list, vpn_id: str, as_string: bool) -> dict:
    definition = {"assembly": [{"definitionId": def_id, "type": "appRoute",
                                "entries": [{"siteLists": site_ids, "vpnLists": [vpn_id]}]}]}
    return {"policyName": name, "policyDescription": desc, "policyType": "feature",
            "policyDefinition": json.dumps(definition) if as_string else definition,
            "isPolicyActivated": False}


def _find(rows: list, key: str, name: str):
    for r in rows:
        if r.get(key) == name:
            return r
    return None


def _by_name(items: list, name: str):
    return next((x for x in items if x["name"] == name), None)


# ---------------------------------------------------------------------------
# Chờ tác vụ bất đồng bộ
# ---------------------------------------------------------------------------


def _wait_action(task_id: str) -> tuple:
    """Chờ device/action/status/{id}. Trả (kết_quả, chi_tiết) với kết_quả là
    'success' | 'failed' | 'timeout' | 'unknown'."""
    if not task_id:
        return "unknown", "vManage không trả mã tác vụ"
    timeout = _env_float("SDWAN_POLICY_ACTIVATE_TIMEOUT", 180)
    step = _env_float("SDWAN_POLICY_POLL_SEC", 5)
    deadline = time.monotonic() + timeout
    errors = 0
    last = ""
    while time.monotonic() < deadline:
        try:
            res = vm_get(f"/dataservice/device/action/status/{task_id}")
            errors = 0
        except VManageError as e:
            errors += 1
            last = str(e)[:150]
            if errors >= 3:
                return "unknown", f"không đọc được trạng thái tác vụ: {last}"
            time.sleep(step)
            continue
        summary = (res.get("summary") or {}) if isinstance(res, dict) else {}
        st = str(summary.get("status", "")).lower()
        datas = res.get("data", []) if isinstance(res, dict) else []
        item_status = [str(d.get("status", "")).lower() for d in datas if isinstance(d, dict)]
        last = f"summary.status={st or '?'}, items={item_status}"
        if st in ("failure", "failed") or any("fail" in s for s in item_status):
            return "failed", last
        if st in ("done", "success", "completed"):
            return "success", last
        time.sleep(step)
    return "timeout", last


# ---------------------------------------------------------------------------
# Kế hoạch + kiểm tra điều kiện (không ghi gì)
# ---------------------------------------------------------------------------


def _plan(traffic_class: str, preferred_color: str, sla_class: str) -> dict:
    tc = (traffic_class or "").strip().lower()
    if tc not in TRAFFIC_CLASSES:
        raise Blocked(f"traffic_class '{traffic_class}' không hỗ trợ. Chỉ có: {sorted(TRAFFIC_CLASSES)}.")
    st = policy_state()
    color = (preferred_color or "").strip().lower()
    color = COLOR_ALIASES.get(color, color)
    if color not in st["overlay_colors"]:
        raise Blocked(f"Màu transport '{preferred_color}' không có trong overlay. Overlay đang có: {st['overlay_colors']}.")
    sla_name = (sla_class or TRAFFIC_CLASSES[tc]["sla"]).strip()
    sla = _by_name(st["sla_classes"], sla_name)
    if not sla:
        raise Blocked(f"Không có SLA class '{sla_name}'. Đang có: {[s['name'] for s in st['sla_classes']]}.")
    def_name, pol_name = _names(tc, color)
    for n in (def_name, pol_name):
        if not NAME_RE.match(n):
            raise Blocked(f"Tên object '{n}' không hợp lệ (phải bắt đầu AI_, tối đa 31 ký tự).")

    sites = []
    for n in _site_list_names():
        s = _by_name(st["site_lists"], n)
        if not s:
            raise Blocked(f"Không tìm thấy site list '{n}'. Đang có: {[x['name'] for x in st['site_lists']]}. "
                          "Đặt SDWAN_POLICY_SITE_LISTS trong .env.")
        sites.append(s)
    vpn = _by_name(st["vpn_lists"], _vpn_list_name())
    if not vpn:
        raise Blocked(f"Không tìm thấy VPN list '{_vpn_list_name()}'. Đang có: {[x['name'] for x in st['vpn_lists']]}. "
                      "Đặt SDWAN_POLICY_VPN_LIST trong .env.")

    others = [p["name"] for p in st["centralized_policies"] if p["activated"] and p["name"] != pol_name]
    if others:
        raise Blocked(f"Đang có centralized policy khác active ({others[0]}). Kịch bản không thay thế policy đang chạy. "
                      "Deactivate nó thủ công nếu thực sự muốn.")

    for d in vm_get_data("/dataservice/device"):
        if str(d.get("device-type", "")).lower() == "vsmart" and str(d.get("reachability", "")).lower() != "reachable":
            raise Blocked(f"vSmart {d.get('host-name')} không reachable, không activate policy lúc này.")

    return {"tc": tc, "color": color, "sla": sla, "def_name": def_name, "pol_name": pol_name,
            "sites": sites, "vpn": vpn, "state": st,
            "existing_def": _by_name(st["approute_definitions"], def_name),
            "existing_pol": _by_name(st["centralized_policies"], pol_name)}


def _def_matches(def_id: str, p: dict) -> bool:
    """Definition cùng tên đã có: nội dung có đúng ý (DSCP, SLA, màu) không."""
    try:
        d = vm_get(f"{DEF_PATH}/{def_id}")
        d = d.get("data", d) if isinstance(d, dict) else d
        seq = d["sequences"][0]
        dscp = seq["match"]["entries"][0].get("value")
        params = {x["field"]: x for x in seq["actions"][0]["parameter"]}
        return (str(dscp) == TRAFFIC_CLASSES[p["tc"]]["dscp"] and params["name"].get("ref") == p["sla"]["id"]
                and params["preferredColor"].get("value") == p["color"])
    except (VManageError, KeyError, IndexError, TypeError):
        return False


def _result(status: str, message: str, p=None, **extra) -> str:
    body = {"status": status, "message": message}
    if p:
        body["plan"] = {
            "traffic_class": p["tc"], "dscp": TRAFFIC_CLASSES[p["tc"]]["dscp"], "preferred_color": p["color"],
            "sla_class": {k: p["sla"][k] for k in ("name", "latency", "loss", "jitter")},
            "definition_name": p["def_name"], "policy_name": p["pol_name"],
            "site_lists": [s["name"] for s in p["sites"]], "vpn_list": p["vpn"]["name"],
        }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False, indent=2, default=str)


def _error(action: str, e: Exception) -> str:
    kind = "blocked" if isinstance(e, Blocked) else "error"
    return json.dumps({"status": kind, "action": action,
                       "message" if kind == "blocked" else "error": str(e)}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# APPLY: tạo (GĐ1) rồi activate (GĐ2)
# ---------------------------------------------------------------------------


def apply_policy_text(traffic_class: str, preferred_color: str, sla_class: str = "") -> str:
    """Tạo + activate App-Route policy. Hàm PLAIN trả JSON.
    status: applied | applied_but_not_verified | created_not_activated |
    activation_failed | already_configured | cancelled | blocked | error."""
    try:
        p = _plan(traffic_class, preferred_color, sla_class)
        tc, color, sla = p["tc"], p["color"], p["sla"]
        rollback = {"action": "remove", "traffic_class": tc, "preferred_color": color}

        def_obj, pol_obj = p["existing_def"], p["existing_pol"]
        if def_obj and not _def_matches(def_obj["id"], p):
            return _result("blocked", f"Đã có App-Route definition '{p['def_name']}' nhưng nội dung khác yêu cầu "
                           "(DSCP/SLA/màu). Xoá nó thủ công hoặc dùng kết hợp tham số khác.", p)
        if pol_obj and pol_obj["activated"]:
            if def_obj and def_obj["id"] in pol_obj["definition_ids"]:
                return _result("already_configured", f"Policy {p['pol_name']} đã active với đúng nội dung, không gửi gì.",
                               p, rollback=rollback)
            return _result("blocked", f"Policy {p['pol_name']} đang active nhưng không chứa definition mong đợi.", p)
        if pol_obj and not (def_obj and def_obj["id"] in pol_obj["definition_ids"]):
            return _result("blocked", f"Policy {p['pol_name']} đã tồn tại với nội dung khác, không tự sửa.", p)

        if is_read_only():
            return _result("blocked", READ_ONLY_MESSAGE, p)

        before = snapshot(sla)

        # ---- GĐ1: tạo object, chưa activate --------------------------------
        created = []
        if not (def_obj and pol_obj):
            lines = [
                f"- Giai đoạn 1/2: TẠO policy, CHƯA kích hoạt (data plane không đổi)",
                f"- Traffic    : {TRAFFIC_CLASSES[tc]['label']}",
                f"- Hành động  : theo SLA '{sla['name']}' (latency<={sla['latency']:g}ms, loss<={sla['loss']:g}%, "
                f"jitter<={sla['jitter']:g}ms), ưu tiên màu '{color}'",
                f"- Áp dụng cho: site list {[s['name'] for s in p['sites']]}, VPN list '{p['vpn']['name']}'",
            ]
            if not def_obj:
                lines.append(f"- POST {DEF_PATH}  (definition {p['def_name']})")
            if not pol_obj:
                lines.append(f"- POST {POLICY_PATH}  (policy {p['pol_name']}, isPolicyActivated=false)")
            if not _confirm_with_admin("\n".join(lines)):
                return _result("cancelled", "Quản trị viên từ chối, chưa có request ghi nào được gửi.", p, before=before)

            if not def_obj:
                resp = vm_write("POST", DEF_PATH, _def_payload(p["def_name"], tc, color, sla["id"]))
                def_id = (resp or {}).get("definitionId")
                if not def_id:
                    d = _find(vm_get_data(DEF_PATH), "name", p["def_name"])
                    def_id = d.get("definitionId") if d else None
                if not def_id:
                    raise VManageError("Tạo definition xong nhưng không lấy được definitionId.")
                created.append(f"definition {p['def_name']}")
            else:
                def_id = def_obj["id"]

            if not pol_obj:
                desc = f"AI-managed: {tc} prefer {color}"
                site_ids = [s["id"] for s in p["sites"]]
                resp, last_err = None, None
                for as_string in (False, True):  # dạng object trước, bị từ chối thì thử chuỗi JSON
                    try:
                        resp = vm_write("POST", POLICY_PATH,
                                        _policy_payload(p["pol_name"], desc, def_id, site_ids, p["vpn"]["id"], as_string))
                        break
                    except VManageError as e:
                        last_err = e
                        if _find(vm_get_data(POLICY_PATH), "policyName", p["pol_name"]):
                            break  # lần gửi trước thực ra đã tạo được, không gửi lại
                if resp is None and not _find(vm_get_data(POLICY_PATH), "policyName", p["pol_name"]):
                    raise VManageError(f"Không tạo được policy: {last_err}. definition {p['def_name']} đã tạo "
                                       "và còn lại, hãy chạy lại hoặc gỡ bằng lệnh 'gỡ'.")
                created.append(f"policy {p['pol_name']}")
            _audit({"scenario": "approute", "step": "created", "objects": created})
            print(f"\n[sdwan_policy] Đã tạo ({', '.join(created)}), CHƯA activate. Mở vManage > Configuration > "
                  "Policies > Centralized Policy để xem.")

        # Lấy lại policyId thật từ vManage
        pol = _find(vm_get_data(POLICY_PATH), "policyName", p["pol_name"])
        if not pol:
            raise VManageError("Không tìm thấy policy vừa tạo khi đọc lại.")
        pid = pol["policyId"]

        # ---- GĐ2: activate (gõ lại tên policy) -----------------------------
        lines = [
            f"- Giai đoạn 2/2: KÍCH HOẠT policy {p['pol_name']} trên vSmart",
            f"- vSmart sẽ đẩy app-route policy xuống mọi edge thuộc {[s['name'] for s in p['sites']]} (VPN {p['vpn']['name']}).",
            f"- Traffic {TRAFFIC_CLASSES[tc]['label']} chỉ đi tunnel đạt SLA '{sla['name']}', ưu tiên '{color}'.",
            f"- POST {POLICY_PATH}/activate/{pid}?confirm=true",
            "- Hiện KHÔNG có centralized policy nào khác đang active; policy này sẽ là policy duy nhất.",
            "- Hoàn tác: lệnh 'gỡ' (deactivate rồi xoá), overlay về đúng trạng thái hiện tại.",
        ]
        if not _confirm_with_admin("\n".join(lines),
                                   ["Đẩy policy xuống edge của sandbox. Hoàn tác được nhưng edge sẽ nhận 2 lần cập nhật."],
                                   require_token=p["pol_name"]):
            return _result("created_not_activated",
                           "Đã tạo object nhưng quản trị viên không xác nhận activate; overlay chưa đổi gì. "
                           "Dùng 'gỡ' để xoá object vừa tạo.", p, before=before, created=created, rollback=rollback)

        resp = vm_write("POST", f"{POLICY_PATH}/activate/{pid}?confirm=true", {})
        task_id = (resp or {}).get("id")
        outcome, detail = _wait_action(task_id)
        _audit({"scenario": "approute", "step": "activate", "policy": p["pol_name"], "task": task_id,
                "outcome": outcome, "detail": detail})
        now = _find(vm_get_data(POLICY_PATH), "policyName", p["pol_name"])
        activated = bool(now and now.get("isPolicyActivated"))
        if outcome == "failed" or (outcome in ("timeout",) and not activated):
            return _result("activation_failed",
                           f"vManage báo activate không thành công ({outcome}: {detail}). Policy vẫn ở trạng thái "
                           "chưa active; dùng 'gỡ' để dọn object.", p, before=before, rollback=rollback,
                           activation={"task_id": task_id, "outcome": outcome, "detail": detail})

        # ---- Đối chiếu sau: chờ edge nhận policy ---------------------------
        wait = _env_float("SDWAN_POLICY_AFTER_WAIT", 120)
        step = _env_float("SDWAN_POLICY_AFTER_POLL", 10)
        deadline = time.monotonic() + wait
        after = snapshot(sla)
        while time.monotonic() < deadline and not _edges_have(after, sla["name"]):
            time.sleep(step)
            after = snapshot(sla)
        got = _edges_have(after, sla["name"])

        status = "applied" if (activated and got) else "applied_but_not_verified"
        msg = ("Policy đã active và mọi edge đã giữ SLA class của policy." if status == "applied" else
               "Policy được báo active nhưng chưa thấy đủ edge giữ SLA class của policy trong thời gian chờ "
               f"(activated={activated}). Có thể edge cập nhật chậm hoặc endpoint không liệt kê tên class, cần kiểm tra "
               "thủ công (show sdwan policy from-vsmart trên edge).")
        _audit({"scenario": "approute", "step": "verified", "status": status, "policy": p["pol_name"]})
        return _result(status, msg, p, before=before, after=after, activation={
            "task_id": task_id, "outcome": outcome, "detail": detail, "policy_activated_in_vmanage": activated},
            rollback=rollback,
            limits=("Không đo được chuyển dịch gói tin thật: DPI tắt và sandbox gần như không có traffic dữ liệu. "
                    "Bằng chứng là (1) edge đã giữ SLA class mới, (2) ước tính của code về tunnel nào đạt SLA."))
    except (Blocked, VManageError, ValueError) as e:
        return _error("apply", e)
    except Exception as e:
        return json.dumps({"status": "error", "action": "apply", "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False, indent=2)


def _edges_have(snap: dict, sla_name: str) -> bool:
    cls = snap["edge_sla_classes"]
    return bool(cls) and all(v is not None and sla_name in v for v in cls.values())


# ---------------------------------------------------------------------------
# REMOVE: deactivate -> xoá policy -> xoá definition
# ---------------------------------------------------------------------------


def remove_policy_text(traffic_class: str, preferred_color: str) -> str:
    """Gỡ policy do kịch bản này tạo (chỉ tên AI_*). Hàm PLAIN trả JSON."""
    try:
        tc = (traffic_class or "").strip().lower()
        if tc not in TRAFFIC_CLASSES:
            raise Blocked(f"traffic_class '{traffic_class}' không hỗ trợ. Chỉ có: {sorted(TRAFFIC_CLASSES)}.")
        color = (preferred_color or "").strip().lower()
        color = COLOR_ALIASES.get(color, color)
        def_name, pol_name = _names(tc, color)
        for n in (def_name, pol_name):
            if not NAME_RE.match(n):
                raise Blocked(f"Tên object '{n}' không hợp lệ.")

        pol = _find(vm_get_data(POLICY_PATH), "policyName", pol_name)
        d = _find(vm_get_data(DEF_PATH), "name", def_name)
        if not pol and not d:
            return json.dumps({"status": "already_configured",
                               "message": f"Không có {pol_name} / {def_name}, không có gì để gỡ."}, ensure_ascii=False, indent=2)
        if is_read_only():
            return json.dumps({"status": "blocked", "message": READ_ONLY_MESSAGE}, ensure_ascii=False, indent=2)

        activated = bool(pol and pol.get("isPolicyActivated"))
        lines = [f"- GỠ policy do AI tạo: {pol_name} / {def_name}"]
        if activated:
            lines.append(f"- 1) POST {POLICY_PATH}/deactivate/{pol['policyId']}  (vSmart rút policy khỏi edge)")
        if pol:
            lines.append(f"- {'2' if activated else '1'}) DELETE {POLICY_PATH}/{pol['policyId']}")
        if d:
            lines.append(f"- DELETE {DEF_PATH}/{d['definitionId']}")
        if not _confirm_with_admin("\n".join(lines), require_token=pol_name if activated else ""):
            return json.dumps({"status": "cancelled", "message": "Quản trị viên từ chối, không gửi gì."},
                              ensure_ascii=False, indent=2)

        steps = []
        if activated:
            resp = vm_write("POST", f"{POLICY_PATH}/deactivate/{pol['policyId']}", {})
            outcome, detail = _wait_action((resp or {}).get("id"))
            steps.append(f"deactivate: {outcome} ({detail})")
            now = _find(vm_get_data(POLICY_PATH), "policyName", pol_name)
            if now and now.get("isPolicyActivated"):
                return json.dumps({"status": "error", "error": "Deactivate chưa có hiệu lực, dừng, không xoá gì.",
                                   "steps": steps}, ensure_ascii=False, indent=2)
        if pol:
            vm_write("DELETE", f"{POLICY_PATH}/{pol['policyId']}")
            steps.append(f"đã xoá policy {pol_name}")
        if d:
            vm_write("DELETE", f"{DEF_PATH}/{d['definitionId']}")
            steps.append(f"đã xoá definition {def_name}")

        left_pol = _find(vm_get_data(POLICY_PATH), "policyName", pol_name)
        left_def = _find(vm_get_data(DEF_PATH), "name", def_name)
        ok = not left_pol and not left_def
        _audit({"scenario": "approute", "step": "remove", "policy": pol_name, "ok": ok, "steps": steps})
        res = {"status": "applied" if ok else "applied_but_not_verified",
               "message": "Đã gỡ và đọc lại từ vManage: không còn object nào." if ok
               else "Đã gửi lệnh gỡ nhưng vẫn còn object, cần kiểm tra thủ công.",
               "steps": steps, "centralized_policy_still_present": bool(left_pol),
               "definition_still_present": bool(left_def)}
        return json.dumps(res, ensure_ascii=False, indent=2)
    except (Blocked, VManageError, ValueError) as e:
        return _error("remove", e)
    except Exception as e:
        return json.dumps({"status": "error", "action": "remove", "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Trạng thái hiện tại (không LLM, để demo trước/sau bằng mắt)
# ---------------------------------------------------------------------------


def policy_status_text(sla_name: str = "Voice-And-Video") -> str:
    try:
        st = policy_state()
        sla = _by_name(st["sla_classes"], sla_name) or (st["sla_classes"][0] if st["sla_classes"] else {})
        return json.dumps({
            "active_centralized_policy": st["active_policy"],
            "centralized_policies": [{k: p[k] for k in ("name", "activated", "elements")} for p in st["centralized_policies"]],
            "edge_sla_classes": edge_sla_classes(),
            "compliance_against_sla": sla.get("name"),
            "compliance_by_color_pair": sla_compliance(sla) if sla else [],
        }, ensure_ascii=False, indent=2)
    except VManageError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
