"""
test_scenario_flow.py — Kiểm tra luồng tương tác của kịch bản 4 (scenario_policy.py)
với LLM giả (không gọi API). Chạy:  python tests/test_scenario_flow.py
"""

import os
import sys
import json
import builtins
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_vmanage  # noqa: E402

server, port = mock_vmanage.start(0)
os.environ.update({
    "VMANAGE_HOST": "127.0.0.1", "VMANAGE_PORT": str(port), "VMANAGE_SCHEME": "http",
    "VMANAGE_USERNAME": mock_vmanage.USER, "VMANAGE_PASSWORD": mock_vmanage.PASSWORD,
    "SDWAN_READ_ONLY": "false", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "dummy-key-for-construction-only",
    "SDWAN_POLICY_SITE_LISTS": "All-Sites", "SDWAN_POLICY_VPN_LIST": "Corp-VPN",
    "SDWAN_POLICY_POLL_SEC": "0.01", "SDWAN_POLICY_AFTER_WAIT": "0.05", "SDWAN_POLICY_AFTER_POLL": "0.01",
})

import crew_policy as cp  # noqa: E402
import scenario_policy as sp  # noqa: E402


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL: {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


# --- dựng crew thật (không chạy) -------------------------------------------
check("planner không có tool (không phụ thuộc function calling)", not cp.policy_planner_agent.tools)
c = cp.build_policy_parse_crew("ưu tiên MPLS cho voice", "facts")
check("crew parse: output_pydantic = PolicyIntent", c.tasks[0].output_pydantic is cp.PolicyIntent)
check("crew báo cáo dựng được", len(cp.build_policy_report_crew("x", "{}").tasks) == 1)
try:
    cp.PolicyIntent(action="apply", traffic_class="karaoke")
    check("PolicyIntent từ chối traffic_class lạ", False)
except Exception:
    check("PolicyIntent từ chối traffic_class lạ", True)

# --- LLM giả: thay hai hàm dựng crew -----------------------------------------
class _Out:
    def __init__(self, **kw):
        self.pydantic = cp.PolicyIntent(**kw)
        self.raw = "fake"


class _FakeParse:
    def __init__(self, **kw):
        self.kw = kw

    def kickoff(self):
        return _Out(**self.kw)


class _FakeReport:
    def kickoff(self):
        return "BÁO CÁO GIẢ"


_logs, _saves = [], []
log_entry = lambda kind, req, ans: _logs.append((kind, req, ans))  # noqa: E731
save_output = lambda kind, req, ans, ok, result=None, steps=None: _saves.append((kind, ok))  # noqa: E731

_answers = []
builtins.input = lambda prompt="": _answers.pop(0) if _answers else (_ for _ in ()).throw(EOFError())


def run(intent_kwargs, *answers):
    cp.build_policy_parse_crew = lambda req, st: _FakeParse(**intent_kwargs)
    cp.build_policy_report_crew = lambda req, res: _FakeReport()
    _answers.extend(answers)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sp.handle_policy_request("ưu tiên MPLS cho voice", log_entry, save_output)
    return buf.getvalue()


out = run({"action": "apply", "traffic_class": "voice", "preferred_color": "mpls"},
          "yes", "AI_voice_mpls_pol", "no")
check("in bảng trước/sau do code dựng", "TRƯỚC" in out and "Voice-And-Video" in out and "đạt/tổng" in out, out[-900:])
check("bảng nói rõ là ước tính của code", "ước tính của code" in out)
check("hiện báo cáo của report_agent", "BÁO CÁO GIẢ" in out)
check("policy đã active sau luồng", any(p["isPolicyActivated"] for p in mock_vmanage.VSMART_POLICIES))
check("log + output được lưu", _logs and _saves and _saves[-1][1] is True)

out = run({"action": "apply", "traffic_class": "voice", "preferred_color": "mpls"})  # đã active: không có câu hỏi nào
check("apply lại khi đã active -> already_configured, không hỏi gì",
      "already_configured" in out and not _answers, out[-400:])

# gỡ qua yêu cầu 'remove'
out = run({"action": "remove", "traffic_class": "voice", "preferred_color": "mpls"}, "AI_voice_mpls_pol")
check("remove qua agent", "applied" in out and not any(p["isPolicyActivated"] for p in mock_vmanage.VSMART_POLICIES), out[-500:])

# hoàn tác ngay sau apply (trả lời yes ở câu hỏi gỡ)
out = run({"action": "apply", "traffic_class": "voice", "preferred_color": "mpls"},
          "yes", "AI_voice_mpls_pol", "yes", "AI_voice_mpls_pol")
check("đề nghị gỡ sau apply và gỡ được", "KẾT QUẢ GỠ" in out
      and not any(p["policyName"].startswith("AI_") for p in mock_vmanage.VSMART_POLICIES), out[-600:])

# yêu cầu không hiểu / thiếu màu
out = run({"error": "yêu cầu chặn traffic không được hỗ trợ"})
check("agent báo lỗi -> hướng dẫn ví dụ, không ghi", "Không xử lý được" in out)
out = run({"action": "apply", "traffic_class": "voice"})
check("thiếu màu -> báo thiếu", "Thiếu màu" in out)
check("không để lại object AI_ nào", not any(d["name"].startswith("AI_") for d in mock_vmanage.APPROUTE_DEFS))

print("\nLuồng kịch bản 4 đạt.")
