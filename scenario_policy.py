"""
scenario_policy.py — Vòng chạy tương tác của kịch bản 4 (App-Route policy).
Nằm riêng file để không đụng main_sdwan.py của bạn. main_sdwan.py chỉ cần thêm
một mục menu gọi run_policy_mode(_log_entry, _save_output_file) (xem README).

Luồng một yêu cầu:
  1. code đọc trạng thái (màu, SLA, policy đang active) từ vManage.
  2. policy_planner_agent tách yêu cầu -> PolicyIntent.
  3. code chạy apply/remove: kiểm tra, xác nhận 2 giai đoạn, ghi, verify.
  4. in bảng trước/sau do CODE dựng (hiện ngay cả khi LLM lỗi).
  5. report_agent viết báo cáo.
  6. hỏi có gỡ policy vừa tạo không.
"""

import json


def _print_evidence(result_json: str) -> None:
    """In bảng trước/sau từ JSON kết quả: không qua LLM."""
    try:
        r = json.loads(result_json)
    except ValueError:
        return
    print(f"\n--- Kết quả (code): {r.get('status')} ---")
    if r.get("message"):
        print(r["message"])
    b, a = r.get("before"), r.get("after")
    if not (b and a):
        return
    print("\nSLA class mà từng edge đang giữ:")
    print(f"  {'edge':<16}{'TRƯỚC':<34}SAU")
    for host in a["edge_sla_classes"]:
        bv, av = b["edge_sla_classes"].get(host), a["edge_sla_classes"][host]
        print(f"  {str(host):<16}{', '.join(bv) if bv else 'n/a':<34}{', '.join(av) if av else 'n/a'}")
    plan = r.get("plan", {})
    sla = plan.get("sla_class", {})
    print(f"\nTunnel đạt SLA '{sla.get('name')}' (ước tính của code, không phải kết luận của router):")
    print(f"  {'local -> remote':<36}{'đạt/tổng':<10}{'latency ms':<12}{'loss %':<8}jitter ms")
    for c in a["compliance_by_color_pair"]:
        pair = f"{c['local_color']} -> {c['remote_color']}"
        print(f"  {pair:<36}{str(c['meeting_sla']) + '/' + str(c['tunnels']):<10}"
              f"{str(c['avg_latency_ms']):<12}{str(c['avg_loss_pct']):<8}{c['avg_jitter_ms']}")
    if r.get("limits"):
        print(f"\nLưu ý: {r['limits']}")
    print()


def _offer_remove(result_json: str, kind: str, request: str, log_entry, save_output) -> None:
    from sdwan_policy_tools import remove_policy_text

    try:
        r = json.loads(result_json)
    except ValueError:
        return
    rb = r.get("rollback")
    if not rb or r.get("status") not in ("applied", "applied_but_not_verified", "created_not_activated", "activation_failed"):
        return
    ans = input(f"Gỡ policy vừa tạo ({rb['traffic_class']} -> {rb['preferred_color']})? (yes/no): ").strip().lower()
    if ans not in ("y", "yes"):
        return
    out = remove_policy_text(rb["traffic_class"], rb["preferred_color"])
    print(f"\n=== KẾT QUẢ GỠ ===\n{out}\n")
    log_entry(kind + " (gỡ)", request, out)
    save_output(kind + " (gỡ)", request, out, ok=True)


