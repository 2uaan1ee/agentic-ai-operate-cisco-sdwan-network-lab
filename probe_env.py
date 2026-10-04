"""
probe_env.py — Thăm dò sandbox THẬT trước khi viết kịch bản sinh traffic.
Chỉ ĐỌC, không đổi cấu hình nào. Chạy khi VPN đang bật:

    python probe_env.py              # chạy cả 2 phần
    python probe_env.py --no-ssh     # chỉ thăm dò vManage API
    python probe_env.py --no-api     # chỉ thăm dò các host (trex/server)

Phần A (vManage API): gọi các endpoint chỉ-đọc mà kịch bản 3 và 4 sẽ cần, in ra
tên trường và 1 dòng mẫu. Mục đích: đối chiếu tên trường thật với code.
Phần B (SSH): thử đăng nhập vào 3 máy trex qua mạng quản lý, xem trên đó có
công cụ gì (iperf3, ping, TRex...) và ping thử sang server của site khác.

Kết quả ghi thêm vào probe_output.json. File này chứa IP/hostname/tên trường của
sandbox, không chứa mật khẩu. Copy toàn bộ phần in ra màn hình gửi lại cho tôi.
"""

import os
import sys
import json

from dotenv import load_dotenv

load_dotenv()

# --- Phần B: các host cần thăm dò (IP lấy từ sơ đồ sandbox) -------------------
TREX_HOSTS = {
    "site1-trex01": "10.10.20.184",
    "site2-trex01": "10.10.20.185",
    "site3-trex01": "10.10.20.186",
}
# Server ở LAN từng site (sơ đồ), dùng làm đích ping chéo site.
PING_TARGETS = {
    "site1-trex01": [("site2-server01", "10.10.22.11"), ("site3-server01", "10.10.24.11")],
    "site2-trex01": [("site1-server01", "10.10.21.11"), ("site3-server01", "10.10.24.11")],
    "site3-trex01": [("site1-server01", "10.10.21.11"), ("site2-server01", "10.10.22.11")],
}
# Tài khoản thử, theo thứ tự. Chỉ có 3 cặp, sửa/thêm tại đây nếu bạn biết cặp khác.
# Sơ đồ ghi cisco/cisco cho server và desktop; hai cặp còn lại lấy từ bảng
# Credentials của sandbox (mật khẩu đọc từ VMANAGE_PASSWORD trong .env).
_SANDBOX_PW = os.getenv("VMANAGE_PASSWORD", "")
CREDS = [("cisco", "cisco"), ("developer", _SANDBOX_PW), ("admin", _SANDBOX_PW)]

TOOLS = ["ping", "iperf3", "iperf", "curl", "hping3", "nping", "t-rex-64", "python3"]

# --- Phần A: endpoint vManage chỉ-đọc ---------------------------------------
API_GLOBAL = [
    "/dataservice/template/policy/vsmart",
    "/dataservice/template/policy/definition/approute",
    "/dataservice/template/policy/list/sla",
    "/dataservice/template/policy/list/dataprefix",
    "/dataservice/template/policy/list/site",
    "/dataservice/template/policy/list/vpn",
]
API_PER_DEVICE = [
    "/dataservice/device/app-route/statistics",
    "/dataservice/device/bfd/sessions",
    "/dataservice/device/control/connections",
]


def _short(obj, limit=1200):
    s = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return s if len(s) <= limit else s[:limit] + "\n... (cắt bớt)"


