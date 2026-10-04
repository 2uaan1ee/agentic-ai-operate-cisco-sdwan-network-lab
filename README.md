Project này dùng CrewAI để điều khiển môi trường Cisco SD-WAN sandbox qua vManage REST API. Code chạy trên máy local của bạn, máy phải đang nối VPN tới sandbox. Có hai kịch bản demo: kịch bản 1 chỉ đọc (kiểm tra sức khoẻ overlay), kịch bản 2 vừa đọc vừa ghi (change request cho Data Prefix List).

Cấu trúc code bám theo repo `2uaan1ee/agentic_ai_for_network_enterprise`: tool viết thành hàm thường rồi bọc `@tool` mỏng bên ngoài, toàn bộ agent và crew nằm trong một file, một file `main` tương tác kèm ghi log, và mọi lớp an toàn đặt ở tầng code thay vì nhờ LLM tự giữ kỷ luật. Điểm khác chính: repo mẫu nói chuyện với switch qua SSH (Netmiko), project này nói chuyện với vManage qua REST (requests).

## Cài đặt và chạy

1. Bật VPN tới sandbox (hướng dẫn nằm ở tab "VPN Access" của trang sandbox). Kiểm tra bằng cách mở `https://10.10.20.90` trên trình duyệt. Sau khi reservation bắt đầu, vManage cần khoảng 10 phút để các edge đăng ký xong, chạy sớm hơn có thể thấy edge unreachable.
2. Tạo môi trường Python (đã thử với Python 3.13, CrewAI yêu cầu từ 3.10 trở lên):
   ```bash
   python -m venv venv
   source venv/bin/activate        # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Tạo file cấu hình rồi điền API key LLM và mật khẩu vManage (lấy ở trang Credentials của sandbox):
   ```bash
   cp .env.example .env
   ```
4. Kiểm tra VPN và tài khoản, bước này không dùng LLM nên không tốn token:
   ```bash
   python main_sdwan.py --check
   ```
   Kết quả đúng là danh sách 7 thiết bị (vManage, vSmart, vBond và 4 edge) kèm trạng thái reachable.
5. Lần chạy đầu nên đặt `SDWAN_READ_ONLY=true` trong `.env`, chạy kịch bản 1 và phần đọc của kịch bản 2. Khi đã yên tâm, đổi sang `false` để thử ghi.
6. Chạy chương trình:
   ```bash
   python main_sdwan.py
   ```

## Các file và vai trò

| File | Vai trò | Tương ứng trong repo mẫu |
|---|---|---|
| `vmanage_client.py` | Đăng nhập vManage, giữ session, gửi GET và lệnh ghi | `_run_raw` trong `network_tools.py` |
| `sdwan_read_tools.py` | Tool đọc: danh sách thiết bị, health của edge, prefix list. Tính status trong code | `network_tools.py`, `network_monitoring_tools.py` |
| `sdwan_write_tools.py` | Hàm ghi duy nhất (Data Prefix List) cùng toàn bộ kiểm soát | `network_write_tools.py` |
| `crew_sdwan.py` | Cấu hình LLM, 3 agent, task và crew cho 2 kịch bản | `crew_network.py` |
| `main_sdwan.py` | Menu tương tác, luồng ticket, ghi log, hoàn tác | `main_network.py` |
| `tests/` | vManage giả lập và hai file test chạy được không cần VPN | không có |

## Cấu hình từng phần

### File `.env`

| Biến | Ý nghĩa |
|---|---|
| `LLM_PROVIDER`, `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Chọn nhà cung cấp LLM và điền đúng key tương ứng |
| `MODEL_NAME` | Để trống thì dùng model rẻ mặc định (`claude-haiku-4-5-20251001` hoặc `gpt-5-mini`). Nếu agent hay gọi sai tool, đổi sang model mạnh hơn |
| `OPENAI_BASE_URL` | Chỉ dùng khi trỏ sang endpoint tương thích OpenAI, khi đó bắt buộc điền `MODEL_NAME` |
| `REPORT_LANGUAGE` | Ngôn ngữ của báo cáo cuối, mặc định Vietnamese. Prompt của agent vẫn viết tiếng Anh vì model theo prompt tiếng Anh ổn định hơn |
| `VMANAGE_HOST`, `VMANAGE_PORT` | Sandbox là `10.10.20.90` và `443` |
| `VMANAGE_USERNAME`, `VMANAGE_PASSWORD` | Tài khoản `admin` của vManage |
| `SDWAN_VERIFY_SSL` | Để `false` vì sandbox dùng chứng chỉ tự ký |
| `SDWAN_READ_ONLY` | `true` chặn mọi lệnh ghi. Đây là công tắc an toàn nên bật khi mới thử |

