"""扩展的一键启动宿主（Chrome Native Messaging）。

扩展调用 chrome.runtime.sendNativeMessage('local.live_subtitles', {...}) 时，Chrome 会启动
这个脚本（经 native_host.bat），通过 stdin/stdout 交换一条消息后本进程就退出。
服务本身以**独立的后台进程**运行，不随本脚本退出，也不随浏览器关闭而退出（靠闲置超时自己退）。

只接受下面几个指令，不执行任意命令：

    {"cmd": "status"}                        → 服务是否在跑、pid、启动参数
    {"cmd": "start", "asr": "parakeet"}      → 没在跑就后台启动（asr ∈ ASR_CHOICES）
    {"cmd": "restart", "asr": "sensevoice"}  → 停掉再按新参数启动（换识别模型用）
    {"cmd": "stop"}                          → 停掉服务
    {"cmd": "log", "lines": 200}             → 返回 runs/service.log 的最后 N 行

安装：运行一次 `安装一键启动.bat`（tools/install_native_host.py），只写当前用户的注册表，不需要管理员。
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
LOG = RUNS / "service.log"
STATE = RUNS / "service_state.json"   # 记录后台服务的 pid 和启动参数
# 扩展写死连 8766；环境变量只给自测用（用户自己的服务占着 8766 时不去碰它）。
PORT = int(os.environ.get("LLS_NATIVE_PORT", "8766"))

ASR_CHOICES = ("parakeet", "sensevoice", "hybrid")
# 闲置多久自动退出（分钟）。0 = 不自动退出。
IDLE_CHOICES = (0, 15, 30, 60, 120)

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


# --- Native Messaging 协议：4 字节小端长度 + UTF-8 JSON ----------------------

def read_message():
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > 1024 * 1024:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


# --- 服务进程管理 ---------------------------------------------------------------

def port_open(port=PORT):
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def listener():
    """正在监听服务端口的进程：(pid, 命令行)，没有则 None。

    不能靠记录下来的 pid 判断：venv 里的 pythonw.exe 只是个转发器，它会再拉起真正的解释器
    （10-01 用户实测：记下的 6744 早已退出，真正的服务是它的子进程 29820），
    于是"记录的 pid 不在了"被误判成"服务是手动用 .bat 开的"，「停止服务」也就失灵了。
    直接问系统谁在监听这个端口最可靠。
    """
    script = (f"$c = Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | "
              "Select-Object -First 1; if ($c) { $p = Get-CimInstance Win32_Process -Filter "
              "\"ProcessId=$($c.OwningProcess)\"; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Write-Output \"$($c.OwningProcess)`t$($p.CommandLine)\" }")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                            creationflags=CREATE_NO_WINDOW)
    line = result.stdout.decode("utf-8", "replace").strip()
    if not line:
        return None
    pid, _, command = line.partition("	")
    return int(pid), command


def _alive(pid):
    """转发器进程还在不在（只用于判断"正在启动"，不用于判断服务本身）。"""
    if not pid:
        return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                            capture_output=True, creationflags=CREATE_NO_WINDOW)
    return f'"{int(pid)}"'.encode() in result.stdout


def is_ours(command):
    """一键启动的服务带 --idle-exit（.bat 手动启动的不带），且跑的是本项目的 run_service.py。"""
    return "run_service.py" in command and "--idle-exit" in command and str(ROOT).lower() in command.lower()


def status():
    state = read_state()
    found = listener() if port_open() else None
    managed = bool(found and is_ours(found[1]))
    return {
        "ok": True,
        "running": found is not None,
        # managed=True：是一键启动的后台服务；False 而 running=True：用户自己用 .bat 开的
        "managed": managed,
        "pid": found[0] if found else None,
        "asr": state.get("asr") if managed else None,
        "en_asr": state.get("en_asr") if managed else None,
        "idle_minutes": state.get("idle_minutes") if managed else None,
        "started_at": state.get("started_at") if managed else None,
        "log": str(LOG),
    }


def python_exe():
    # 用 pythonw（无控制台窗口）；找不到就退回当前解释器 + CREATE_NO_WINDOW。
    # 查找顺序与 .bat 一致：项目内 .venv → ../../.venv（原开发目录布局）→ 当前解释器旁边
    candidates = [ROOT / ".venv" / "Scripts" / "pythonw.exe",
                  ROOT.parent.parent / ".venv" / "Scripts" / "pythonw.exe",
                  Path(sys.executable).with_name("pythonw.exe"), Path(sys.executable)]
    return next(str(p) for p in candidates if p.is_file())


EN_ASR_CHOICES = ("parakeet", "sensevoice")


def start(asr="parakeet", idle_minutes=30, en_asr="parakeet"):
    if asr not in ASR_CHOICES:
        return {"ok": False, "error": f"未知识别模型：{asr}"}
    if en_asr not in EN_ASR_CHOICES:
        en_asr = "parakeet"
    if idle_minutes not in IDLE_CHOICES:
        idle_minutes = 30
    if port_open():
        return {**status(), "already": True}
    # 防重入：服务加载模型要几秒到几十秒才开始监听端口，这期间再来一次 start（弹窗重开后又点了一次、
    # 或 restart 紧跟着 start）会拉起第二个实例，两个抢同一个端口，后起的那个报错退出，
    # 日志还会互相穿插（10-01 用户实测日志就是这样）。记下的启动时间在 90 秒内且进程还在，就当作"正在启动"。
    state = read_state()
    if state.get("started_at") and time.time() - state["started_at"] < 90 and _alive(state.get("pid")):
        return {"ok": True, "starting": True, "already": True, "pid": state.get("pid"), "asr": state.get("asr")}

    RUNS.mkdir(parents=True, exist_ok=True)
    log = LOG.open("a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 由扩展启动：asr={asr} en_asr={en_asr} "
              f"idle={idle_minutes}min =====\n")
    log.flush()
    argv = [python_exe(), "-u", str(ROOT / "tools" / "run_service.py"), "--asr", asr, "--en-asr", en_asr,
            "--port", str(PORT), "--idle-exit", str(idle_minutes * 60)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True)
    STATE.write_text(json.dumps({"pid": process.pid, "asr": asr, "en_asr": en_asr, "idle_minutes": idle_minutes,
                                 "started_at": time.time()}), encoding="utf-8")
    return {"ok": True, "starting": True, "pid": process.pid, "asr": asr}


def stop():
    found = listener() if port_open() else None
    if found and not is_ours(found[1]):
        # 端口被占但不是一键启动的：多半是用户手动开的 .bat 窗口，不替用户关。
        return {"ok": False, "error": "服务是手动用 .bat 启动的，请直接关闭那个窗口"}
    pids = {found[0]} if found else set()
    recorded = read_state().get("pid")
    if recorded:
        pids.add(int(recorded))   # 转发器进程（多半已退出），一并清掉
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    for _ in range(50):
        if not port_open():
            break
        time.sleep(0.1)
    STATE.unlink(missing_ok=True)
    return {**status(), "stopped": True}


def tail_log(lines=200):
    lines = max(1, min(int(lines), 2000))
    try:
        with LOG.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"ok": True, "text": "（还没有服务日志：服务尚未通过扩展启动过）", "path": str(LOG)}
    return {"ok": True, "text": "\n".join(text.splitlines()[-lines:]), "path": str(LOG)}


def handle(message):
    cmd = (message or {}).get("cmd")
    if cmd == "status":
        return status()
    if cmd == "start":
        return start(message.get("asr", "parakeet"), message.get("idle_minutes", 30),
                     message.get("en_asr", "parakeet"))
    if cmd == "restart":
        stopped = stop()
        if not stopped.get("ok"):
            return stopped
        return start(message.get("asr", "parakeet"), message.get("idle_minutes", 30),
                     message.get("en_asr", "parakeet"))
    if cmd == "stop":
        return stop()
    if cmd == "log":
        return tail_log(message.get("lines", 200))
    return {"ok": False, "error": f"未知指令：{cmd}"}


def main():
    try:
        message = read_message()
        send_message(handle(message))
    except Exception as exc:  # 宿主崩了扩展只会看到"native host has exited"，这里尽量回一句人话
        send_message({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
