"""
run_policy.py — Chạy riêng kịch bản 4 (App-Route policy) mà không cần sửa
main_sdwan.py. Dùng lại hàm ghi log của main_sdwan.py nên mọi tương tác vẫn
vào qa_log.md và output/lan_N.md như các kịch bản khác.

    python run_policy.py            # vòng tương tác
    python run_policy.py --status   # chỉ xem trạng thái hiện tại, không dùng LLM
    python run_policy.py --banner   # in trạng thái + hướng dẫn lúc khởi động (mặc định KHÔNG in)

Muốn có mục menu "3" trong main_sdwan.py thì thêm 2 chỗ (xem README):
    print("3. Kịch bản 4: chọn đường cho traffic bằng App-Route policy (đọc + ghi)")
    elif choice == "3":
        from scenario_policy import run_policy_mode
        run_policy_mode(_log_entry, _save_output_file)
"""

import os
import sys
import time

# Tắt các thông báo/prompt của CrewAI khi quay demo: câu hỏi "xem execution traces?"
# và telemetry. Phải đặt TRƯỚC khi import crewai. Không ghi đè nếu .env đã đặt.
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")


def _fallback_log(kind, request, answer):
    try:
        with open("qa_log.md", "a", encoding="utf-8") as f:
            f.write(f"\n## [{kind}] {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n**Yêu cầu:** {request}\n\n{answer}\n\n---\n")
    except Exception:
        pass


def _fallback_save(*args, **kwargs):
    pass


if __name__ == "__main__":
    if "--status" in sys.argv:
        from sdwan_policy_tools import policy_status_text
        print(policy_status_text())
        sys.exit(0)

    from crew_sdwan import check_llm_ready
    from scenario_policy import run_policy_mode

    check_llm_ready()
    try:  # LiteLLM (nếu có) hay in "Give Feedback / Get Help" khi lỗi
        import litellm
        litellm.suppress_debug_info = True
    except Exception:
        pass
    try:
        import main_sdwan as m
        m._init_run_file()
        log_entry, save_output = m._log_entry, m._save_output_file
    except Exception as e:  # main_sdwan.py bị sửa tên hàm: vẫn chạy, log đơn giản
        print(f"[run_policy] Không dùng được hàm log của main_sdwan.py ({e}), dùng log đơn giản.")
        log_entry, save_output = _fallback_log, _fallback_save
    run_policy_mode(log_entry, save_output, banner="--banner" in sys.argv)