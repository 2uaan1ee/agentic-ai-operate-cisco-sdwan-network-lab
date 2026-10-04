"""
sdwan_write_tools.py — Thao tác GHI duy nhất của project: quản lý Data Prefix
List trên vManage, giới hạn chặt ở tầng code.

Vì sao chọn Data Prefix List làm kịch bản ghi:
  - Là object thật trong Configuration > Policies > Lists, tạo/sửa/xoá qua
    REST API bằng JSON đơn giản.
  - List chưa được policy nào tham chiếu thì KHÔNG ảnh hưởng data plane và
    không kích hoạt push policy xuống vSmart. Tức là demo có ghi thật mà
    không làm hỏng overlay của sandbox.
  - Rollback dễ (xoá list hoặc gỡ prefix vừa thêm).

Cùng triết lý với network_write_tools.py của repo mẫu: KHÔNG có agent nào cầm
tool ghi, KHÔNG có đường nào để agent đưa JSON/URL tuỳ ý. Agent chỉ điền tham
số (action, list_name, prefixes). Payload JSON và URL do code dựng theo khuôn
cố định. Hàm này được main_sdwan.py gọi trên luồng chính.

Các lớp kiểm soát độc lập, phải qua hết trước khi có request ghi nào chạm vManage:
 1. Công tắc SDWAN_READ_ONLY (nằm trong vmanage_client.vm_write).
 2. Chỉ 4 action: create / add / remove / delete, chỉ loại dataPrefix.
 3. Tên list phải khớp ^AI_[A-Za-z0-9_-]{1,28}$. List do người dùng/lab tạo
    sẵn (không có tiền tố AI_) không sửa được qua tool này.
 4. List đang được policy tham chiếu (referenceCount > 0) hoặc readOnly thì
    chặn, vì sửa nó sẽ ảnh hưởng policy đang chạy.
 5. Prefix phải là IPv4 CIDR hợp lệ, không rộng hơn /8, không phải default
    route, không chồng lên dải quản trị 10.10.20.0/24.
 6. Đọc trạng thái hiện tại trước khi ghi: đã đúng thì 'already_configured',
    không gửi gì.
 7. Cổng xác nhận con người: in tóm tắt, chờ yes/no. Xoá list hoặc gỡ hết
    prefix phải gõ lại tên list.
 8. Đọc lại từ vManage sau khi ghi để verify trạng thái thật.
 9. Mọi thay đổi được ghi vào change_audit.log (JSON lines).
"""

import os
import re
import json
import time
import ipaddress

from vmanage_client import vm_get_data, vm_write, is_read_only, READ_ONLY_MESSAGE, VManageError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG_PATH = os.path.join(BASE_DIR, "change_audit.log")

LIST_PATH = "/dataservice/template/policy/list/dataprefix"
LIST_NAME_RE = re.compile(r"^AI_[A-Za-z0-9_-]{1,28}$")
ALLOWED_ACTIONS = {"create", "add", "remove", "delete"}
MAX_ENTRIES = 50
MIN_PREFIX_LEN = 8

# Dải không được đưa vào list qua tool này: mạng quản trị của sandbox (tương
# đương VLAN 99 bị chặn trong repo mẫu). Prefix nào chồng lên dải này đều bị
# từ chối. Default route 0.0.0.0/0 đã bị chặn riêng bởi MIN_PREFIX_LEN ở trên
# (không đưa vào đây vì mọi prefix đều "chồng" lên 0.0.0.0/0).
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.10.20.0/24"),
]


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not LIST_NAME_RE.match(name):
        raise ValueError(
            f"Tên list '{name}' không hợp lệ. Phải bắt đầu bằng 'AI_' và chỉ gồm "
            "chữ, số, '_' hoặc '-', tối đa 31 ký tự (vd AI_BRANCH_LAN)."
        )
    return name


def _normalize_prefix(prefix: str) -> str:
    raw = (prefix or "").strip()
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise ValueError(f"'{raw}' không phải prefix IPv4 hợp lệ (vd 10.10.21.0/24).")
    if net.version != 4:
        raise ValueError(f"'{raw}' không phải IPv4 — tool chỉ hỗ trợ data prefix IPv4.")
    if net.prefixlen < MIN_PREFIX_LEN:
        raise ValueError(
            f"Prefix {net} quá rộng (/{net.prefixlen}). Độ dài prefix tối thiểu được phép là "
            f"/{MIN_PREFIX_LEN} (từ /{MIN_PREFIX_LEN} đến /32)."
        )
    for blocked in BLOCKED_NETWORKS:
        if net.overlaps(blocked):
            raise ValueError(
                f"Prefix {net} chồng lên dải bị chặn {blocked} (mạng quản trị của "
                "sandbox), tool không cho đưa vào list."
            )
    return str(net)


