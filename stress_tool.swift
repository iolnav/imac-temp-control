// stress_tool.swift - Công cụ tạo tải nặng (stress test) kết hợp cho CPU, GPU và VRAM trên macOS (Metal).
// Biên dịch: swiftc -O stress_tool.swift -framework Metal -framework Foundation -o stress_tool

import Foundation
import Metal
import os

/// Tham số dòng lệnh của chương trình.
struct Config {
    var cpuThreads: Int = ProcessInfo.processInfo.activeProcessorCount
    var vramMB: Int = 4096
    var durationSec: Double = 0  // 0 = chạy đến khi nhận SIGINT / SIGTERM
}

func parseArgs() -> Config {
    var cfg = Config()
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--cpu":
            if i + 1 < args.count, let v = Int(args[i + 1]) {
                cfg.cpuThreads = max(0, v)
                i += 1
            }
        case "--vram-mb":
            if i + 1 < args.count, let v = Int(args[i + 1]) {
                cfg.vramMB = max(0, v)
                i += 1
            }
        case "--duration":
            if i + 1 < args.count, let v = Double(args[i + 1]) {
                cfg.durationSec = max(0, v)
                i += 1
            }
        default:
            break
        }
        i += 1
    }
    return cfg
}

// Cờ dừng an toàn luồng (dùng os_unfair_lock để trình biên dịch -O không tối ưu hóa bỏ qua kiểm tra).
private var stopLock = os_unfair_lock()
private var _shouldStop = false

func isStopped() -> Bool {
    os_unfair_lock_lock(&stopLock)
    let v = _shouldStop
    os_unfair_lock_unlock(&stopLock)
    return v
}

func requestStop() {
    os_unfair_lock_lock(&stopLock)
    _shouldStop = true
    os_unfair_lock_unlock(&stopLock)
}

func handleSignal(_ sig: Int32) {
    _exit(0)
}

/// Khởi chạy các luồng ép tải CPU bằng phép toán dấu phẩy động nặng (FMA, lượng giác, căn bậc hai).
func startCPUStress(threads: Int, group: DispatchGroup) {
    guard threads > 0 else { return }
    for t in 0..<threads {
        group.enter()
        // Dùng QoS .utility để vẫn ép 100% CPU nhưng không làm nghẽn luồng điều phối GPU/Metal và tiến trình ghi log
        DispatchQueue.global(qos: .utility).async {
            var a = 1.000001 + Double(t) * 0.0001
            var b = 0.999999 - Double(t) * 0.0001
            var c = 1.234567
            var sink: Double = 0
            while !isStopped() {
                // Vòng lặp tính toán liên tục để giữ ALU và FPU của CPU ở mức tải tối đa
                for _ in 0..<50_000 {
                    a = a.addingProduct(b, c)
                    b = sin(a) + 1.5
                    c = cos(b) + sqrt(abs(a) + 1.0)
                    if a > 1e6 || a < -1e6 { a = 1.000001 }
                }
                sink += a + b + c
            }
            // Ngăn trình biên dịch tối ưu hóa bỏ qua vòng lặp
            if sink == -123456.789 {
                print(sink)
            }
            group.leave()
        }
    }
}

/// Mã nguồn Metal Compute Shader dùng để khởi tạo và ép tải GPU + VRAM.
private let metalSource = """
#include <metal_stdlib>
using namespace metal;

// Khởi tạo giá trị ban đầu trực tiếp trong VRAM (tránh nghẽn băng thông PCIe)
kernel void init_vram(device float4 *dst [[buffer(0)]],
                      constant uint &seed [[buffer(1)]],
                      uint id [[thread_position_in_grid]]) {
    float f = float((id + seed) & 0xFFFFu) * 0.0001f + 0.5f;
    dst[id] = float4(f, f * 1.01f, f * 0.99f, f * 1.02f);
}

// Ép tải đồng thời nhân tính toán GPU (ALU/FMA) và băng thông bộ nhớ VRAM (đọc/ghi)
kernel void burn_gpu_vram(device const float4 *srcA [[buffer(0)]],
                          device const float4 *srcB [[buffer(1)]],
                          device float4 *dst        [[buffer(2)]],
                          uint id [[thread_position_in_grid]]) {
    float4 x = srcA[id];
    float4 y = srcB[id];
    for (int i = 0; i < 128; i++) {
        x = fma(x, y, float4(1.00013f, 0.99987f, 1.00029f, 0.99971f));
        y = fma(y, x, float4(0.99979f, 1.00021f, 0.99963f, 1.00037f));
        // Giữ giá trị trong khoảng ổn định để tránh NaN/Inf
        x = fract(x) + float4(0.5f);
        y = fract(y) + float4(0.5f);
    }
    dst[id] = (x + y) * 0.5f;
}
"""

