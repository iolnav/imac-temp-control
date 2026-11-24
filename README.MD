# iMac Temp Control (`imac-temp-control`)

Công cụ theo dõi nhiệt độ **raw** (cảm biến gốc từ AppleSMC / VirtualSMC), chạy **stress test kết hợp CPU + GPU + VRAM (Metal)** và xuất báo cáo biểu đồ trực quan cho iMac Intel (OpenCore / macOS).

A lightweight macOS tool for reading **raw** AppleSMC / VirtualSMC temperatures, running a combined **CPU + GPU + VRAM (Metal) stress test**, and generating visual thermal reports for Intel iMacs (OpenCore / macOS).

---

## Tính năng chính / Key Features

- **Đọc trực tiếp AppleSMC bằng `ctypes`** (không cần `brew` hay công cụ bên ngoài):
  - **CPU Core 0 → 3 (raw):** `TC0c`, `TC1c`, `TC2c`, `TC3c`
  - **GPU (raw):** `TG0d` (GPU Die raw), `TG0p` (GPU Proximity raw), `TG1p` (GPU A VRAM Proximity raw), `TG2p` (GPU VR Proximity raw)
  - **Tốc độ quạt:** `F0Ac` (RPM)
- **Stress test kết hợp CPU + GPU + VRAM (`stress_tool.swift`):**
  - Tự động biên dịch từ `stress_tool.swift` bằng `swiftc` tích hợp sẵn trên macOS.
  - **CPU:** ép tải đa luồng với các phép toán dấu phẩy động nặng (`fma`, `sin`, `cos`, `sqrt`).
  - **GPU + VRAM:** sử dụng Metal Compute Shader cấp phát `.storageModePrivate` trực tiếp trong VRAM rời (giới hạn an toàn tối đa 75% dung lượng khuyến nghị), đọc/ghi xoay vòng toàn bộ VRAM kèm tính toán cường độ cao.
- **Ghi log 3 pha (`idle` → `load` → `cool`)** ra tệp CSV và **xuất báo cáo PNG + HTML** (hỗ trợ so sánh nhiều lần đo, ví dụ trước và sau khi thay keo tản nhiệt).

---

- **Direct AppleSMC reading via `ctypes`** (no `brew` or external dependencies required):
  - **CPU Cores 0–3 (raw):** `TC0c`, `TC1c`, `TC2c`, `TC3c`
  - **GPU (raw):** `TG0d` (GPU Die raw), `TG0p` (GPU Proximity raw), `TG1p` (GPU A VRAM Proximity raw), `TG2p` (GPU VR Proximity raw)
  - **Fan speed:** `F0Ac` (RPM)
- **Combined CPU + GPU + VRAM Stress Test (`stress_tool.swift`):**
  - Automatically compiled from `stress_tool.swift` using macOS's built-in `swiftc`.
  - **CPU:** multi-threaded heavy floating-point workload (`fma`, `sin`, `cos`, `sqrt`).
  - **GPU + VRAM:** Metal Compute Shader allocating `.storageModePrivate` buffers directly in discrete VRAM (safely capped at 75% of `recommendedMaxWorkingSetSize`), continuously cycling reads/writes across VRAM with heavy FMA compute.
- **3-phase logging (`idle` → `load` → `cool`)** to CSV and **PNG + HTML report generation** (supports comparing multiple runs, e.g., before and after thermal paste replacement).

---

## Yêu cầu hệ thống / Requirements

- macOS trên iMac Intel (hỗ trợ `AppleSMC` / `VirtualSMC`)
- Python 3
- Swift compiler (`swiftc` từ Xcode Command Line Tools — dùng để biên dịch `stress_tool.swift`)
- `matplotlib` (chỉ cần khi chạy lệnh `report` / only required for `report`):

```bash
python3 -m pip install matplotlib
```

---

## Cách sử dụng / Usage

### 1. Liệt kê tất cả cảm biến nhiệt độ SMC / List all SMC temperature sensors

```bash
python3 imac_thermal.py list
```

### 2. Chạy stress test kết hợp CPU + GPU + VRAM / Run combined CPU + GPU + VRAM stress test

Chạy trong 300 giây với 4096 MB VRAM và in nhiệt độ raw theo thời gian thực:

Run for 300 seconds with 4096 MB VRAM and print live raw temperatures:

```bash
python3 imac_thermal.py stress --duration 300 --vram-mb 4096
```

*(Đặt `--duration 0` để chạy liên tục cho đến khi nhấn `Ctrl-C`. / Use `--duration 0` to run until `Ctrl-C`.)*

### 3. Ghi nhật ký nhiệt độ 3 pha / Log 3-phase thermal data to CSV

```bash
python3 imac_thermal.py log --idle 300 --load 600 --cool 300 --vram-mb 4096 --out sau_thay_keo.csv
```

Tùy chọn bổ sung / Additional options:
- `--no-cpu-stress`: tắt stress CPU / disable CPU stress
- `--no-gpu-stress`: tắt stress GPU & VRAM / disable GPU & VRAM stress
- `--sensors "GPU_VRAM_Raw=TG1p,GPU_Die_Raw=TG0d"`: ghi đè khóa SMC / override SMC keys

### 4. Xuất báo cáo biểu đồ PNG & HTML / Generate PNG & HTML report

Một tệp CSV / Single CSV:

```bash
python3 imac_thermal.py report sau_thay_keo.csv
```

So sánh nhiều tệp CSV / Compare multiple CSVs:

```bash
python3 imac_thermal.py report truoc.csv sau.csv
```

---

## Cấu trúc mã nguồn / Repository Structure

- `imac_thermal.py`: Chương trình chính đọc SMC, điều phối stress test, ghi CSV và xuất báo cáo.
- `stress_tool.swift`: Công cụ native (Swift + Metal) tạo tải nặng cho CPU, GPU và VRAM.
- `.gitignore`: Loại bỏ các tệp báo cáo (`.csv`, `.html`, `.png`), tệp thực thi biên dịch (`stress_tool`) và tệp rác hệ thống.
