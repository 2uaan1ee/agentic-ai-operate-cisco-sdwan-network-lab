"""
sdwan_read_tools.py — Tool CHỈ ĐỌC cho CrewAI agent, đọc dữ liệu qua vManage
REST API (tương đương network_tools.py + network_monitoring_tools.py của repo
mẫu).

Triết lý giữ nguyên như repo mẫu: số liệu và trạng thái (ok/warning/critical)
do CODE tính sẵn, LLM chỉ đọc trường 'status' rồi diễn giải. LLM không tự đếm
BFD session, không tự phán đoán "bao nhiêu là nhiều".

Module này chỉ import `vm_get_data` / `vm_get` từ vmanage_client, không import
`vm_write`, nên về mặt cấu trúc không có đường nào từ tool đọc đi tới lệnh ghi.

Mỗi chức năng có 2 lớp: hàm PLAIN `*_text` (gọi tay/test độc lập được) và
wrapper `@tool` mỏng để agent dùng — cùng pattern plain/tool-wrapper như repo
mẫu.
"""

import json
from crewai.tools import tool

from vmanage_client import vm_get_data

# Loại thiết bị là control plane. Mọi thiết bị khác trong /dataservice/device
# (vedge-C8000V, vedge-cloud, ...) coi là edge.
CONTROLLER_TYPES = {"vmanage", "vsmart", "vbond"}

# Peer type trên control connection của edge mà ta dùng để đánh giá trạng thái.
# vbond bị bỏ qua có chủ đích: kết nối tới vBond chỉ tồn tại thoáng qua lúc
# edge mới lên (vBond chỉ giúp edge tìm vSmart/vManage rồi ngắt), nên state
# "connect" của peer vBond là bình thường, không phải lỗi.
STATUS_PEER_TYPES = {"vsmart", "vmanage"}

MAX_ROWS = 60  # cắt bớt danh sách dài trước khi đưa vào prompt gửi LLM


def _is_controller(dev: dict) -> bool:
    dtype = str(dev.get("device-type") or dev.get("personality") or "").lower()
    return dtype in CONTROLLER_TYPES


def _dev_ip(dev: dict) -> str:
    return str(dev.get("deviceId") or dev.get("system-ip") or "")


def _normalize_device(d: dict) -> dict:
    return {
        "hostname": d.get("host-name"),
        "system_ip": _dev_ip(d),
        "type": d.get("device-type") or d.get("personality"),
        "model": d.get("device-model"),
        "site_id": d.get("site-id"),
        "reachability": d.get("reachability"),
        "vmanage_status": d.get("status"),
        "version": d.get("version"),
        "is_controller": _is_controller(d),
    }


def _all_devices() -> list:
    return vm_get_data("/dataservice/device")