/// Khởi chạy stress test GPU và VRAM qua Metal.
func startGPUVRAMStress(vramMB: Int, group: DispatchGroup) {
    guard vramMB > 0 else { return }
    // Ưu tiên chọn GPU rời (isLowPower == false, vd Radeon Pro 580) thay vì iGPU
    let selectedDevice = MTLCopyAllDevices().first(where: { !$0.isLowPower }) ?? MTLCreateSystemDefaultDevice()
    guard let device = selectedDevice,
          let queue = device.makeCommandQueue() else {
        fputs("[cảnh báo] Không khởi tạo được thiết bị Metal để stress GPU.\n", stderr)
        return
    }

    // Giới hạn an toàn tối đa 75% dung lượng VRAM khuyến nghị để tránh treo giao diện macOS
    let maxSafeMB = Int(Double(device.recommendedMaxWorkingSetSize) * 0.75) / (1024 * 1024)
    let targetMB = min(vramMB, max(256, maxSafeMB))
    let chunkMB = 128
    let chunkCount = max(3, targetMB / chunkMB)  // Cần ít nhất 3 buffer để xoay vòng đọc/ghi
    let chunkBytes = chunkMB * 1024 * 1024
    let elementCount = chunkBytes / MemoryLayout<SIMD4<Float>>.stride

    guard let library = try? device.makeLibrary(source: metalSource, options: nil),
          let initFn = library.makeFunction(name: "init_vram"),
          let burnFn = library.makeFunction(name: "burn_gpu_vram"),
          let initPipe = try? device.makeComputePipelineState(function: initFn),
          let burnPipe = try? device.makeComputePipelineState(function: burnFn) else {
        fputs("[cảnh báo] Không biên dịch được Metal shader.\n", stderr)
        return
    }

    // Cấp phát các vùng đệm hoàn toàn nằm trong VRAM riêng của GPU (.storageModePrivate)
    var buffers: [MTLBuffer] = []
    for _ in 0..<chunkCount {
        if let buf = device.makeBuffer(length: chunkBytes, options: .storageModePrivate) {
            buffers.append(buf)
        }
    }
    guard buffers.count >= 3 else {
        fputs("[cảnh báo] Không cấp phát đủ VRAM trên GPU.\n", stderr)
        return
    }

    let tgWidth = min(256, burnPipe.maxTotalThreadsPerThreadgroup)
    let gridSize = MTLSize(width: elementCount, height: 1, depth: 1)
    let tgSize = MTLSize(width: tgWidth, height: 1, depth: 1)

    // Khởi tạo dữ liệu cho toàn bộ các chunk VRAM
    for (idx, buf) in buffers.enumerated() {
        if let cmd = queue.makeCommandBuffer(),
           let enc = cmd.makeComputeCommandEncoder() {
            var seed = UInt32(idx * 97 + 1)
            enc.setComputePipelineState(initPipe)
            enc.setBuffer(buf, offset: 0, index: 0)
            enc.setBytes(&seed, length: MemoryLayout<UInt32>.stride, index: 1)
            enc.dispatchThreads(gridSize, threadsPerThreadgroup: tgSize)
            enc.endEncoding()
            cmd.commit()
            cmd.waitUntilCompleted()
        }
    }

    let actualMB = buffers.count * chunkMB
    print("[GPU/VRAM] Đang ép tải \(device.name) với \(actualMB) MB VRAM (\(buffers.count) x \(chunkMB) MB)...")
    fflush(stdout)

    group.enter()
    // Chạy trên QoS .userInteractive để luồng gửi lệnh Metal luôn được đánh thức ngay lập tức, không bị CPU burn chèn ép
    DispatchQueue.global(qos: .userInteractive).async {
        var step = 0
        let n = buffers.count
        while !isStopped() {
            // Đẩy 2 lệnh nối tiếp (double-buffering) để GPU luôn bận liên tục không bị rảnh giữa các lần chờ
            var lastCmd: MTLCommandBuffer?
            for _ in 0..<2 {
                if isStopped() { break }
                let a = buffers[step % n]
                let b = buffers[(step + 1) % n]
                let c = buffers[(step + 2) % n]
                step += 1

                if let cmd = queue.makeCommandBuffer(),
                   let enc = cmd.makeComputeCommandEncoder() {
                    enc.setComputePipelineState(burnPipe)
                    enc.setBuffer(a, offset: 0, index: 0)
                    enc.setBuffer(b, offset: 0, index: 1)
                    enc.setBuffer(c, offset: 0, index: 2)
                    enc.dispatchThreads(gridSize, threadsPerThreadgroup: tgSize)
                    enc.endEncoding()
                    cmd.commit()
                    lastCmd = cmd
                }
            }
            lastCmd?.waitUntilCompleted()
        }
        group.leave()
    }
}

// ----------------------------------------------------------------- Điểm vào -----
signal(SIGINT, handleSignal)
signal(SIGTERM, handleSignal)

let cfg = parseArgs()
let group = DispatchGroup()

if cfg.cpuThreads > 0 {
    print("[CPU] Đang ép tải \(cfg.cpuThreads) luồng tính toán dấu phẩy động...")
    fflush(stdout)
    startCPUStress(threads: cfg.cpuThreads, group: group)
}

if cfg.vramMB > 0 {
    startGPUVRAMStress(vramMB: cfg.vramMB, group: group)
}

if cfg.durationSec > 0 {
    let deadline = Date().addingTimeInterval(cfg.durationSec)
    while !isStopped() && Date() < deadline {
        Thread.sleep(forTimeInterval: 0.2)
    }
    requestStop()
} else {
    while !isStopped() {
        Thread.sleep(forTimeInterval: 0.2)
    }
}

group.wait()
