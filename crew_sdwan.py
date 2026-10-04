"""
crew_sdwan.py — Định nghĩa LLM, agent, task, crew cho 2 kịch bản demo trên
Cisco SD-WAN sandbox. Cấu trúc bám theo crew_network.py của repo mẫu.

Kịch bản 1 (ĐỌC)  : kiểm tra sức khoẻ overlay.
    overlay_analyst_agent (tool đọc) -> report_agent (không tool).
Kịch bản 2 (ĐỌC + GHI): change request quản lý Data Prefix List.
    change_planner_agent (đọc ticket -> ChangeRequest có cấu trúc)
    -> code ghi + verify trong main_sdwan.py (có xác nhận con người)
    -> report_agent viết báo cáo.

Mô hình quyền, giữ nguyên ý tưởng của repo mẫu:
  READ  : overlay_analyst_agent, change_planner_agent chỉ có tool đọc.
  WRITE : KHÔNG agent nào cầm tool ghi. Lý do giống repo mẫu: CrewAI chạy tool
          trong worker thread dưới khung log Rich nên câu hỏi yes/no của
          input() dễ bị che hoặc treo, và agent hay đi đường vòng. LLM quyết
          định tham số, CODE thực thi.
  report_agent: không có tool, chỉ diễn giải dữ liệu đã có.

Lưu ý riêng tư: khi dùng API cloud, câu hỏi và dữ liệu đọc từ vManage (tên
thiết bị, system-ip, trạng thái tunnel) được gửi tới nhà cung cấp LLM. Với
sandbox của Cisco thì không có vấn đề, nhưng nếu áp dụng sang mạng thật cần
cân nhắc lại.
"""

import os
from typing import List, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from crewai import Agent, Task, Crew, Process, LLM

from sdwan_read_tools import (
    list_sdwan_devices,
    check_overlay_health,
    check_all_edges_health,
    list_data_prefix_lists,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Cấu hình LLM — đọc từ .env, không hard-code API key trong code.
# ---------------------------------------------------------------------------

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-5-mini",
}
_API_KEY_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

if LLM_PROVIDER not in _DEFAULT_MODELS:
    raise RuntimeError(
        f"LLM_PROVIDER='{LLM_PROVIDER}' không hợp lệ trong .env, chỉ hỗ trợ "
        "'anthropic' hoặc 'openai'."
    )

MODEL_NAME = os.environ.get("MODEL_NAME") or _DEFAULT_MODELS[LLM_PROVIDER]
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip()

if OPENAI_BASE_URL and LLM_PROVIDER != "openai":
    raise RuntimeError("OPENAI_BASE_URL chỉ dùng được khi LLM_PROVIDER=openai.")
if OPENAI_BASE_URL and not os.environ.get("MODEL_NAME"):
    raise RuntimeError(
        "Đang dùng OPENAI_BASE_URL tùy chỉnh nhưng MODEL_NAME để trống, hãy điền "
        "đúng tên model của endpoint đó."
    )

# Ngôn ngữ báo cáo cuối. Prompt của agent giữ tiếng Anh (model theo prompt
# tiếng Anh ổn định hơn), còn câu trả lời cho người dùng theo biến này.
REPORT_LANGUAGE = os.environ.get("REPORT_LANGUAGE", "Vietnamese").strip() or "Vietnamese"


def check_llm_ready() -> None:
    """Báo lỗi rõ ràng nếu thiếu API key, thay vì để lỗi 401 khó hiểu văng ra
    giữa lúc crew đang chạy."""
    key_var = _API_KEY_ENV_VAR[LLM_PROVIDER]
    if not os.environ.get(key_var):
        raise RuntimeError(f"Thiếu {key_var} trong .env, điền API key rồi chạy lại.")


