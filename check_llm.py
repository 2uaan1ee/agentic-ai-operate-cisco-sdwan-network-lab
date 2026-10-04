"""
check_llm.py — Kiểm tra LLM trong .env có dùng được cho project này không,
TRƯỚC khi chạy kịch bản. Không cần VPN, không đụng vManage.

Chạy:  python check_llm.py

Hai bài kiểm tra:
  1. Gọi LLM trả lời 1 câu đơn giản (key, base URL, tên model có đúng không).
  2. Cho 1 agent một tool giả và yêu cầu gọi nó. Agent của project phụ thuộc
     vào việc model gọi tool đúng, nên đây là bài kiểm tra quan trọng hơn.
     Tool trả về một con số ngẫu nhiên mà LLM không thể đoán, nên nếu con số
     đó xuất hiện trong câu trả lời nghĩa là model đã thực sự gọi tool.
"""

import random
import sys

from crewai import Agent, Task, Crew, Process
from crewai.tools import tool

from crew_sdwan import llm, check_llm_ready, LLM_PROVIDER, MODEL_NAME, OPENAI_BASE_URL

SECRET = str(random.randint(100000, 999999))
CALLS = []


@tool("Get lab code")
def get_lab_code() -> str:
    """Return the lab verification code. Call this tool to obtain the code;
    the code cannot be guessed."""
    CALLS.append(1)
    return f"LAB-CODE-{SECRET}"


def main() -> int:
    check_llm_ready()
    print(f"Provider : {LLM_PROVIDER}")
    print(f"Model    : {MODEL_NAME}")
    print(f"Base URL : {OPENAI_BASE_URL or '(mặc định của provider)'}\n")

    print("[1/2] Gọi LLM trả lời 1 câu...")
    try:
        reply = llm.call("Reply with exactly one word: pong")
    except Exception as e:
        print(f"THẤT BẠI: {type(e).__name__}: {e}\n")
        print("Kiểm tra: API key, OPENAI_BASE_URL, tên MODEL_NAME (đúng chữ hoa/thường "
              "theo danh sách model của FPT), và máy có ra được internet không.")
        return 1
    print(f"OK, model trả lời: {str(reply).strip()[:80]!r}\n")

    print("[2/2] Kiểm tra model có gọi tool không...")
    agent = Agent(
        role="Tool tester",
        goal="Call the provided tool and report the code it returns.",
        backstory="You always use the tool when asked and never invent values.",
        tools=[get_lab_code],
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=4,
    )
    task = Task(
        description="Call the 'Get lab code' tool and answer with the exact code it returns.",
        expected_output="The exact lab code returned by the tool.",
        agent=agent,
    )
    try:
        out = str(Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff())
    except Exception as e:
        print(f"THẤT BẠI: {type(e).__name__}: {e}")
        return 1

    if CALLS and SECRET in out:
        print(f"OK, model đã gọi tool {len(CALLS)} lần và trả đúng mã. Dùng được cho project.")
        return 0
    if CALLS:
        print("MỘT PHẦN: tool đã được gọi nhưng câu trả lời không chứa đúng mã.")
        print(f"Câu trả lời: {out[:200]!r}")
        print("Model đọc kết quả tool chưa ổn định. Thử model khác trong MODEL_NAME.")
        return 1
    print("THẤT BẠI: model KHÔNG gọi tool (có thể tự bịa ra câu trả lời).")
    print(f"Câu trả lời: {out[:200]!r}")
    print("Đổi MODEL_NAME sang model khác (xem gợi ý trong .env.fpt.example). Nếu không "
          "model nào gọi được tool, project này sẽ không chạy đúng với FPT.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