def _resolve_device(name_or_ip: str) -> dict:
    """Tìm thiết bị theo hostname hoặc system-ip. Hostname khớp không phân
    biệt hoa/thường; chấp nhận khớp một phần nếu chỉ có đúng 1 kết quả."""
    q = (name_or_ip or "").strip().lower()
    devices = _all_devices()
    if not q:
        raise ValueError("Thiếu tên hoặc system-ip của thiết bị.")
    exact = [d for d in devices if q in (str(d.get("host-name", "")).lower(), _dev_ip(d).lower())]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in devices if q in str(d.get("host-name", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(d.get("host-name")) for d in devices) or "(không có thiết bị)"
    if len(partial) > 1:
        raise ValueError(f"'{name_or_ip}' khớp nhiều thiết bị: {names}. Hãy nêu rõ hơn.")
    raise ValueError(f"Không tìm thấy thiết bị '{name_or_ip}'. Thiết bị có sẵn: {names}")


def _worst(*statuses: str) -> str:
    order = {"ok": 0, "warning": 1, "critical": 2}
    return max(statuses, key=lambda s: order.get(s, 0))


# ---------------------------------------------------------------------------
# Hàm PLAIN
# ---------------------------------------------------------------------------


def list_sdwan_devices_text() -> str:
    """Danh sách toàn bộ thiết bị vManage đang quản lý (controller + edge)."""
    try:
        devices = [_normalize_device(d) for d in _all_devices()]
        return json.dumps(
            {"total": len(devices), "devices": devices[:MAX_ROWS]},
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _control_summary(dev_ip: str) -> dict:
    rows = vm_get_data("/dataservice/device/control/connections", {"deviceId": dev_ip})
    considered = [r for r in rows if str(r.get("peer-type", "")).lower() in STATUS_PEER_TYPES]
    up = [r for r in considered if str(r.get("state", "")).lower() == "up"]
    down = [r for r in considered if str(r.get("state", "")).lower() != "up"]
    if not considered or not up:
        status = "critical"
    elif down:
        status = "warning"
    else:
        status = "ok"
    return {
        "counted_peer_types": sorted(STATUS_PEER_TYPES),
        "total": len(considered),
        "up": len(up),
        "down": len(down),
        "down_connections": [
            {"peer_type": r.get("peer-type"), "peer_system_ip": r.get("system-ip"),
             "state": r.get("state"), "local_color": r.get("local-color")}
            for r in down[:MAX_ROWS]
        ],
        "status": status,
    }


def _bfd_summary(dev_ip: str) -> dict:
    rows = vm_get_data("/dataservice/device/bfd/sessions", {"deviceId": dev_ip})
    up = [r for r in rows if str(r.get("state", "")).lower() == "up"]
    down = [r for r in rows if str(r.get("state", "")).lower() != "up"]
    if not rows or not up:
        status = "critical"
    elif down:
        status = "warning"
    else:
        status = "ok"
    return {
        "total": len(rows),
        "up": len(up),
        "down": len(down),
        "sessions": [
            {"remote_system_ip": r.get("system-ip"), "remote_site_id": r.get("site-id"),
             "local_color": r.get("local-color") or r.get("color"),
             "remote_color": r.get("color"), "state": r.get("state")}
            for r in rows[:MAX_ROWS]
        ],
        "status": status,
    }


def check_overlay_health_text(device: str) -> str:
    """Kiểm tra 1 edge: reachability, control connection tới vSmart/vManage,
    BFD session. Trả JSON có sẵn status từng phần và overall_status."""
    try:
        dev = _resolve_device(device)
        ip = _dev_ip(dev)
        reach = str(dev.get("reachability", "")).lower()
        reach_status = "ok" if reach == "reachable" else "critical"
        report = {
            "device": _normalize_device(dev),
            "reachability_status": reach_status,
        }
        if _is_controller(dev):
            report["note"] = (
                "Đây là controller (vManage/vSmart/vBond), không có BFD session. "
                "Chỉ có reachability được đánh giá."
            )
            report["overall_status"] = reach_status
        else:
            ctrl = _control_summary(ip)
            bfd = _bfd_summary(ip)
            report["control_connections"] = ctrl
            report["bfd"] = bfd
            report["overall_status"] = _worst(reach_status, ctrl["status"], bfd["status"])
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def check_all_edges_health_text() -> str:
    """Quét toàn bộ edge, trả bảng tóm tắt 1 dòng/edge + tổng số theo trạng
    thái. Dùng cho câu hỏi chung kiểu 'overlay có ổn không?'."""
    try:
        devices = _all_devices()
        edges = [d for d in devices if not _is_controller(d)]
        rows = []
        for d in edges[:MAX_ROWS]:
            ip = _dev_ip(d)
            reach = str(d.get("reachability", "")).lower()
            reach_status = "ok" if reach == "reachable" else "critical"
            try:
                ctrl, bfd = _control_summary(ip), _bfd_summary(ip)
                overall = _worst(reach_status, ctrl["status"], bfd["status"])
                rows.append({
                    "hostname": d.get("host-name"), "system_ip": ip, "site_id": d.get("site-id"),
                    "reachability": d.get("reachability"),
                    "control_up": f"{ctrl['up']}/{ctrl['total']}",
                    "bfd_up": f"{bfd['up']}/{bfd['total']}",
                    "overall_status": overall,
                })
            except Exception as e:
                rows.append({"hostname": d.get("host-name"), "system_ip": ip,
                             "overall_status": "unknown", "error": str(e)})
        counts = {"ok": 0, "warning": 0, "critical": 0, "unknown": 0}
        for r in rows:
            counts[r["overall_status"]] = counts.get(r["overall_status"], 0) + 1
        controllers = [_normalize_device(d) for d in devices if _is_controller(d)]
        return json.dumps(
            {"edge_count": len(rows), "status_counts": counts, "edges": rows,
             "controllers": controllers},
            ensure_ascii=False, indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def list_data_prefix_lists_text() -> str:
    """Danh sách Data Prefix List hiện có trên vManage (chỉ đọc). Dùng cho
    kịch bản 2 để biết list nào đã có, đang được policy nào tham chiếu."""
    try:
        lists = vm_get_data("/dataservice/template/policy/list/dataprefix")
        out = [
            {
                "listId": x.get("listId"),
                "name": x.get("name"),
                "description": x.get("description"),
                "entries": [e.get("ipPrefix") for e in (x.get("entries") or [])],
                "referenceCount": x.get("referenceCount"),
                "readOnly": x.get("readOnly"),
            }
            for x in lists
        ]
        return json.dumps({"total": len(out), "lists": out[:MAX_ROWS]}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool cho CrewAI agent. Docstring là thứ LLM đọc để chọn tool, viết rõ ràng.
# Mỗi tool chỉ gọi lại hàm PLAIN ở trên.
# ---------------------------------------------------------------------------


@tool("List SD-WAN devices")
def list_sdwan_devices() -> str:
    """List every device managed by vManage (vManage, vBond, vSmart and the
    edge routers) with hostname, system-ip, site-id, reachability and
    software version. Use this first when you are not sure of the exact
    hostname the user is talking about (e.g. 'site 1 router')."""
    return list_sdwan_devices_text()


@tool("Check overlay health of one edge")
def check_overlay_health(device: str) -> str:
    """Check ONE edge device: reachability, control connections to
    vSmart/vManage and BFD sessions (IPsec tunnels in the overlay). The
    result already contains 'status' fields (ok/warning/critical) computed in
    code; read them as given, do not judge the numbers yourself.

    Args:
        device: Hostname (e.g. 'site1-cedge01') or system-ip of the device.
    """
    return check_overlay_health_text(device)


@tool("Check overlay health of all edges")
def check_all_edges_health() -> str:
    """Scan ALL edge devices and return one summary row per edge (control
    connections up/total, BFD sessions up/total, overall status) plus a count
    of edges per status. Use this for broad questions such as 'is the overlay
    healthy?' or 'which site has a problem?'."""
    return check_all_edges_health_text()


@tool("List data prefix lists")
def list_data_prefix_lists() -> str:
    """List the Data Prefix Lists that exist on vManage, with their prefixes
    and how many policies reference each list. Read-only. Use it to check
    whether a list already exists before a change request."""
    return list_data_prefix_lists_text()