# temperature thấp vì đây là tác vụ kỹ thuật cần chính xác.
_llm_kwargs = {"model": f"{LLM_PROVIDER}/{MODEL_NAME}", "temperature": 0.2}
if OPENAI_BASE_URL:
    _llm_kwargs["base_url"] = OPENAI_BASE_URL
llm = LLM(**_llm_kwargs)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

overlay_analyst_agent = Agent(
    role="SD-WAN Overlay Analyst",
    goal=(
        "Collect accurate overlay health data from vManage (device "
        "reachability, control connections, BFD sessions) that answers the "
        "user's question."
    ),
    backstory=(
        "You are an SD-WAN operations engineer working with Cisco "
        "Catalyst SD-WAN (vManage, vSmart, vBond, cEdge/vEdge). The lab uses "
        "a hub-and-spoke overlay: branch edges build IPsec tunnels (BFD "
        "sessions) only to the DC hub. When the user names a site loosely "
        "(e.g. 'site 1'), list the devices first to find the exact hostname. "
        "For broad questions ('is the overlay healthy?') use the all-edges "
        "tool; for one device use the single-device tool. The tools return "
        "a 'status' (ok/warning/critical) already computed in code. Read that "
        "field as given and never re-judge the numbers yourself."
    ),
    tools=[list_sdwan_devices, check_overlay_health, check_all_edges_health],
    llm=llm,
    verbose=True,
    allow_delegation=False,
    max_iter=8,
)

change_planner_agent = Agent(
    role="Change Request Interpreter",
    goal=(
        "Turn a free-text change ticket about a vManage Data Prefix List into "
        "exact parameters: action, list name and prefixes."
    ),
    backstory=(
        "You read short change tickets written by network admins, often in "
        "Vietnamese, e.g. 'tạo list AI_BRANCH_LAN gồm 10.10.21.0/24 và "
        "10.10.22.0/24', 'thêm 10.10.24.0/24 vào AI_BRANCH_LAN', 'gỡ "
        "10.10.22.0/24 khỏi AI_BRANCH_LAN', 'xoá list AI_BRANCH_LAN'. You may "
        "call the prefix-list tool ONCE to see which lists already exist so "
        "you can tell whether the ticket means create or add. You do NOT "
        "change anything yourself: the system performs the change after you, "
        "with its own safety checks and an administrator confirmation."
    ),
    tools=[list_data_prefix_lists],
    llm=llm,
    verbose=True,
    allow_delegation=False,
    max_iter=4,
)

report_agent = Agent(
    role="Reporting Agent",
    goal="Turn the collected data into a clear, accurate report for the end user.",
    backstory=(
        "You receive data produced by tools and by the system and explain it "
        "in plain language. You never invent numbers. You quote the "
        "relevant raw figures or JSON fields so the user can cross-check."
    ),
    llm=llm,
    verbose=True,
    allow_delegation=False,
)


# ---------------------------------------------------------------------------
# Kịch bản 1: Overlay health check (ĐỌC)
# ---------------------------------------------------------------------------


def build_health_crew(question: str) -> Crew:
    task_collect = Task(
        description=(
            f'User question: "{question}"\n\n'
            "Decide which device(s) are relevant and call the right tool(s). "
            "Broad question -> the all-edges tool. One named site/device -> "
            "the single-device tool (list devices first if the name is vague)."
        ),
        expected_output=(
            "The JSON data returned by the tools, including every 'status' "
            "field, for the devices relevant to the question."
        ),
        agent=overlay_analyst_agent,
    )

    task_report = Task(
        description=(
            "Using the data from the previous step, write a report for the "
            f'question: "{question}"\n'
            f"Write it in {REPORT_LANGUAGE}. Start with the overall status, then "
            "give the figures (control connections up/total, BFD sessions "
            "up/total) per device. Read the 'status' fields exactly as given. "
            "If anything is 'warning' or 'critical', name the device and the "
            "failing tunnel/connection and propose a concrete next check "
            "(e.g. which transport color is down, what to look at on that "
            "edge). If everything is 'ok', say so and that no action is "
            "needed. Do not invent data that is not in the tool output."
        ),
        expected_output=(
            "A concise report with overall status, per-device figures, and a "
            "recommended action only when something is not ok."
        ),
        agent=report_agent,
        context=[task_collect],
    )

    return Crew(
        agents=[overlay_analyst_agent, report_agent],
        tasks=[task_collect, task_report],
        process=Process.sequential,
        verbose=True,
    )


