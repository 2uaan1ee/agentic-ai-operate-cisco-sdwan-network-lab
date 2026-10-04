"""
test_crew_build.py — Kiểm tra phần CrewAI dựng được (import, agent, task, crew,
tool wrapper) mà KHÔNG gọi LLM thật. Dùng API key giả chỉ để khởi tạo đối
tượng LLM. Chạy:  python tests/test_crew_build.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_vmanage  # noqa: E402

server, port = mock_vmanage.start(0)
os.environ.update({
    "VMANAGE_HOST": "127.0.0.1", "VMANAGE_PORT": str(port), "VMANAGE_SCHEME": "http",
    "VMANAGE_USERNAME": mock_vmanage.USER, "VMANAGE_PASSWORD": mock_vmanage.PASSWORD,
    "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "dummy-key-for-construction-only",
})

import crew_sdwan as cs  # noqa: E402
import sdwan_read_tools as rd  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


check("LLM khởi tạo đúng model mặc định của provider", "claude-haiku" in cs.llm.model, cs.llm.model)

c1 = cs.build_health_crew("overlay có ổn không?")
check("crew kịch bản 1: 2 agent, 2 task", len(c1.agents) == 2 and len(c1.tasks) == 2)
check("report_agent không có tool", not cs.report_agent.tools)
check("analyst chỉ có tool đọc", {t.name for t in cs.overlay_analyst_agent.tools} ==
      {"List SD-WAN devices", "Check overlay health of one edge", "Check overlay health of all edges"},
      [t.name for t in cs.overlay_analyst_agent.tools])

c2 = cs.build_change_parse_crew("tạo list AI_BRANCH_LAN gồm 10.10.21.0/24")
check("crew parse ticket: output_pydantic = ChangeRequest", c2.tasks[0].output_pydantic is cs.ChangeRequest)
check("change_planner chỉ có tool đọc prefix list", [t.name for t in cs.change_planner_agent.tools] == ["List data prefix lists"])

c3 = cs.build_change_report_crew("ticket", json.dumps({"status": "applied"}))
check("crew báo cáo kịch bản 2 dựng được", len(c3.tasks) == 1)

# Không agent nào cầm tool ghi: quét tên tool của mọi agent.
all_tools = [t.name.lower() for a in (cs.overlay_analyst_agent, cs.change_planner_agent, cs.report_agent) for t in a.tools]
check("không agent nào có tool ghi", not any(w in n for n in all_tools for w in ("create", "delete", "change", "write", "assign")), all_tools)

# Tool wrapper gọi được và trả JSON đúng (đường đi mà agent sẽ dùng).
out = json.loads(rd.check_overlay_health.run(device="site2-cedge01"))
check("tool wrapper check_overlay_health.run()", out["overall_status"] == "warning", out)
out = json.loads(rd.check_all_edges_health.run())
check("tool wrapper check_all_edges_health.run()", out["edge_count"] == 4)
out = json.loads(rd.list_data_prefix_lists.run())
check("tool wrapper list_data_prefix_lists.run()", out["total"] == 2)

# ChangeRequest validate đúng
req = cs.ChangeRequest(action="add", list_name="AI_X", prefixes=["10.1.1.0/24"])
check("ChangeRequest hợp lệ", req.action == "add")
try:
    cs.ChangeRequest(action="drop_everything")
    check("ChangeRequest từ chối action lạ", False)
except Exception:
    check("ChangeRequest từ chối action lạ", True)

# Import main không chạy gì (chỉ khi gọi mới chạy)
import main_sdwan  # noqa: E402
check("import main_sdwan không tự chạy menu", hasattr(main_sdwan, "_handle_change_ticket"))

print("\nPhần dựng CrewAI đạt.")
