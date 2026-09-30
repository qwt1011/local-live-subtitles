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


def pid_alive(pid):
    if not pid:
        return False
    result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH", "/FO", "CSV"],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return f'"{int(pid)}"' in result.stdout


def status():
    state = read_state()
    alive = pid_alive(state.get("pid"))
    return {
        "ok": True,
        "running": port_open(),
        # managed=True 表示是本宿主启动的后台服务；False 而 running=True 表示用户自己用 .bat 开的
        "managed": alive,
        "pid": state.get("pid") if alive else None,
        "asr": state.get("asr") if alive else None,
        "idle_minutes": state.get("idle_minutes") if alive else None,
        "started_at": state.get("started_at") if alive else None,
        "log": str(LOG),
    }


def python_exe():
    # 用 pythonw（无控制台窗口）；找不到就退回当前解释器 + CREATE_NO_WINDOW。
    # 查找顺序与 .bat 一致：项目内 .venv → ../../.venv（原开发目录布局）→ 当前解释器旁边
    candidates = [ROOT / ".venv" / "Scripts" / "pythonw.exe",
                  ROOT.parent.parent / ".venv" / "Scripts" / "pythonw.exe",
                  Path(sys.executable).with_name("pythonw.exe"), Path(sys.executable)]
    return next(str(p) for p in candidates if p.is_file())


def start(asr="parakeet", idle_minutes=30):
    if asr not in ASR_CHOICES:
        return {"ok": False, "error": f"未知识别模型：{asr}"}
    if idle_minutes not in IDLE_CHOICES:
        idle_minutes = 30
    if port_open():
        return {**status(), "already": True}

    RUNS.mkdir(parents=True, exist_ok=True)
    log = LOG.open("a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 由扩展启动：asr={asr} "
              f"idle={idle_minutes}min =====\n")
    log.flush()
    argv = [python_exe(), "-u", str(ROOT / "tools" / "run_service.py"), "--asr", asr,
            "--port", str(PORT), "--idle-exit", str(idle_minutes * 60)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True)
    STATE.write_text(json.dumps({"pid": process.pid, "asr": asr, "idle_minutes": idle_minutes,
                                 "started_at": time.time()}), encoding="utf-8")
    return {"ok": True, "starting": True, "pid": process.pid, "asr": asr}


def stop():
    state = read_state()
    pid = state.get("pid")
    if pid and pid_alive(pid):
        # /T 连同子进程一起结束（pythonw → 服务本身就是同一个进程，但保险起见）
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
        for _ in range(30):
            if not port_open():
                break
            time.sleep(0.1)
    elif port_open():
        # 端口被占但不是本宿主启动的：多半是用户手动开的 .bat 窗口，不替用户关。
        return {"ok": False, "error": "服务是手动用 .bat 启动的，请直接关闭那个窗口"}
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
        return start(message.get("asr", "parakeet"), message.get("idle_minutes", 30))
    if cmd == "restart":
        stopped = stop()
        if not stopped.get("ok"):
            return stopped
        return start(message.get("asr", "parakeet"), message.get("idle_minutes", 30))
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