def _normalize_prefixes(prefixes) -> list:
    if not prefixes:
        return []
    out = []
    for p in prefixes:
        n = _normalize_prefix(p)
        if n not in out:
            out.append(n)
    if len(out) > MAX_ENTRIES:
        raise ValueError(f"Tối đa {MAX_ENTRIES} prefix mỗi yêu cầu.")
    return out


# ---------------------------------------------------------------------------
# Đọc trạng thái
# ---------------------------------------------------------------------------


def _entries_of(lst: dict) -> list:
    return [e.get("ipPrefix") for e in (lst.get("entries") or []) if e.get("ipPrefix")]


def _find_list(name: str):
    for x in vm_get_data(LIST_PATH):
        if x.get("name") == name:
            return x
    return None


def _protected_reason(lst: dict):
    """Lý do list không được sửa qua tool, hoặc None nếu sửa được."""
    if lst.get("readOnly"):
        return "list ở chế độ readOnly (do hệ thống tạo)"
    refs = lst.get("referenceCount") or 0
    if refs and int(refs) > 0:
        return (
            f"list đang được {refs} policy/template tham chiếu — sửa nó có thể "
            "ảnh hưởng policy đang chạy"
        )
    return None


# ---------------------------------------------------------------------------
# Cổng xác nhận con người
# ---------------------------------------------------------------------------


def _confirm_with_admin(summary: str, warnings: list = None, require_token: str = "") -> bool:
    """In tóm tắt thay đổi và DỪNG chờ quản trị viên gõ yes/no ngay trên
    console. require_token: thay đổi rủi ro cao phải gõ lại đúng chuỗi này.
    Không có console tương tác (EOFError) thì mặc định HUỶ."""
    print("\n" + "=" * 70)
    print("[XÁC NHẬN THAY ĐỔI CẤU HÌNH] Chuẩn bị thực hiện trên vManage:")
    print(summary)
    if warnings:
        print("\nCẢNH BÁO:")
        for w in warnings:
            print(f"  - {w}")
    print("=" * 70)
    try:
        if require_token:
            answer = input(
                f"Thay đổi rủi ro cao, gõ lại chính xác '{require_token}' để xác nhận "
                "(gõ gì khác là huỷ): "
            )
            return answer.strip() == require_token
        answer = input("Đồng ý thực hiện thay đổi này trên vManage? (yes/no): ")
    except EOFError:
        print("[sdwan_write_tools] Không có console tương tác để xác nhận, mặc định HUỶ.")
        return False
    return answer.strip().lower() in ("y", "yes")


def _audit(entry: dict) -> None:
    try:
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hàm ghi chính
# ---------------------------------------------------------------------------


def _result(status: str, message: str, action: str, name: str, before=None, after=None, **extra) -> str:
    body = {
        "status": status,
        "message": message,
        "action": action,
        "list_name": name,
        "entries_before": before,
        "entries_after": after,
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False, indent=2)


