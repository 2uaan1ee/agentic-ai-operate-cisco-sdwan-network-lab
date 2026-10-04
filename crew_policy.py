"""
crew_policy.py — Agent/Task/Crew cho kịch bản 4 (App-Route policy). Nằm riêng
file để không đụng crew_sdwan.py; dùng lại llm và report_agent của file đó.

  policy_planner_agent : đọc yêu cầu tự nhiên -> PolicyIntent có cấu trúc.
                         KHÔNG có tool. Danh sách màu transport, SLA class và
                         policy đang chạy được code đọc từ vManage rồi dán vào
                         prompt, nên agent không phụ thuộc việc model gọi tool.
  (code)               : sdwan_policy_tools.apply_policy_text / remove_policy_text
                         kiểm tra, hỏi xác nhận 2 giai đoạn, ghi, verify.
  report_agent         : viết báo cáo từ JSON kết quả (trước/sau).

Quyền: không agent nào cầm tool ghi. LLM chỉ điền 4 trường; payload JSON và URL
do code dựng.
"""

import os
from typing import Literal, Optional

from pydantic import BaseModel, Field
from crewai import Agent, Task, Crew, Process

from crew_sdwan import llm, REPORT_LANGUAGE
from sdwan_policy_tools import TRAFFIC_CLASSES

# Chế độ yên lặng (mặc định BẬT): không in các khung "Agent Started / Task / Final
# Answer" của CrewAI, để màn hình chỉ còn phần chương trình tự in (tóm tắt xác nhận,
# bảng trước/sau, báo cáo). Đặt SDWAN_QUIET=false trong .env khi cần debug agent.
QUIET = os.environ.get("SDWAN_QUIET", "true").strip().lower() != "false"


class PolicyIntent(BaseModel):
    """Tham số đã tách từ 1 yêu cầu về chọn đường cho traffic."""

    action: Optional[Literal["apply", "remove"]] = Field(
        None, description="'apply' = create and activate the policy; 'remove' = take the AI-created policy away")
    traffic_class: Optional[Literal["voice", "video", "critical"]] = Field(
        None, description="voice, video or critical (business-critical data)")
    preferred_color: Optional[str] = Field(
        None, description="Transport color to prefer, exactly one of the overlay colors given in the task, e.g. 'mpls'")
    sla_class: str = Field("", description="Only if the user names an SLA class; otherwise leave empty")
    error: str = Field("", description="Non-empty if the request cannot be mapped: say what is missing or unsupported")


policy_planner_agent = Agent(
    role="Traffic Steering Request Interpreter",
    goal="Turn a free-text request about steering traffic over a WAN transport into exact parameters.",
    backstory=(
        "You read short requests from network admins, often in Vietnamese, such as 'ưu tiên MPLS cho voice', "
        "'cho video đi internet', 'gỡ policy voice'. You map them to: action (apply/remove), traffic class "
        "(voice, video, critical) and the preferred transport color. You only use the traffic classes and "
        "colors that the task lists. You never invent a color or a class. You change nothing yourself: the "
        "system builds the policy with its own safety checks and asks the administrator to confirm."
    ),
    llm=llm,
    verbose=not QUIET,
    allow_delegation=False,
    max_iter=3,
)

# Report agent riêng cho kịch bản 4 (cùng vai với report_agent của crew_sdwan.py,
# nhưng tách ra để chế độ yên lặng không ảnh hưởng kịch bản 1 và 2).
policy_report_agent = Agent(
    role="Reporting Agent",
    goal="Turn the collected data into a clear, accurate report for the end user.",
    backstory=(
        "You receive data produced by the system and explain it in plain language. You never invent "
        "numbers. You quote the relevant raw figures or JSON fields so the user can cross-check."
    ),
    llm=llm,
    verbose=not QUIET,
    allow_delegation=False,
)


def state_summary(state: dict) -> str:
    sla = ", ".join(f"{s['name']}({s['latency']:g}ms/{s['loss']:g}%/{s['jitter']:g}ms)" for s in state["sla_classes"]
                    if s.get("latency") is not None and s.get("loss") is not None and s.get("jitter") is not None)
    classes = ", ".join(f"{k}={v['label']}" for k, v in TRAFFIC_CLASSES.items())
    return (f"Overlay transport colors: {state['overlay_colors']}\n"
            f"Supported traffic classes: {classes}\n"
            f"SLA classes (latency/loss/jitter): {sla}\n"
            f"Currently active centralized policy: {state['active_policy'] or 'none'}")


def build_policy_parse_crew(request: str, state_text: str) -> Crew:
    task = Task(
        description=(
            f'Request: "{request}"\n\n'
            f"Facts read from vManage:\n{state_text}\n\n"
            "Extract: action ('apply' to prefer a color for a traffic class, 'remove' to take the policy away), "
            "traffic_class (voice / video / critical) and preferred_color (must be exactly one of the overlay "
            "colors above; 'MPLS' -> 'mpls', 'internet' -> the internet color in the list). Leave sla_class empty "
            "unless the user names one. If the request asks for something else (blocking traffic, a specific "
            "application, a color not in the list, no traffic class), leave the fields empty and explain in "
            "'error'. Do not guess."
        ),
        expected_output="A PolicyIntent object.",
        agent=policy_planner_agent,
        output_pydantic=PolicyIntent,
    )
    return Crew(agents=[policy_planner_agent], tasks=[task], process=Process.sequential, verbose=not QUIET)


def build_policy_report_crew(request: str, result_json: str) -> Crew:
    task = Task(
        description=(
            f'Request: "{request}"\n\n'
            f"Result produced by the system (JSON):\n{result_json}\n\n"
            f"Write a report in {REPORT_LANGUAGE}. State the outcome by 'status': 'applied' -> the policy is active; "
            "'applied_but_not_verified' -> warn that the edges were not confirmed to hold the policy; "
            "'created_not_activated' -> objects exist but nothing was activated; 'activation_failed' -> explain "
            "using 'activation'; 'already_configured' -> nothing changed; 'cancelled' -> the administrator "
            "declined, not an error; 'blocked' or 'error' -> explain the reason from 'message'/'error'.\n"
            "When 'before' and 'after' exist, compare them: (1) edge_sla_classes per edge before vs after, which "
            "shows whether each edge received the policy; (2) compliance_by_color_pair: how many tunnels of each "
            "color pair meet the SLA, and the average latency/loss/jitter. State plainly that (2) is the "
            "system's estimate from averaged statistics. Quote the figures from the JSON; never invent numbers. "
            "Repeat the 'limits' text: no packet-level path shift was measured. Do not claim traffic moved from "
            "one link to another."
        ),
        expected_output=("A short report with the outcome, the before/after comparison with figures, "
                         "and the stated limits."),
        agent=policy_report_agent,
    )
    return Crew(agents=[policy_report_agent], tasks=[task], process=Process.sequential, verbose=not QUIET)