Credentials của vBond, vSmart, devbox và các router trong bảng bạn dán không được dùng ở đây, vì mọi thao tác đi qua vManage. Cách này đúng với kiến trúc SD-WAN: cEdge và vEdge được quản lý bằng template từ vManage, nếu sửa trực tiếp bằng CLI thì cấu hình bị chặn hoặc bị vManage ghi đè.

### Lớp kết nối: `vmanage_client.py`

vManage yêu cầu ba bước trước khi gọi API: gửi `POST /j_security_check` với `j_username` và `j_password` để lấy cookie `JSESSIONID`, rồi `GET /dataservice/client/token` để lấy XSRF token, sau đó mọi request POST, PUT, DELETE phải kèm header `X-XSRF-TOKEN`. File này làm cả ba bước, giữ session dùng lại giữa các lần gọi, và tự đăng nhập lại một lần khi session hết hạn.

Đăng nhập sai là trường hợp dễ lỡ: vManage vẫn trả HTTP 200 kèm trang HTML đăng nhập, không trả 401. Code nhận ra bằng cách kiểm tra cookie và nội dung trang, rồi báo lỗi rõ ràng.

Hàm đọc (`vm_get`, `vm_get_data`) và hàm ghi (`vm_write`) tách riêng. Tool đọc chỉ import hàm đọc nên về cấu trúc không có đường nào từ tool đọc tới lệnh ghi. Mọi đường dẫn phải bắt đầu bằng `/dataservice/`. Mỗi request được ghi vào `tool_calls.log` (không ghi mật khẩu hay token).

### Tool đọc: `sdwan_read_tools.py`

Có bốn tool: liệt kê thiết bị, kiểm tra một edge, quét toàn bộ edge, liệt kê Data Prefix List. Đúng triết lý của repo mẫu, số liệu và trạng thái do code tính. LLM không tự đếm BFD session và không tự phán đoán bao nhiêu là bất thường, nó chỉ đọc trường `status` đã có sẵn.

Quy tắc tính status cho mỗi edge:

| Phần | critical | warning | ok |
|---|---|---|---|
| Reachability | khác `reachable` | không có | `reachable` |
| Control connection tới vSmart và vManage | không có kết nối nào up | có kết nối down | tất cả up |
| BFD session | không có session nào up | có session down | tất cả up |

Kết nối tới vBond bị bỏ qua có chủ đích. Edge chỉ nói chuyện với vBond thoáng qua lúc khởi động để tìm vSmart, nên state không phải `up` ở peer này là bình thường và sẽ gây cảnh báo giả. Trạng thái tổng của edge là mức xấu nhất trong ba phần. Ngưỡng nằm ở đầu file nếu bạn muốn đổi.

Tên thiết bị chấp nhận hostname hoặc system-ip, và chấp nhận gõ tắt như `site1` nếu chỉ khớp đúng một thiết bị.

### Tool ghi: `sdwan_write_tools.py`

Kịch bản ghi là quản lý Data Prefix List (Configuration > Policies > Lists). Lý do chọn: list chưa được policy nào tham chiếu thì không tác động tới data plane và không kích hoạt push policy xuống vSmart, nên bạn có ghi thật mà không làm hỏng overlay của sandbox. Payload cũng đơn giản (tên, loại `dataPrefix`, danh sách `ipPrefix`) và hoàn tác dễ.

Hàm `change_prefix_list_text(action, list_name, prefixes)` nhận bốn action: `create`, `add`, `remove`, `delete`. Không có tool nào nhận JSON hay URL tự do từ agent. URL và payload do code dựng theo khuôn cố định, agent chỉ điền ba tham số trên. Các lớp kiểm soát nằm ở phần "Lớp an toàn" bên dưới.

### Agent và crew: `crew_sdwan.py`

| Agent | Quyền | Tool | Việc làm |
|---|---|---|---|
| `overlay_analyst_agent` | Đọc | liệt kê thiết bị, health một edge, health mọi edge | Chọn tool phù hợp với câu hỏi, thu thập dữ liệu (kịch bản 1) |
| `change_planner_agent` | Đọc | liệt kê prefix list | Đọc ticket tiếng Việt, trả về đối tượng `ChangeRequest` có cấu trúc (kịch bản 2) |
| `report_agent` | Không có tool | không | Viết báo cáo từ dữ liệu đã có, dùng chung cho cả hai kịch bản |