# ---------------------------------------------------------------------------
# Kịch bản 2: Change request Data Prefix List (ĐỌC + GHI)
# ---------------------------------------------------------------------------


class ChangeRequest(BaseModel):
    """Tham số đã tách từ 1 ticket quản lý Data Prefix List."""

    action: Optional[Literal["create", "add", "remove", "delete"]] = Field(
        None,
        description=(
            "'create' = new list; 'add' = add prefixes to an existing list; "
            "'remove' = remove some prefixes from a list; 'delete' = delete the whole list"
        ),
    )
    list_name: Optional[str] = Field(None, description="Exact list name, e.g. 'AI_BRANCH_LAN'")
    prefixes: List[str] = Field(
        default_factory=list,
        description="IPv4 prefixes in CIDR form, e.g. ['10.10.21.0/24']. Empty for 'delete'.",
    )
    description: str = Field("", description="Optional short description for a new list")
    error: str = Field(
        "",
        description="Non-empty if something cannot be determined from the ticket: say what is missing",
    )


def build_change_parse_crew(ticket: str) -> Crew:
    """Crew 1 agent, CHỈ đọc hiểu ticket -> ChangeRequest. Không có tool ghi.
    main_sdwan.py nhận tham số rồi tự gọi hàm ghi (có kiểm tra + xác nhận)."""
    task_parse = Task(
        description=(
            f'Change ticket: "{ticket}"\n\n'
            "Extract: the action (create / add / remove / delete), the exact "
            "list name, and the IPv4 prefixes in CIDR form. If the ticket does "
            "not say whether the list exists, call the prefix-list tool once "
            "and decide: list missing -> create, list present -> add. Do not "
            "guess: if the action, the list name or the prefixes needed for "
            "the action are missing, leave them empty and explain in 'error'. "
            "Do not rename lists or change prefixes the ticket gave."
        ),
        expected_output="A ChangeRequest object.",
        agent=change_planner_agent,
        output_pydantic=ChangeRequest,
    )
    return Crew(
        agents=[change_planner_agent],
        tasks=[task_parse],
        process=Process.sequential,
        verbose=True,
    )


def build_change_report_crew(ticket: str, write_result_json: str) -> Crew:
    """report_agent viết báo cáo từ JSON kết quả của hàm ghi (đã chạy xong
    trong main_sdwan.py). Agent không có tool, chỉ diễn giải."""
    task_report = Task(
        description=(
            f'Ticket: "{ticket}"\n\n'
            "Result of the change, produced by the system (JSON):\n"
            f"{write_result_json}\n\n"
            f"Write a report in {REPORT_LANGUAGE}. Say what the system found "
            "before the change (entries_before) and the outcome by 'status': "
            "'applied' -> report the real list content read back from vManage "
            "(entries_after); 'applied_but_not_verified' -> warn clearly that "
            "the read-back does not match what was requested; "
            "'already_configured' -> nothing needed to change; 'cancelled' -> "
            "the administrator declined, this is not an error; 'blocked' or "
            "'error' -> explain the reason in plain language using 'message' "
            "or 'error'. Do not claim anything that is not in the JSON."
        ),
        expected_output=(
            "A short report stating the checks done, the outcome, and the "
            "list content before and after when available."
        ),
        agent=report_agent,
    )
    return Crew(
        agents=[report_agent],
        tasks=[task_report],
        process=Process.sequential,
        verbose=True,
    )
