#!/usr/bin/env python3
"""
imac_thermal.py - Ghi nhật ký nhiệt độ raw của CPU / GPU trên iMac (Intel, OpenCore) và xuất báo cáo.
Không cần cài đặt qua brew. Đọc trực tiếp SMC (VirtualSMC / AppleSMC) bằng ctypes.

Cách dùng:
  python3 imac_thermal.py list
  python3 imac_thermal.py stress --duration 300 --vram-mb 4096
  python3 imac_thermal.py log --idle 300 --load 600 --cool 300 --out sau_thay_keo.csv
  python3 imac_thermal.py report sau_thay_keo.csv
  python3 imac_thermal.py report truoc.csv sau.csv      # so sánh (nếu có dữ liệu cũ)

Xuất báo cáo cần thư viện matplotlib:  python3 -m pip install matplotlib
"""
import argparse
import base64
import csv
import ctypes
import ctypes.util
import datetime
import io
import multiprocessing
import os
import shutil
import struct
import subprocess
import sys
import time


# ----------------------------------------------------------------- SMC ----
class Vers(ctypes.Structure):
    """Cấu trúc lưu thông tin phiên bản của khóa SMC."""

    _fields_ = [
        ("major", ctypes.c_uint8),
        ("minor", ctypes.c_uint8),
        ("build", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
        ("release", ctypes.c_uint16),
    ]


class PLimit(ctypes.Structure):
    """Cấu trúc giới hạn công suất (Power Limit) trong dữ liệu SMC."""

    _fields_ = [
        ("version", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("cpu", ctypes.c_uint32),
        ("gpu", ctypes.c_uint32),
        ("mem", ctypes.c_uint32),
    ]


class KeyInfo(ctypes.Structure):
    """Thông tin mô tả kích thước, kiểu dữ liệu và thuộc tính của một khóa SMC."""

    _fields_ = [
        ("size", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("attr", ctypes.c_uint8),
    ]


class KeyData(ctypes.Structure):
    """Cấu trúc trao đổi dữ liệu chính với AppleSMC qua IOConnectCallStructMethod."""

    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", Vers),
        ("plimit", PLimit),
        ("info", KeyInfo),
        ("result", ctypes.c_uint8),
        ("status", ctypes.c_uint8),
        ("data8", ctypes.c_uint8),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_uint8 * 32),
    ]


assert ctypes.sizeof(KeyData) == 80, "Sai kích thước cấu trúc dữ liệu SMC (phải bằng 80 byte)"


def fourcc(s):
    """Chuyển chuỗi 4 ký tự ASCII sang số nguyên big-endian 32-bit."""
    return int.from_bytes(s.encode("ascii"), "big")


def unfourcc(n):
    """Chuyển số nguyên big-endian 32-bit về chuỗi 4 ký tự ASCII."""
    return n.to_bytes(4, "big").decode("ascii", "replace")


class SMC:
    """Lớp giao tiếp trực tiếp với trình điều khiển AppleSMC qua IOKit."""

    def __init__(self):
        self.iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"))
        io_ = self.iokit
        io_.IOServiceMatching.restype = ctypes.c_void_p
        io_.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io_.IOServiceGetMatchingService.restype = ctypes.c_uint
        io_.IOServiceGetMatchingService.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        io_.IOServiceOpen.restype = ctypes.c_int
        io_.IOServiceOpen.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        io_.IOConnectCallStructMethod.restype = ctypes.c_int
        io_.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        dev = io_.IOServiceGetMatchingService(0, io_.IOServiceMatching(b"AppleSMC"))
        if not dev:
            sys.exit("Không tìm thấy AppleSMC (hãy kiểm tra VirtualSMC trong EFI).")
        task = ctypes.c_uint.in_dll(self.libc, "mach_task_self_").value
        self.conn = ctypes.c_uint(0)
        if io_.IOServiceOpen(dev, task, 0, ctypes.byref(self.conn)) != 0:
            sys.exit("Không mở được kết nối tới SMC.")

    def _call(self, inp):
        """Gửi lệnh đọc/tra cứu tới SMC và kiểm tra mã lỗi trả về."""
        out = KeyData()
        size = ctypes.c_size_t(ctypes.sizeof(KeyData))
        r = self.iokit.IOConnectCallStructMethod(
            self.conn,
            2,
            ctypes.byref(inp),
            ctypes.sizeof(KeyData),
            ctypes.byref(out),
            ctypes.byref(size),
        )
        if r != 0 or out.result != 0:
            raise KeyError("Lỗi giao tiếp SMC")
        return out

    def key_at(self, index):
        """Lấy tên khóa SMC tại vị trí chỉ mục `index`."""
        inp = KeyData()
        inp.data8, inp.data32 = 8, index
        return unfourcc(self._call(inp).key)

    def read(self, key):
        """Đọc giá trị của một khóa SMC. Trả về (giá_trị_số | None, kiểu_dữ_liệu)."""
        # Bước 1: Lấy thông tin kiểu và kích thước của khóa (data8 = 9)
        inp = KeyData()
        inp.key, inp.data8 = fourcc(key), 9
        info = self._call(inp).info

        # Bước 2: Đọc dữ liệu thô theo kích thước vừa lấy (data8 = 5)
        inp2 = KeyData()
        inp2.key, inp2.data8 = fourcc(key), 5
        inp2.info.size = info.size
        out = self._call(inp2)
        raw = bytes(out.bytes)[:info.size]
        t = unfourcc(info.type)
        try:
            if t == "sp78":
                # Số thực dấu phẩy tĩnh có dấu 7.8 bit (big-endian)
                return struct.unpack(">h", raw[:2])[0] / 256.0, t
            if t == "flt ":
                # Số thực IEEE 754 độ chính xác đơn (little-endian)
                return struct.unpack("<f", raw[:4])[0], t
            if t == "fpe2":
                # Số thực không dấu 14.2 bit (big-endian), thường dùng cho tốc độ quạt
                return struct.unpack(">H", raw[:2])[0] / 4.0, t
            if t in ("ui8 ", "ui16", "ui32"):
                return float(int.from_bytes(raw, "big")), t
        except struct.error:
            pass
        return None, t


# ------------------------------------------------- cảm biến mặc định ------
# Tên khóa SMC có thể khác nhau tùy đời máy. Dùng lệnh `list` để đối chiếu với System Toolkit Pro,
# hoặc ghi đè khi chạy `log` bằng tham số --sensors "GPU_VRAM_Raw=TG1p,GPU_Die_Raw=TG0d".
# Quy ước chữ cái cuối của Apple SMC: chữ thường (p, d, c) là giá trị raw; chữ hoa (P, D) là cooked.
DEFAULT_SENSORS = {
    "CPU_Core0": "TC0c",     # Nhiệt độ raw nhân CPU 0
    "CPU_Core1": "TC1c",     # Nhiệt độ raw nhân CPU 1
    "CPU_Core2": "TC2c",     # Nhiệt độ raw nhân CPU 2
    "CPU_Core3": "TC3c",     # Nhiệt độ raw nhân CPU 3
    "GPU_Die_Raw": "TG0d",   # Nhiệt độ raw GPU Die
    "GPU_Prox_Raw": "TG0p",  # Nhiệt độ raw GPU Proximity
    "GPU_VRAM_Raw": "TG1p",  # Nhiệt độ raw GPU A VRAM Proximity
    "GPU_VR_Raw": "TG2p",    # Nhiệt độ raw GPU VR (nguồn cấp điện) Proximity
}
FAN_KEY = "F0Ac"

# Bảng chú giải tên gọi các khóa SMC (chữ hoa P = "cooked" đã nội suy, chữ thường p/d/c = "raw" cảm biến gốc).
KEY_NAMES = {
    "TC0P": "CPU 1 Proximity (cooked)",
    "TC0p": "CPU Proximity (raw)",
    "TC0c": "CPU Core 0 (raw)",
    "TC1c": "CPU Core 1 (raw)",
    "TC2c": "CPU Core 2 (raw)",
    "TC3c": "CPU Core 3 (raw)",
    "TCXc": "PECI CPU",
    "TCSc": "PECI SA",
    "TCGc": "PECI GPU",
    "TC0J": "CPU PECI Die Max Error",
    "TG0D": "GPU 0 Die (cooked)",
    "TG0d": "GPU Die (raw)",
    "TG0E": "GPU 0 Die (điều khiển công suất)",
    "TG0F": "GPU 0 Die (điều khiển quạt)",
    "TG0J": "GPU 0 Die Max Error",
    "TG0P": "GPU Proximity (cooked)",
    "TG0p": "GPU 0 Proximity (raw)",
    "TG1P": "GPU A VRAM Proximity (cooked)",
    "TG1p": "GPU A VRAM Proximity (raw)",
    "TG2P": "GPU A VR Proximity (cooked)",
    "TG2p": "GPU VR Proximity (raw)",
}


def parse_sensors(arg):
    """Phân tích chuỗi --sensors để ghi đè danh sách cảm biến mặc định."""
    s = dict(DEFAULT_SENSORS)
    if arg:
        for part in arg.split(","):
            if "=" in part:
                name, key = part.split("=", 1)
                s[name.strip()] = key.strip()
    return s


def cmd_list(_args):
    """Liệt kê toàn bộ khóa nhiệt độ SMC (bắt đầu bằng 'T') có giá trị hợp lệ."""
    smc = SMC()
    n = int(smc.read("#KEY")[0])
    print(f"Tổng số khóa SMC: {n}. Các khóa nhiệt độ (T***) đang có giá trị hợp lệ:\n")
    for i in range(n):
        try:
            k = smc.key_at(i)
            if not k.startswith("T"):
                continue
            v, _ = smc.read(k)
        except KeyError:
            continue
        if v is not None and 1 < v < 130:
            name = KEY_NAMES.get(k, "")
            print(f"  {k}   {v:6.1f} °C   {('<- ' + name) if name else ''}")
    print(
        "\nCột '<-' là tên tham chiếu theo chuẩn SMC. Để xác minh: mở System Toolkit Pro, "
        "chạy lệnh này NGAY LÚC ĐÓ rồi đối chiếu giá trị làm tròn tại cùng một thời điểm."
    )


# --------------------------------------------------------------- stress + ghi log -----
def _burn(stop):
    """Tiến trình con dự phòng (Python) tạo tải CPU khi không có trình biên dịch Swift."""
    x = 0
    while not stop.is_set():
        for _ in range(200000):
            x = (x * 1103515245 + 12345) & 0xFFFFFFFF


def ensure_stress_tool():
    """Biên dịch stress_tool.swift sang tệp thực thi nếu chưa có hoặc mã nguồn mới hơn."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(base_dir, "stress_tool.swift")
    bin_path = os.path.join(base_dir, "stress_tool")
    if not os.path.exists(src):
        return None
    need_build = (
        not os.path.exists(bin_path)
        or os.path.getmtime(src) > os.path.getmtime(bin_path)
    )
    if need_build:
        swiftc = shutil.which("swiftc")
        if not swiftc:
            return None
        print("Đang biên dịch stress_tool.swift (Metal GPU + VRAM + CPU)...")
        res = subprocess.run(
            [swiftc, "-O", src, "-framework", "Metal", "-framework", "Foundation", "-o", bin_path],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print(f"[cảnh báo] Biên dịch stress_tool thất bại: {res.stderr.strip()}")
            return None
    return bin_path


def start_stress(cpu_threads, vram_mb):
    """Khởi chạy bài stress test kết hợp CPU + GPU + VRAM. Trả về (tiến_trình_native, danh_sách_proc_python, event_stop)."""
    if cpu_threads <= 0 and vram_mb <= 0:
        return None, [], None
    tool = ensure_stress_tool()
    if tool:
        cmd = [tool, "--cpu", str(max(0, cpu_threads)), "--vram-mb", str(max(0, vram_mb))]
        proc = subprocess.Popen(cmd)
        return proc, [], None

    # Phương án dự phòng bằng Python nếu không có stress_tool (chỉ ép tải CPU)
    if vram_mb > 0:
        print("[cảnh báo] Không tìm thấy stress_tool/swiftc nên chỉ ép tải CPU.")
    if cpu_threads > 0:
        stop = multiprocessing.Event()
        procs = [multiprocessing.Process(target=_burn, args=(stop,)) for _ in range(cpu_threads)]
        for p in procs:
            p.start()
        return None, procs, stop
    return None, [], None


def stop_stress(native_proc, procs, stop):
    """Dừng sạch toàn bộ tiến trình stress test đang chạy."""
    if native_proc is not None and native_proc.poll() is None:
        native_proc.terminate()
        try:
            native_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            native_proc.kill()
    if stop is not None:
        stop.set()
        for p in procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()


def read_snapshot(smc, sensors):
    """Đọc một mẫu nhiệt độ của tất cả cảm biến và tốc độ quạt."""
    vals = []
    for key in sensors.values():
        try:
            vals.append(round(smc.read(key)[0], 1))
        except Exception:
            vals.append("")
    try:
        fan = round(smc.read(FAN_KEY)[0])
    except Exception:
        fan = ""
    return vals, fan


def resolve_active_sensors(sensors_arg):
    """Kiểm tra và trả về từ điển các cảm biến SMC đọc được giá trị hợp lệ."""
    smc = SMC()
    sensors = parse_sensors(sensors_arg)
    active = {}
    for name, key in sensors.items():
        try:
            v, _ = smc.read(key)
            if v is not None and 1 < v < 130:
                active[name] = key
                continue
        except KeyError:
            pass
        print(f"[bỏ qua] {name} ({key}) không đọc được - hãy dùng lệnh `list` để tìm đúng khóa.")
    if not active:
        sys.exit("Không có cảm biến nào đọc được.")
    return smc, active


def run_session(args, phases, auto_report=False):
    """Chạy phiên stress test kết hợp ghi log CSV theo thời gian thực và tự động xuất báo cáo nếu được bật."""
    smc, active = resolve_active_sensors(args.sensors)
    cols = ["time_s", "phase"] + list(active) + ["fan_rpm"]
    cpu_threads = 0 if args.no_cpu_stress else (args.procs or os.cpu_count() or 4)
    vram_mb = 0 if args.no_gpu_stress else args.vram_mb

    print(f"Ghi vào {args.out}  |  cảm biến: {', '.join(f'{n}={k}' for n, k in active.items())}")
    print(f"Cấu hình tải ('load'): CPU={cpu_threads} luồng | GPU/VRAM={vram_mb} MB.\n")

    t0 = time.time()
    native_proc, procs, stop = None, [], None
    row_count = 0
    try:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for pname, dur in phases:
                if pname == "load":
                    native_proc, procs, stop = start_stress(cpu_threads, vram_mb)
                end = None if (pname == "load" and dur <= 0) else (time.time() + dur)
                while end is None or time.time() < end:
                    vals, fan = read_snapshot(smc, active)
                    row = [round(time.time() - t0, 1), pname] + vals + [fan]
                    w.writerow(row)
                    f.flush()
                    row_count += 1
                    print(
                        f"{row[0]:7.0f}s  {pname:<5} "
                        + "  ".join(f"{n}={v}" for n, v in zip(list(active) + ["quạt"], row[2:]))
                    )
                    time.sleep(args.interval)
                if pname == "load":
                    stop_stress(native_proc, procs, stop)
                    native_proc, procs, stop = None, [], None
    except KeyboardInterrupt:
        print("\nDừng sớm (Ctrl-C), dữ liệu đã được lưu lại.")
    finally:
        stop_stress(native_proc, procs, stop)

    if auto_report and row_count > 0:
        report_base = getattr(args, "report_out", None) or os.path.splitext(args.out)[0]
        print("\nĐang tạo báo cáo và biểu đồ trực quan...")
        generate_report([args.out], out_base=report_base)
    elif row_count > 0:
        print(f"\nXong. Tạo báo cáo bằng lệnh:  python3 {os.path.basename(__file__)} report {args.out}")


def cmd_stress(args):
    """Chạy stress test CPU + GPU + VRAM, ghi log thời gian thực và tự động tạo báo cáo HTML/PNG."""
    phases = []
    if args.idle > 0:
        phases.append(("idle", args.idle))
    phases.append(("load", args.duration))
    if args.cool > 0:
        phases.append(("cool", args.cool))
    run_session(args, phases, auto_report=not args.no_report)


def cmd_log(args):
    """Ghi nhật ký nhiệt độ theo các pha (idle, load, cool) ra tệp CSV."""
    phases = [(n, d) for n, d in (("idle", args.idle), ("load", args.load), ("cool", args.cool)) if d > 0]
    if not phases:
        sys.exit("Ít nhất một pha (idle, load, cool) phải lớn hơn 0 giây.")
    run_session(args, phases, auto_report=args.report)


# ------------------------------------------------------------ báo cáo -----
def load_csv(path):
    """Đọc dữ liệu từ tệp CSV đã ghi nhật ký."""
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cols = [c for c in rows[0].keys() if c not in ("time_s", "phase", "fan_rpm")]
    data = {
        "t": [float(r["time_s"]) / 60 for r in rows],
        "phase": [r["phase"] for r in rows],
        "fan": [float(r["fan_rpm"]) if r["fan_rpm"] else None for r in rows],
    }
    for c in cols:
        data[c] = [float(r[c]) if r[c] else None for r in rows]
    return data, cols


def stats(vals, phases, ph):
    """Tính Min, Trung bình, Max và giá trị ổn định (20% mẫu cuối) cho một pha."""
    v = [x for x, p in zip(vals, phases) if p == ph and x is not None]
    if not v:
        return None
    tail = v[max(0, int(len(v) * 0.8)):]
    return min(v), sum(v) / len(v), max(v), sum(tail) / len(tail)


def spans(t, phases):
    """Xác định khoảng thời gian bắt đầu và kết thúc của từng pha để tô màu nền biểu đồ."""
    out, start = [], 0
    for i in range(1, len(phases) + 1):
        if i == len(phases) or phases[i] != phases[start]:
            out.append((phases[start], t[start], t[i - 1]))
            start = i
    return out


def generate_report(files, out_base=None):
    """Vẽ biểu đồ PNG và xuất bảng thống kê HTML từ một hoặc nhiều tệp CSV."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("Thiếu thư viện matplotlib. Cài đặt bằng lệnh:  python3 -m pip install matplotlib")

    sets = []
    for p in files:
        d, cols = load_csv(p)
        sets.append((os.path.splitext(os.path.basename(p))[0], d, cols))

    colors = {"idle": "#e8f1fb", "load": "#fdeaea", "cool": "#eaf6ea"}
    if len(sets) == 1:
        label, d, cols = sets[0]
        fig, (a1, a2) = plt.subplots(
            2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
        )
        for c in cols:
            a1.plot(d["t"], d[c], label=c, linewidth=1.6)
        a2.plot(d["t"], d["fan"], color="tab:gray", linewidth=1.4)
        for ph, x0, x1 in spans(d["t"], d["phase"]):
            for a in (a1, a2):
                a.axvspan(x0, x1, color=colors.get(ph, "#eee"), alpha=0.7, lw=0)
            a1.text((x0 + x1) / 2, a1.get_ylim()[1], ph, ha="center", va="top", fontsize=9)
        a1.set_ylabel("Nhiệt độ (°C)")
        a1.grid(alpha=0.3)
        a1.legend(ncol=3, loc="lower right")
        a1.set_title(f"Nhiệt độ iMac - {label}")
        a2.set_ylabel("Quạt (RPM)")
        a2.set_xlabel("Thời gian (phút)")
        a2.grid(alpha=0.3)
    else:
        cols = sets[0][2]
        fig, axes = plt.subplots(len(cols), 1, figsize=(11, 3 * len(cols)), sharex=True, squeeze=False)
        for ax, c in zip(axes[:, 0], cols):
            for label, d, _cs in sets:
                if c in d:
                    ax.plot(d["t"], d[c], label=label, linewidth=1.6)
            ax.set_ylabel(f"{c} (°C)")
            ax.grid(alpha=0.3)
            ax.legend(loc="lower right")
        axes[-1, 0].set_xlabel("Thời gian (phút)")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    png = buf.getvalue()
    base = out_base or "bao_cao_nhiet_do"
    with open(base + ".png", "wb") as f:
        f.write(png)

    # Tạo bảng thống kê ra màn hình console và HTML
    lines, html = [], []
    for label, d, cols in sets:
        html.append(
            f"<h2>{label}</h2><table><tr><th>Cảm biến</th><th>Pha</th><th>Thấp nhất</th>"
            f"<th>Trung bình</th><th>Cao nhất</th><th>Ổn định (20% cuối)</th></tr>"
        )
        lines.append(f"\n=== {label} ===")
        lines.append(f"{'Cảm biến':<14}{'Pha':<6}{'Min':>7}{'TB':>7}{'Max':>7}{'Ổn định':>9}")
        for c in cols:
            for ph in ("idle", "load", "cool"):
                s = stats(d[c], d["phase"], ph)
                if s:
                    lines.append(f"{c:<14}{ph:<6}{s[0]:7.1f}{s[1]:7.1f}{s[2]:7.1f}{s[3]:9.1f}")
                    html.append(
                        f"<tr><td>{c}</td><td>{ph}</td><td>{s[0]:.1f}</td><td>{s[1]:.1f}</td>"
                        f"<td>{s[2]:.1f}</td><td>{s[3]:.1f}</td></tr>"
                    )
        fans = [x for x in d["fan"] if x]
        if fans:
            lines.append(f"Quạt: TB {sum(fans) / len(fans):.0f} RPM, tối đa {max(fans):.0f} RPM")
        html.append("</table>")
    print("\n".join(lines))

    page = (
        f"""<!doctype html><meta charset="utf-8"><title>Báo cáo nhiệt độ iMac</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:960px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse;margin-bottom:16px}}td,th{{border:1px solid #ccc;padding:4px 10px;text-align:right}}
th{{background:#f2f2f2}}td:first-child,td:nth-child(2){{text-align:left}}img{{max-width:100%}}</style>
<h1>Báo cáo nhiệt độ iMac</h1><p>Tạo lúc {datetime.datetime.now():%Y-%m-%d %H:%M}</p>
<img src="data:image/png;base64,{base64.b64encode(png).decode()}">{''.join(html)}"""
    )
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(page)
    print(f"\nĐã lưu: {base}.html (báo cáo đầy đủ) và {base}.png (biểu đồ)")
    os.system(f'open "{base}.html"')


def cmd_report(args):
    """Điểm vào lệnh `report` từ dòng lệnh."""
    generate_report(args.files, out_base=args.out)


# -------------------------------------------------------------- main -----
def main():
    """Điểm khởi chạy chương trình dòng lệnh."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="liệt kê các khóa nhiệt độ SMC").set_defaults(fn=cmd_list)

    st = sub.add_parser(
        "stress",
        help="chạy stress test CPU + GPU + VRAM, ghi log thời gian thực và tự động tạo báo cáo",
    )
    st.add_argument("--duration", type=int, default=300, help="thời lượng stress test, giây (0 = chạy vô hạn, mặc định 300)")
    st.add_argument("--idle", type=int, default=0, help="tùy chọn thêm pha nghỉ trước khi stress, giây (mặc định 0)")
    st.add_argument("--cool", type=int, default=0, help="tùy chọn thêm pha làm nguội sau khi stress, giây (mặc định 0)")
    st.add_argument("--interval", type=float, default=2.0, help="chu kỳ lấy mẫu và in nhiệt độ, giây (mặc định 2.0)")
    st.add_argument("--out", default="stress_test.csv", help="tên tệp CSV lưu nhật ký (mặc định stress_test.csv)")
    st.add_argument("--report-out", help="tên tệp báo cáo HTML/PNG đầu ra (mặc định trùng tên với --out)")
    st.add_argument("--no-report", action="store_true", help="không tự động tạo báo cáo sau khi stress xong")
    st.add_argument("--sensors", help='ghi đè khóa SMC, vd "GPU_VRAM_Raw=TG1p,GPU_Die_Raw=TG0d"')
    st.add_argument("--procs", type=int, help="số luồng ép tải CPU (mặc định = số luồng hệ thống)")
    st.add_argument("--vram-mb", type=int, default=4096, help="dung lượng VRAM ép tải, MB (mặc định 4096)")
    st.add_argument("--no-cpu-stress", action="store_true", help="không ép tải CPU")
    st.add_argument("--no-gpu-stress", action="store_true", help="không ép tải GPU và VRAM")
    st.set_defaults(fn=cmd_stress)

    p = sub.add_parser("log", help="ghi nhật ký nhiệt độ ra tệp CSV")
    p.add_argument("--idle", type=int, default=300, help="số giây ở trạng thái nghỉ (mặc định 300)")
    p.add_argument("--load", type=int, default=600, help="số giây chạy tải nặng (mặc định 600)")
    p.add_argument("--cool", type=int, default=300, help="số giây làm nguội (mặc định 300)")
    p.add_argument("--interval", type=float, default=2.0, help="chu kỳ lấy mẫu, tính bằng giây")
    p.add_argument("--out", default="nhiet_do.csv", help="tên tệp CSV đầu ra")
    p.add_argument("--report", action="store_true", help="tự động tạo báo cáo HTML + biểu đồ sau khi ghi xong")
    p.add_argument("--sensors", help='ghi đè khóa SMC, vd "GPU_VRAM_Raw=TG1p,GPU_Die_Raw=TG0d"')
    p.add_argument("--procs", type=int, help="số luồng ép tải CPU (mặc định = số luồng hệ thống)")
    p.add_argument("--vram-mb", type=int, default=4096, help="dung lượng VRAM ép tải trong pha load, MB (mặc định 4096)")
    p.add_argument("--no-cpu-stress", action="store_true", help="không tự động ép tải CPU")
    p.add_argument("--no-gpu-stress", action="store_true", help="không tự động ép tải GPU và VRAM")
    p.set_defaults(fn=cmd_log)

    r = sub.add_parser("report", help="tạo báo cáo HTML + biểu đồ từ CSV")
    r.add_argument("files", nargs="+", help="danh sách tệp CSV đầu vào")
    r.add_argument("--out", help="tên tệp đầu ra (không gồm phần mở rộng)")
    r.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