Không agent nào cầm tool ghi. Lý do giống repo mẫu: CrewAI chạy tool trong thread phụ dưới khung log Rich, nên câu hỏi `input()` xác nhận có thể bị che hoặc treo, và agent hay đi đường vòng. Vì vậy `change_planner_agent` chỉ dịch ticket thành tham số (`output_pydantic=ChangeRequest`, trường `action` chỉ nhận bốn giá trị hợp lệ), còn `main_sdwan.py` tự gọi hàm ghi trên luồng chính.

### Chương trình chính: `main_sdwan.py`

Menu có ba lựa chọn: kịch bản 1, kịch bản 2, và `9` để kiểm tra kết nối. Mỗi lượt được ghi vào `qa_log.md` và vào `output/lan_N.md` (mỗi lần chạy chương trình một file, N tăng dần, không ghi đè file cũ), kèm output thô của từng bước để đưa vào báo cáo. Lỗi giữa chừng như API rate limit hay vManage không phản hồi chỉ in ra rồi quay lại menu.

## Hai kịch bản demo

### Kịch bản 1: kiểm tra sức khoẻ overlay (chỉ đọc)

Chọn `1` rồi hỏi bằng tiếng Việt, ví dụ:

- `overlay hiện tại có ổn không?`
- `site1 có tunnel nào bị down không?`
- `dc-cedge01 đang có bao nhiêu BFD session up?`

Với câu hỏi chung, analyst gọi tool quét mọi edge (mỗi edge hai lần gọi API: control connection và BFD). Với câu hỏi về một site, nó gọi tool một edge. Report agent đọc status và đưa ra việc cần kiểm tra tiếp nếu có chỗ warning hoặc critical, chẳng hạn màu transport nào đang down.

Gợi ý để demo có nội dung: theo trang sandbox, WAN emulator chèn 200 ms RTT và 0,05 loss ở link giữa Internet và DC, nên chất lượng tunnel qua Internet có thể khác tunnel qua MPLS. Tool hiện tại chỉ đếm session up hay down, chưa đọc latency và loss. Nếu muốn so sánh chất lượng, tra trong tài liệu API của vManage endpoint thống kê app-route rồi thêm một tool đọc mới.

### Kịch bản 2: change request Data Prefix List (đọc rồi ghi)

Chọn `2`, chương trình in các list hiện có rồi nhận ticket, ví dụ:

- `tạo list AI_BRANCH_LAN gồm 10.10.21.0/24 và 10.10.22.0/24`
- `thêm 10.10.24.0/24 vào AI_BRANCH_LAN`
- `gỡ 10.10.22.0/24 khỏi AI_BRANCH_LAN`
- `xoá list AI_BRANCH_LAN`

Các dải 10.10.21.0/24, 10.10.22.0/24 và 10.10.24.0/24 là LAN của ba branch trong sơ đồ sandbox, nên ví dụ có ý nghĩa thực tế: đây là kiểu list mà policy sau này sẽ tham chiếu.

Luồng xử lý một ticket:

1. `change_planner_agent` đọc ticket, gọi tool xem list đã tồn tại chưa nếu ticket không nói rõ, rồi trả về `ChangeRequest` (đọc).
2. Code đọc trạng thái hiện tại của list trên vManage, kiểm tra các điều kiện an toàn, tính ra nội dung list sau thay đổi (đọc).
3. Code in bản tóm tắt gồm nội dung trước, nội dung sau, URL và phương thức sẽ gọi, rồi chờ bạn gõ `yes` (xác nhận).
4. Code gửi POST, PUT hoặc DELETE (ghi).
5. Code đọc lại list từ vManage và so với mong đợi, kết quả là `applied` hoặc `applied_but_not_verified` (verify).
6. `report_agent` viết báo cáo từ JSON kết quả.
7. Chương trình hỏi có hoàn tác không. Hoàn tác đi qua đúng hàm ghi nên vẫn qua đủ kiểm tra và hỏi xác nhận lần nữa.

Mọi thay đổi đã áp dụng còn được ghi vào `change_audit.log` (mỗi dòng một JSON, gồm trạng thái trước và sau).

Đây là kịch bản có đủ vòng đọc, quyết định, ghi, verify, hoàn tác. Khi demo, nên thử thêm vài ticket cố tình sai để cho thấy các lớp chặn hoạt động: tên list không có `AI_`, prefix `10.10.20.0/24`, thao tác lên list `CORP_LAN` đang được policy dùng.

## Lớp an toàn