def handle_policy_request(request: str, log_entry, save_output) -> None:
    from crew_policy import build_policy_parse_crew, build_policy_report_crew, state_summary
    from sdwan_policy_tools import apply_policy_text, remove_policy_text, policy_state
    from vmanage_client import VManageError

    kind = "Traffic policy"
    steps = []
    try:
        try:
            state_text = state_summary(policy_state())
        except VManageError as e:
            msg = f"Không đọc được trạng thái policy từ vManage: {e}"
            print(f"\n=== KẾT QUẢ ===\n{msg}\n")
            log_entry(kind, request, msg)
            save_output(kind, request, msg, ok=False)
            return
        parsed = build_policy_parse_crew(request, state_text).kickoff().pydantic
        if parsed is None or parsed.error or not (parsed.action and parsed.traffic_class):
            reason = parsed.error if parsed is not None and parsed.error else "không xác định được hành động hoặc loại traffic"
            msg = (f"Không xử lý được yêu cầu: {reason}. Hãy ghi rõ, ví dụ 'ưu tiên MPLS cho voice' "
                   "hoặc 'gỡ policy voice'.")
            print(f"\n=== KẾT QUẢ ===\n{msg}\n")
            log_entry(kind, request, msg)
            save_output(kind, request, msg, ok=False)
            return
        if parsed.action == "apply" and not parsed.preferred_color:
            msg = "Thiếu màu transport cần ưu tiên (ví dụ mpls)."
            print(f"\n=== KẾT QUẢ ===\n{msg}\n")
            log_entry(kind, request, msg)
            save_output(kind, request, msg, ok=False)
            return
        steps.append(("Tham số do agent tách từ yêu cầu", parsed.model_dump_json(indent=2)))
        print(f"\n[scenario_policy] Hiểu yêu cầu: {parsed.action.upper()} {parsed.traffic_class} "
              f"-> {parsed.preferred_color or ''}")
        if parsed.action == "apply":
            result_json = apply_policy_text(parsed.traffic_class, parsed.preferred_color, parsed.sla_class)
        else:
            result_json = remove_policy_text(parsed.traffic_class, parsed.preferred_color or "")
        steps.append(("Kết quả kiểm tra + ghi + verify (code)", result_json))
    except KeyboardInterrupt:
        print("\n[scenario_policy] Đã dừng yêu cầu này (Ctrl+C).\n")
        log_entry(kind, request, "ĐÃ DỪNG (Ctrl+C)")
        save_output(kind, request, "ĐÃ DỪNG (Ctrl+C)", ok=False, steps=steps)
        return
    except Exception as e:
        print(f"\n[scenario_policy] LỖI khi xử lý yêu cầu: {e}\n")
        log_entry(kind, request, f"LỖI: {e}")
        save_output(kind, request, f"LỖI: {e}", ok=False, steps=steps)
        return

    _print_evidence(result_json)

    result = None
    try:
        result = build_policy_report_crew(request, result_json).kickoff()
        answer = str(result)
    except KeyboardInterrupt:
        answer = "(Đã dừng bước viết báo cáo, kết quả gốc:)\n\n" + result_json
    except Exception as e:
        answer = f"(Không viết được báo cáo: {e}. Kết quả gốc:)\n\n" + result_json

    print(f"\n=== BÁO CÁO ===\n{answer}\n")
    log_entry(kind, request, answer)
    save_output(kind, request, answer, ok=True, result=result, steps=steps)

    _offer_remove(result_json, kind, request, log_entry, save_output)


HELP = (
    "Kịch bản 4: chọn đường cho traffic bằng App-Route policy. Chương trình SẼ GHI thật lên vManage "
    "và activate policy trên vSmart (hỏi xác nhận 2 lần). Chỉ tạo/xoá object tên AI_*, không đụng "
    "Corp-vSmart-Policy có sẵn.\n"
    "Ví dụ:\n"
    "  - ưu tiên MPLS cho voice\n"
    "  - gỡ policy voice\n"
    "Lệnh: 'status' xem trạng thái hiện tại (không dùng LLM), 'help' xem lại hướng dẫn này, "
    "'back' thoát."
)


def run_policy_mode(log_entry, save_output, banner: bool = False) -> None:
    """Mặc định vào thẳng dấu nhắc, KHÔNG in trạng thái hay hướng dẫn (để quay demo gọn).
    banner=True (hoặc gõ 'help' / 'status' trong vòng lặp) mới in ra."""
    from sdwan_policy_tools import policy_status_text

    if banner:
        print(policy_status_text())
        print("\n" + HELP + "\n")
    while True:
        text = input("[4]> ").strip()
        low = text.lower()
        if low in ("back", "exit", "quit", "thoat"):
            break
        if not text:
            continue
        if low == "status":
            print(policy_status_text())
            continue
        if low == "help":
            print(HELP)
            continue
        handle_policy_request(text, log_entry, save_output)