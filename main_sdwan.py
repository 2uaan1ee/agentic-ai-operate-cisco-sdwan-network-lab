"""
main_sdwan.py — Chạy 2 kịch bản demo CrewAI trên Cisco SD-WAN sandbox.

Mode (gõ số để chọn, sau đó gõ thẳng câu hỏi / ticket):
  1. Đọc: kiểm tra sức khoẻ overlay.
  2. Ghi: change request Data Prefix List (có xác nhận, verify và hoàn tác).
  3. Kiểm tra kết nối vManage (không dùng LLM).
  0. Thoát.
Có thể hỏi nhanh 1 câu ở mode khác: "1 overlay có ổn không?".

Lưu log giống repo mẫu:
  - qa_log.md: mọi tương tác nối tiếp vào 1 file.
  - output/lan_N.md: mỗi lần chạy chương trình 1 file riêng, N = số lớn nhất
    đang có + 1, không bao giờ ghi đè file cũ.
"""

import os
import re
import sys
import json
import time

QA_LOG_PATH = "qa_log.md"
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
_RUN_FILE_RE = re.compile(r"^lan_(\d+)\.md$", re.IGNORECASE)

_run_number = None
_run_entry_count = 0


# ---------------------------------------------------------------------------
# Ghi log (bám theo main_network.py của repo mẫu)
# ---------------------------------------------------------------------------


def _next_run_number() -> int:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    numbers = [
        int(m.group(1)) for name in os.listdir(OUTPUT_DIR)
        if (m := _RUN_FILE_RE.match(name))
    ]
    return max(numbers, default=0) + 1


def _run_file_path() -> str:
    return os.path.join(OUTPUT_DIR, f"lan_{_run_number}.md")


def _init_run_file() -> None:
    global _run_number, _run_entry_count
    _run_number = _next_run_number()
    _run_entry_count = 0


def _task_outputs(result) -> list:
    outputs = []
    for t in getattr(result, "tasks_output", None) or []:
        outputs.append((str(getattr(t, "agent", "") or "?").strip(), getattr(t, "raw", "") or ""))
    return outputs