Tất cả nằm trong code, không phụ thuộc việc LLM cư xử đúng.

| Lớp | Hành vi |
|---|---|
| Công tắc `SDWAN_READ_ONLY` | Chặn trước cả bước hỏi xác nhận, và `vm_write` kiểm tra lại lần nữa |
| Chỉ bốn action, chỉ loại `dataPrefix` | Không có đường nào tạo policy, template hay đụng thiết bị |
| Tên list phải khớp `^AI_[A-Za-z0-9_-]{1,28}$` | List do lab hoặc người khác tạo không sửa được |
| List có `referenceCount > 0` hoặc `readOnly` bị chặn | Không sửa list mà policy đang chạy dùng |
| Validate prefix | Chỉ IPv4 CIDR hợp lệ, từ /8 đến /32, không chồng lên dải quản trị `10.10.20.0/24`, tối đa 50 prefix |
| Đọc trạng thái trước khi ghi | Đã đúng rồi thì trả `already_configured`, không gửi gì |
| Không cho list rỗng | Gỡ hết prefix bị chặn, phải dùng `delete` |
| Cổng xác nhận con người | In tóm tắt, chờ `yes`. Xoá list phải gõ lại tên list. Không có console tương tác thì mặc định huỷ |
| Verify sau khi ghi | Đọc lại từ vManage, không chỉ tin request thành công |
| Audit log | `change_audit.log` và `tool_calls.log` |

Prefix gõ dạng `10.10.21.5/24` được chuẩn hoá thành `10.10.21.0/24` và bản tóm tắt hiển thị giá trị đã chuẩn hoá.

## Kiểm thử không cần VPN

`tests/mock_vmanage.py` là vManage giả chạy trên localhost, mô phỏng cả các hành vi dễ gây lỗi của vManage thật (đăng nhập sai vẫn trả HTTP 200 kèm HTML, POST thiếu XSRF token bị 403). Dữ liệu thiết bị trong đó là dữ liệu mẫu tự đặt, không phải dữ liệu thật của sandbox.

```bash
python tests/test_tools.py        # 44 kiểm tra: tool đọc, mọi lớp chặn của tool ghi, luồng create/add/remove/delete/rollback
python tests/test_crew_build.py   # dựng agent, task, crew, kiểm tra không agent nào có tool ghi
```

Cả hai file chạy không cần API key thật và không cần VPN.

## Những điều chưa kiểm chứng

- Code chưa chạy trên sandbox thật, vì môi trường tôi viết code không có VPN. Luồng đăng nhập và endpoint ghi dựa trên tài liệu của Cisco DevNet và SDK `python-viptela`, phần còn lại dựa trên cấu trúc response mà tôi biết của vManage. Tên trường (`host-name`, `peer-type`, `state`, `referenceCount`...) có thể lệch nhẹ theo phiên bản vManage của sandbox. Nếu `--check` hoặc kịch bản 1 báo thiếu dữ liệu, mở `tool_calls.log` và gọi thử endpoint bằng Postman để so tên trường, sau đó sửa ở `_normalize_device`, `_control_summary`, `_bfd_summary`.
- Chưa chạy end-to-end với LLM thật (không có API key trong môi trường viết code). Phần dựng crew, tool wrapper và schema đã được kiểm tra, nhưng chất lượng việc agent chọn tool và tách ticket cần bạn thử. Nếu agent tách sai, đổi `MODEL_NAME` sang model mạnh hơn.
- Giới hạn độ dài tên list (31 ký tự) là con số bảo thủ tôi chọn. Nếu vManage của sandbox từ chối tên, rút ngắn tên.
- Trang sandbox ghi dc-wan-edge01 là `10.10.20.171` trong sơ đồ nhưng `10.10.20.173` trong bảng credentials. Project này không dùng IP đó nên không bị ảnh hưởng.
- Với dữ liệu mock, chưa có ca edge ở trạng thái `unreachable`. Logic đã có nhưng chỉ được kiểm tra qua đường `warning`.

## Hướng mở rộng

- Thêm tool đọc `show` trên devbox hoặc thiết bị qua SSH để đối chiếu với dữ liệu vManage.
- Thêm tool đọc latency và loss theo từng tunnel, để agent so sánh MPLS với Internet trên link có WAN emulator.
- Kịch bản ghi nặng hơn: sửa feature template rồi attach lại cho một edge, hoặc tạo app-route policy rồi activate. Hai thao tác này ảnh hưởng tới overlay thật, nên chỉ làm sau khi bạn đã quen với backup và rollback template.