def change_prefix_list_text(action: str, list_name: str, prefixes=None, description: str = "") -> str:
    """Tạo/sửa/xoá Data Prefix List. Hàm PLAIN trả JSON, gọi tay được để test.

    action:
      create : tạo list mới với `prefixes`.
      add    : thêm `prefixes` vào list có sẵn.
      remove : gỡ `prefixes` khỏi list có sẵn (không được gỡ hết, dùng delete).
      delete : xoá cả list (prefixes bị bỏ qua).

    status trả về: applied | applied_but_not_verified | already_configured |
    cancelled | blocked | error.
    """
    action = (action or "").strip().lower()
    try:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"action '{action}' không hợp lệ, chỉ cho phép: {sorted(ALLOWED_ACTIONS)}")
        name = _validate_name(list_name)
        wanted = _normalize_prefixes(prefixes)
        if action in ("create", "add", "remove") and not wanted:
            raise ValueError(f"action '{action}' cần ít nhất 1 prefix.")

        current = _find_list(name)
        before = _entries_of(current) if current else None
        warnings = []
        require_token = ""

        # ---- Phân loại trạng thái hiện tại, quyết định entries đích --------
        if action == "create":
            if current:
                if set(before) == set(wanted):
                    return _result("already_configured",
                                   f"List {name} đã tồn tại với đúng các prefix này, không gửi gì.",
                                   action, name, before, before)
                return _result("blocked",
                               f"List {name} đã tồn tại với prefix khác ({', '.join(before) or 'rỗng'}). "
                               "Dùng action 'add' hoặc 'remove' để sửa thay vì tạo lại.",
                               action, name, before, before)
            target = wanted

        else:
            if not current:
                if action == "delete":
                    return _result("already_configured",
                                   f"List {name} không tồn tại, không có gì để xoá.",
                                   action, name, None, None)
                return _result("blocked",
                               f"List {name} chưa tồn tại. Dùng action 'create' để tạo trước.",
                               action, name, None, None)
            reason = _protected_reason(current)
            if reason:
                return _result("blocked", f"Không sửa được {name}: {reason}.", action, name, before, before)

            if action == "add":
                missing = [p for p in wanted if p not in before]
                if not missing:
                    return _result("already_configured",
                                   f"{name} đã có đủ các prefix này, không gửi gì.",
                                   action, name, before, before)
                target = before + missing
                if len(target) > MAX_ENTRIES:
                    raise ValueError(f"List sau khi thêm sẽ vượt {MAX_ENTRIES} prefix.")

            elif action == "remove":
                present = [p for p in wanted if p in before]
                if not present:
                    return _result("already_configured",
                                   f"{name} không chứa prefix nào trong yêu cầu, không gửi gì.",
                                   action, name, before, before)
                target = [p for p in before if p not in present]
                if not target:
                    return _result("blocked",
                                   f"Gỡ các prefix này sẽ làm {name} rỗng, vManage không cho list rỗng. "
                                   "Dùng action 'delete' nếu muốn xoá cả list.",
                                   action, name, before, before)

            else:  # delete
                target = None
                require_token = name
                warnings.append(f"Xoá hẳn list {name} cùng {len(before)} prefix: {', '.join(before)}.")

        # Công tắc chỉ-đọc: chặn TRƯỚC khi hỏi xác nhận, để không bắt quản trị
        # viên xác nhận một thay đổi chắc chắn sẽ không được gửi.
        if is_read_only():
            return _result("blocked", READ_ONLY_MESSAGE, action, name, before, before)

        # ---- Tóm tắt + xác nhận con người ----------------------------------
        summary_lines = [
            f"- Hành động : {action.upper()}",
            f"- List      : {name}",
            f"- Hiện tại  : {', '.join(before) if before is not None else '(chưa tồn tại)'}",
            f"- Sau đổi   : {', '.join(target) if target is not None else '(list bị xoá)'}",
        ]
        if action in ("create", "add", "remove"):
            summary_lines.append(f"- Prefix yêu cầu (đã chuẩn hoá): {', '.join(wanted)}")
        if action == "create":
            summary_lines.append(f"- Request   : POST {LIST_PATH}")
        elif action == "delete":
            summary_lines.append(f"- Request   : DELETE {LIST_PATH}/{current['listId']}")
        else:
            summary_lines.append(f"- Request   : PUT {LIST_PATH}/{current['listId']}")

        if not _confirm_with_admin("\n".join(summary_lines), warnings, require_token):
            return _result("cancelled",
                           "Quản trị viên từ chối (hoặc không có console để xác nhận), "
                           "không có request ghi nào được gửi tới vManage.",
                           action, name, before, before)

        # ---- Gửi request ghi (payload do code dựng) ------------------------
        desc = (description or (current or {}).get("description") or "Managed by CrewAI demo").strip()[:200]
        sent = None
        if action == "create":
            sent = {"name": name, "description": desc, "type": "dataPrefix",
                    "entries": [{"ipPrefix": p} for p in target]}
            resp = vm_write("POST", LIST_PATH, sent)
        elif action == "delete":
            resp = vm_write("DELETE", f"{LIST_PATH}/{current['listId']}")
        else:
            sent = {"name": name, "description": desc, "type": "dataPrefix",
                    "listId": current["listId"],
                    "entries": [{"ipPrefix": p} for p in target]}
            resp = vm_write("PUT", f"{LIST_PATH}/{current['listId']}", sent)

        # ---- Verify bằng cách đọc lại trạng thái thật ----------------------
        after_list = _find_list(name)
        after = _entries_of(after_list) if after_list else None
        if action == "delete":
            verified = after_list is None
        else:
            verified = after is not None and set(after) == set(target)

        # Thông tin để hoàn tác (đảo ngược) thay đổi vừa rồi.
        if action == "create":
            rollback = {"action": "delete", "list_name": name, "prefixes": []}
        elif action == "add":
            rollback = {"action": "remove", "list_name": name,
                        "prefixes": [p for p in target if p not in before]}
        elif action == "remove":
            rollback = {"action": "add", "list_name": name,
                        "prefixes": [p for p in before if p not in target]}
        else:
            rollback = {"action": "create", "list_name": name, "prefixes": before}

        status = "applied" if verified else "applied_but_not_verified"
        _audit({"action": action, "list": name, "before": before, "after": after, "status": status})
        return _result(
            status,
            "Đã gửi thay đổi và đọc lại từ vManage." if verified
            else "Đã gửi thay đổi nhưng trạng thái đọc lại KHÔNG khớp mong đợi, cần kiểm tra thủ công.",
            action, name, before, after,
            request_sent=sent, vmanage_response=resp, rollback=rollback,
        )
    except (ValueError, VManageError) as e:
        return json.dumps({"status": "error", "action": action, "list_name": list_name, "error": str(e)},
                          ensure_ascii=False, indent=2)
    except Exception as e:  # lỗi bất ngờ vẫn trả JSON để luồng chính không crash
        return json.dumps({"status": "error", "action": action, "list_name": list_name,
                           "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False, indent=2)