def _rows_of(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        d = res.get("data", res)
        return d if isinstance(d, list) else [d]
    return [res]


def probe_api(results: dict) -> None:
    from vmanage_client import vm_get, VManageError

    print("=" * 70)
    print("PHẦN A: vManage API (chỉ đọc)")
    print("=" * 70)
    api_out = results.setdefault("api", {})

    raw = {}  # path -> rows (hoặc path?deviceId -> rows), dùng cho phần tóm tắt A2

    def call(path, params=None, limit=1200):
        label = path + (f"?deviceId={params['deviceId']}" if params else "")
        try:
            rows = _rows_of(vm_get(path, params))
        except VManageError as e:
            print(f"\n[{label}]\n  LỖI: {str(e)[:200]}")
            api_out[label] = {"error": str(e)[:300]}
            return
        raw[label] = rows
        keys = sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        print(f"\n[{label}]\n  {len(rows)} dòng. Trường: {', '.join(keys) if keys else '(không có)'}")
        sample = rows[0] if rows else None
        if sample is not None:
            print("  Dòng mẫu:\n" + "\n".join("    " + ln for ln in _short(sample, limit).splitlines()))
        api_out[label] = {"count": len(rows), "keys": keys, "sample": sample}

    try:
        devices = _rows_of(vm_get("/dataservice/device"))
    except VManageError as e:
        print(f"Không đọc được danh sách thiết bị: {e}")
        api_out["devices_error"] = str(e)
        return
    ctrl = {"vmanage", "vsmart", "vbond"}
    edges = [d for d in devices if str(d.get("device-type", "")).lower() not in ctrl]
    print(f"Đọc được {len(devices)} thiết bị, trong đó {len(edges)} edge.")
    api_out["edge_names"] = [d.get("host-name") for d in edges]

    for path in API_GLOBAL:
        call(path)
    # Hai edge đủ để thấy hình dạng dữ liệu: DC (hub) và 1 branch.
    pick = [d for d in edges if "dc" in str(d.get("host-name", "")).lower()][:1]
    pick += [d for d in edges if d not in pick][:1]
    for d in pick:
        ip = d.get("deviceId") or d.get("system-ip")
        print(f"\n--- Edge {d.get('host-name')} ({ip}) ---")
        for path in API_PER_DEVICE:
            call(path, {"deviceId": ip})
    summarize(raw, api_out)
    probe_details(raw, devices, edges, call)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def summarize(raw: dict, api_out: dict) -> None:
    """Phần A2: bảng gọn để đọc nhanh trạng thái policy và chất lượng tunnel."""
    print("\n" + "=" * 70)
    print("PHẦN A2: TÓM TẮT")
    print("=" * 70)
    summ = api_out.setdefault("summary", {})

    # SLA class
    sla = raw.get("/dataservice/template/policy/list/sla")
    if sla is not None:
        print("\nSLA class (name | latency ms | loss % | jitter ms | owner | refs):")
        rows = []
        for r in sla:
            e = (r.get("entries") or [{}])[0]
            rows.append([r.get("name"), e.get("latency"), e.get("loss"), e.get("jitter"),
                         r.get("owner"), r.get("referenceCount")])
            print("  " + " | ".join(str(x) for x in rows[-1]))
        summ["sla"] = rows

    # Site / VPN list
    for key, title in (("site", "Site list"), ("vpn", "VPN list")):
        lst = raw.get(f"/dataservice/template/policy/list/{key}")
        if lst is None:
            continue
        print(f"\n{title} (name | entries | refs | owner):")
        rows = []
        for r in lst:
            ents = [str(next(iter(e.values()), "")) if isinstance(e, dict) else str(e)
                    for e in (r.get("entries") or [])]
            rows.append([r.get("name"), ",".join(ents)[:60], r.get("referenceCount"), r.get("owner")])
            print("  " + " | ".join(str(x) for x in rows[-1]))
        if not lst:
            print("  (trống)")
        summ[key + "_lists"] = rows

    # Centralized policy
    pol = raw.get("/dataservice/template/policy/vsmart")
    if pol is not None:
        print("\nCentralized policy (name | ĐANG ACTIVATE? | loại phần tử trong policy):")
        rows = []
        for r in pol:
            pdef = _as_json(r.get("policyDefinition")) or {}
            types = [str(a.get("type")) for a in (pdef.get("assembly") or []) if isinstance(a, dict)]
            rows.append([r.get("policyName"), r.get("isPolicyActivated"), ",".join(types)])
            print(f"  {r.get('policyName')} | activated={r.get('isPolicyActivated')} | {','.join(types) or '(không đọc được assembly)'}")
        if not pol:
            print("  (chưa có policy nào: kích hoạt policy đầu tiên sẽ là thao tác lần đầu)")
        summ["centralized_policies"] = rows

    # App-route definition
    ar = raw.get("/dataservice/template/policy/definition/approute")
    if ar is not None:
        print("\nApp-route policy definition (name | refs | activated):")
        rows = []
        for r in ar:
            rows.append([r.get("name"), r.get("referenceCount"), r.get("isActivatedByVsmart")])
            print("  " + " | ".join(str(x) for x in rows[-1]))
        if not ar:
            print("  (chưa có)")
        summ["approute_defs"] = rows

    # Chất lượng tunnel theo cặp màu, gộp từ app-route statistics của các edge đã thăm dò
    agg = {}
    for label, rows in raw.items():
        if "app-route/statistics" not in label:
            continue
        for r in rows:
            k = (r.get("local-color"), r.get("remote-color"))
            lat = _num(r.get("average-latency", r.get("mean-latency")))
            los = _num(r.get("loss", r.get("mean-loss")))
            jit = _num(r.get("average-jitter", r.get("mean-jitter")))
            a = agg.setdefault(k, {"n": 0, "lat": [], "loss": [], "jit": []})
            a["n"] += 1
            for name, v in (("lat", lat), ("loss", los), ("jit", jit)):
                if v is not None:
                    a[name].append(v)
    if agg:
        print("\nChất lượng tunnel theo cặp màu (local -> remote | số mẫu | latency tb/max ms | loss tb/max % | jitter tb/max ms):")
        out = []
        for (lc, rc), a in sorted(agg.items(), key=lambda kv: str(kv[0])):
            def f(xs):
                return f"{sum(xs) / len(xs):.1f}/{max(xs):.1f}" if xs else "n/a"
            line = f"  {lc} -> {rc} | {a['n']} | {f(a['lat'])} | {f(a['loss'])} | {f(a['jit'])}"
            print(line)
            out.append(line.strip())
        summ["tunnel_quality"] = out


def probe_details(raw, devices, edges, call) -> None:
    """Phần A3: chi tiết cần để dựng payload tạo policy đúng với phiên bản vManage này."""
    print("\n" + "=" * 70)
    print("PHẦN A3: CHI TIẾT (để dựng payload policy cho kịch bản 4)")
    print("=" * 70)

    # Controller: vSmart phải ở chế độ vManage (gắn template) thì mới activate được policy.
    print("\nController (hostname | type | trường liên quan chế độ cấu hình):")
    for d in devices:
        if str(d.get("device-type", "")).lower() in ("vsmart", "vmanage", "vbond"):
            keys = ("configOperationMode", "template", "personality", "reachability", "status",
                    "version", "uuid")
            print("  " + str(d.get("host-name")) + " | " + str(d.get("device-type")) + " | "
                  + ", ".join(f"{k}={d.get(k)}" for k in keys if k in d))

    # Định nghĩa app-route sẵn có: mẫu hình dạng payload (sequences/match/actions)
    for r in raw.get("/dataservice/template/policy/definition/approute", []):
        if r.get("definitionId"):
            call("/dataservice/template/policy/definition/approute/" + r["definitionId"], limit=4000)

    # Policy trung tâm sẵn có: hình dạng đầy đủ khi đọc theo id
    for r in raw.get("/dataservice/template/policy/vsmart", []):
        if r.get("policyId"):
            call("/dataservice/template/policy/vsmart/" + r["policyId"], limit=4000)

    # Bộ đếm gói theo tunnel: dùng để chứng minh traffic đi đường nào trước/sau policy
    dc = [d for d in edges if "dc" in str(d.get("host-name", "")).lower()][:1]
    for d in dc:
        ip = d.get("deviceId") or d.get("system-ip")
        call("/dataservice/device/tunnel/statistics", {"deviceId": ip}, limit=2500)
        # Sandbox có script tự sinh traffic (DNS tới Netflix, Outlook...) trên server01.
        # Thử vài endpoint DPI để biết có thấy traffic đó theo ứng dụng/flow không.
        # Endpoint nào không tồn tại thì chỉ báo LỖI, không ảnh hưởng gì.
        for extra in ("/dataservice/device/dpi/application-aggregation",
                      "/dataservice/device/dpi/summary",
                      "/dataservice/device/app-route/sla-class"):
            call(extra, {"deviceId": ip}, limit=1500)


def _ssh_run(client, cmd, timeout=15):
    _, out, err = client.exec_command(cmd, timeout=timeout)
    return (out.read().decode(errors="replace") + err.read().decode(errors="replace")).strip()


def probe_ssh(results: dict) -> None:
    try:
        import paramiko
    except ImportError:
        print("Thiếu paramiko. Chạy: pip install paramiko")
        return

    print("\n" + "=" * 70)
    print("PHẦN B: các máy trex (SSH qua mạng quản lý 10.10.20.0/24)")
    print("=" * 70)
    ssh_out = results.setdefault("ssh", {})

    for name, ip in TREX_HOSTS.items():
        print(f"\n[{name}  {ip}]")
        entry = ssh_out.setdefault(name, {"host": ip})
        client = None
        for user, pw in CREDS:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                c.connect(ip, username=user, password=pw, timeout=8, banner_timeout=8,
                          auth_timeout=8, allow_agent=False, look_for_keys=False)
                client = c
                entry["login_user"] = user
                print(f"  Đăng nhập OK với user '{user}'")
                break
            except paramiko.AuthenticationException:
                print(f"  user '{user}': sai mật khẩu")
                c.close()
            except Exception as e:
                # Lỗi mạng/timeout: thử cặp khác cũng vô ích, dừng luôn host này.
                print(f"  KHÔNG KẾT NỐI ĐƯỢC ({type(e).__name__}: {str(e)[:100]})")
                entry["error"] = f"{type(e).__name__}: {str(e)[:100]}"
                c.close()
                break
        if client is None:
            entry.setdefault("error", "không đăng nhập được với các cặp đã thử")
            continue

        try:
            info = _ssh_run(client, "hostname; id -un; uname -sr; ip -br -4 addr 2>&1 | head -8")
            print("  " + info.replace("\n", "\n  "))
            entry["info"] = info
            found = _ssh_run(client, "for t in " + " ".join(TOOLS) + "; do p=$(command -v $t) && echo \"$t=$p\" || echo \"$t=(không có)\"; done")
            print("  Công cụ:\n    " + found.replace("\n", "\n    "))
            entry["tools"] = found
            extra = _ssh_run(client, "ls -d /opt/trex* /home/*/trex* /root/trex* 2>/dev/null | head -5; sudo -n true 2>&1 | head -1")
            print("  Thư mục TRex / sudo:\n    " + (extra.replace("\n", "\n    ") or "(không thấy)"))
            entry["trex_dirs_sudo"] = extra
            entry["ping"] = {}
            for tname, tip in PING_TARGETS[name]:
                out = _ssh_run(client, f"ping -I eth1 -c 3 -W 2 {tip} 2>&1 | tail -3", timeout=20)
                print(f"  ping -I eth1 {tname} ({tip}):\n    " + out.replace("\n", "\n    "))
                entry["ping"][tname] = out
        except Exception as e:
            print(f"  Lỗi khi chạy lệnh: {type(e).__name__}: {e}")
            entry["error"] = f"{type(e).__name__}: {e}"
        finally:
            client.close()


def main() -> None:
    results = {}
    if "--no-api" not in sys.argv:
        probe_api(results)
    if "--no-ssh" not in sys.argv:
        probe_ssh(results)
    try:
        with open("probe_output.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print("\nĐã lưu probe_output.json")
    except Exception as e:
        print(f"Không lưu được probe_output.json: {e}")


if __name__ == "__main__":
    main()