def _save_output_file(kind: str, request: str, answer: str, ok: bool, result=None, steps=None) -> None:
    """Ghi nối 1 lượt hỏi/đáp vào output/lan_N.md. Lỗi ghi file không được làm
    crash chương trình."""
    global _run_number, _run_entry_count
    try:
        if _run_number is None:
            _init_run_file()
        if _run_entry_count == 0:
            while True:
                try:
                    with open(_run_file_path(), "x", encoding="utf-8") as f:
                        f.write(f"# Lần chạy {_run_number}\n\n- Bắt đầu: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    break
                except FileExistsError:
                    _run_number = _next_run_number()

        _run_entry_count += 1
        lines = [
            "", "---", "",
            f"## Lượt {_run_entry_count} ({kind})", "",
            f"- Thời gian: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Trạng thái: {'OK' if ok else 'LỖI'}", "",
            "### Yêu cầu", "", request, "",
            "### Kết quả", "", answer, "",
        ]
        task_outputs = list(steps or []) + (_task_outputs(result) if result is not None else [])
        if task_outputs:
            lines += ["### Output từng bước (raw)", ""]
            for i, (agent, raw) in enumerate(task_outputs, 1):
                lines += [f"#### Bước {i}: {agent}", "", "````", raw, "````", ""]
        with open(_run_file_path(), "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[main_sdwan] Không lưu được file output ({e}), bỏ qua.")


def _log_entry(kind: str, request: str, answer: str) -> None:
    try:
        with open(QA_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n## [{kind}] {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"**Yêu cầu:** {request}\n\n**Kết quả:**\n\n{answer}\n\n---\n")
    except Exception as e:
        print(f"[main_sdwan] Không ghi được vào {QA_LOG_PATH} ({e}), bỏ qua.")


# ---------------------------------------------------------------------------
# Kịch bản 1: overlay health (đọc)
# ---------------------------------------------------------------------------


def _run_crew_safely(crew, kind: str, request: str, header: str) -> None:
    """Chạy crew có try/except: lỗi giữa chừng (API rate limit, vManage không
    phản hồi...) chỉ in ra và quay lại menu, không làm crash REPL."""
    try:
        result = crew.kickoff()
    except KeyboardInterrupt:
        print("\n[main_sdwan] Đã dừng yêu cầu này (Ctrl+C).\n")
        _log_entry(kind, request, "ĐÃ DỪNG (Ctrl+C)")
        _save_output_file(kind, request, "ĐÃ DỪNG (Ctrl+C)", ok=False)
        return
    except Exception as e:
        print(f"\n[main_sdwan] LỖI khi xử lý yêu cầu: {e}\n")
        _log_entry(kind, request, f"LỖI: {e}")
        _save_output_file(kind, request, f"LỖI: {e}", ok=False)
        return

    from rich.console import Console
    from rich.markdown import Markdown

    print(f"\n{header}")
    Console().print(Markdown(str(result)))
    print()
    _log_entry(kind, request, str(result))
    _save_output_file(kind, request, str(result), ok=True, result=result)


def _handle_health_question(question: str) -> None:
    from crew_sdwan import build_health_crew

    _run_crew_safely(build_health_crew(question), "Overlay health", question, "=== KẾT QUẢ ===")


# ---------------------------------------------------------------------------
# Kịch bản 2: change request Data Prefix List (đọc + ghi)
# ---------------------------------------------------------------------------


def _offer_rollback(write_result: str, kind: str, ticket: str) -> None:
    """Sau khi 'applied', hỏi có hoàn tác không. Hoàn tác đi qua đúng hàm ghi
    nên vẫn có đủ kiểm tra và vẫn hỏi xác nhận lần nữa."""
    from sdwan_write_tools import change_prefix_list_text

    try:
        data = json.loads(write_result)
    except ValueError:
        return
    rb = data.get("rollback")
    if data.get("status") != "applied" or not rb:
        return
    answer = input(
        f"Hoàn tác thay đổi vừa rồi ({rb['action']} {rb['list_name']} {' '.join(rb['prefixes'])})? (yes/no): "
    ).strip().lower()
    if answer not in ("y", "yes"):
        return
    rb_result = change_prefix_list_text(rb["action"], rb["list_name"], rb["prefixes"])
    print(f"\n=== KẾT QUẢ HOÀN TÁC ===\n{rb_result}\n")
    _log_entry(kind + " (hoàn tác)", ticket, rb_result)
    _save_output_file(kind + " (hoàn tác)", ticket, rb_result, ok=True)


def _handle_change_ticket(ticket: str) -> None:
    """Luồng xử lý 1 ticket:
    1. change_planner_agent đọc hiểu ticket -> ChangeRequest (chỉ đọc).
    2. Gọi hàm ghi TRÊN LUỒNG CHÍNH: hàm tự kiểm tra, hỏi xác nhận, ghi, đọc
       lại để verify.
    3. report_agent viết báo cáo từ JSON kết quả.
    4. Nếu đã áp dụng, hỏi có hoàn tác không."""
    from crew_sdwan import build_change_parse_crew, build_change_report_crew
    from sdwan_write_tools import change_prefix_list_text

    kind = "Change request"
    steps = []
    try:
        parsed = build_change_parse_crew(ticket).kickoff().pydantic
        if parsed is None or parsed.error or not (parsed.action and parsed.list_name):
            reason = parsed.error if parsed is not None and parsed.error else "không xác định được hành động hoặc tên list"
            msg = (
                f"Không xử lý được ticket: {reason}. Hãy ghi rõ, ví dụ "
                "'tạo list AI_BRANCH_LAN gồm 10.10.21.0/24 và 10.10.22.0/24'."
            )
            print(f"\n=== KẾT QUẢ ===\n{msg}\n")
            _log_entry(kind, ticket, msg)
            _save_output_file(kind, ticket, msg, ok=False)
            return
        steps.append(("Tham số do agent tách từ ticket", parsed.model_dump_json(indent=2)))
        print(
            f"\n[main_sdwan] Hiểu yêu cầu: {parsed.action.upper()} list {parsed.list_name} "
            f"{parsed.prefixes or ''}"
        )
        write_result = change_prefix_list_text(
            parsed.action, parsed.list_name, parsed.prefixes, parsed.description
        )
        steps.append(("Kết quả kiểm tra + ghi + verify (code)", write_result))
    except KeyboardInterrupt:
        print("\n[main_sdwan] Đã dừng yêu cầu này (Ctrl+C).\n")
        _log_entry(kind, ticket, "ĐÃ DỪNG (Ctrl+C)")
        _save_output_file(kind, ticket, "ĐÃ DỪNG (Ctrl+C)", ok=False, steps=steps)
        return
    except Exception as e:
        print(f"\n[main_sdwan] LỖI khi xử lý ticket: {e}\n")
        _log_entry(kind, ticket, f"LỖI: {e}")
        _save_output_file(kind, ticket, f"LỖI: {e}", ok=False, steps=steps)
        return

    # Việc ghi (nếu có) đã xong. Lỗi ở bước viết báo cáo không được làm mất
    # kết quả, nên khi đó in thẳng JSON.
    result = None
    try:
        result = build_change_report_crew(ticket, write_result).kickoff()
        answer = str(result)
    except KeyboardInterrupt:
        answer = "(Đã dừng bước viết báo cáo, kết quả gốc:)\n\n" + write_result
    except Exception as e:
        answer = f"(Không viết được báo cáo: {e}. Kết quả gốc:)\n\n" + write_result

    from rich.console import Console
    from rich.markdown import Markdown

    print("\n=== KẾT QUẢ ===")
    Console().print(Markdown(str(answer)))
    print()
    _log_entry(kind, ticket, answer)
    _save_output_file(kind, ticket, answer, ok=True, result=result, steps=steps)

    _offer_rollback(write_result, kind, ticket)


# ---------------------------------------------------------------------------
# Kiểm tra kết nối (không dùng LLM)
# ---------------------------------------------------------------------------


def _check_connection() -> None:
    from sdwan_read_tools import list_sdwan_devices_text

    print("Đang đăng nhập vManage và đọc danh sách thiết bị...")
    out = list_sdwan_devices_text()
    try:
        data = json.loads(out)
    except ValueError:
        print(out)
        return
    if "error" in data:
        print(f"LỖI: {data['error']}")
        return
    print(f"Kết nối OK. vManage đang quản lý {data['total']} thiết bị:\n")
    for d in data["devices"]:
        print(f"  {str(d['hostname']):<18} {str(d['system_ip']):<16} site {str(d['site_id']):<5} "
              f"{str(d['type']):<16} {d['reachability']}")
    print()


if __name__ == "__main__":
    # Lệnh nhanh không cần LLM: python main_sdwan.py --check
    if "--check" in sys.argv:
        _check_connection()
        sys.exit(0)

    from crew_sdwan import check_llm_ready

    check_llm_ready()
    _init_run_file()

    # Mode: 1 = đọc (overlay health), 2 = ghi (Data Prefix List), 3 = kiểm tra
    # kết nối. Gõ số để đổi mode, hoặc "<số> <câu hỏi>" để hỏi luôn 1 câu.
    handlers = {"1": _handle_health_question, "2": _handle_change_ticket}
    mode = None

    from crewai.events.listeners.tracing.utils import set_suppress_tracing_messages
    set_suppress_tracing_messages(True)

    while True:
        try:
            line = input(f"[{mode}]> " if mode else "mode> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == "0" or line.lower() in ("exit", "quit", "thoat"):
            break

        head, _, rest = line.partition(" ")
        if head == "3":
            _check_connection()
            continue
        if head in handlers:
            if rest.strip():
                handlers[head](rest.strip())
            else:
                mode = head
            continue
        if mode is None:
            continue
        handlers[mode](line)
