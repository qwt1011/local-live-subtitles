"""从内嵌（硬）字幕的视频里 OCR 出带时间的字幕轨，用作翻译质量的参考。

    python tools/ocr_subtitles.py --video "<内嵌中字版.mp4>" --out eval/zh_refs/en_asmr_01.json
    python tools/ocr_subtitles.py --video ... --start 480 --duration 60 --debug   # 只跑一段并保存截图

做法：每 0.25 秒取一帧，裁出字幕条 → 二值化（白字变黑字、其余变白）→ Windows 自带 OCR（Windows.Media.Ocr，
zh-Hans）→ 相邻帧文字相同（或只差一两个字的 OCR 抖动）就合并成一条，记下出现和消失的时间。
不需要额外安装 OCR 引擎；只能在 Windows 上跑。

字幕条位置因视频而异，用 --crop 指定（ffmpeg crop 参数 w:h:x:y，基于原始分辨率）；默认值对应
B 站 852x480 的那份内嵌中字版（避开了左下角的频道水印）。
"""

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]

# 用 PowerShell 调 WinRT OCR：一次进程处理一整批图片，避免每帧都启动一次 PowerShell（约 1 秒）。
OCR_PS1 = r'''
param([string]$ListFile)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait() | Out-Null; $task.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans-CN'))
[Console]::OutputEncoding = [Text.Encoding]::UTF8
foreach ($path in [IO.File]::ReadAllLines($ListFile, [Text.Encoding]::UTF8)) {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $text = ($result.Lines | ForEach-Object { $_.Text }) -join ' '
  $stream.Dispose()
  Write-Output ("{0}`t{1}" -f [IO.Path]::GetFileName($path), $text)
}
'''

# OCR 偶尔把简体认成繁体字形；只收这几个实测出现过的，避免误伤
TRAD_TO_SIMP = str.maketrans({"記": "记", "說": "说", "這": "这", "麼": "么", "們": "们", "還": "还",
                              "對": "对", "會": "会", "時": "时", "嗎": "吗", "為": "为", "個": "个"})


def clean(text):
    """去掉 OCR 在字间插的空格、两端的杂点，统一字形。"""
    text = "".join(text.split()).translate(TRAD_TO_SIMP)
    return text.strip("一-_—~'\"“”‘’.,，、|")


def extract_frames(video, start, duration, step, crop, out_dir):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # 白字（亮度 > 200）变黑、其余变白：去掉画面背景，Windows OCR 在这种图上几乎不出错
    vf = f"fps={1 / step},crop={crop},scale=iw*2:-1,format=gray,lutyuv=y='if(gt(val,200),0,255)'"
    args = [ffmpeg, "-y", "-loglevel", "error", "-ss", str(start), "-i", str(video)]
    if duration:
        args += ["-t", str(duration)]
    args += ["-vf", vf, str(out_dir / "f%06d.png")]
    subprocess.run(args, check=True)
    return sorted(out_dir.glob("f*.png"))


def ocr(paths, batch=400):
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "ocr.ps1"
        script.write_text(OCR_PS1, encoding="utf-8-sig")   # PowerShell 5 需要 BOM 才按 UTF-8 读
        for i in range(0, len(paths), batch):
            chunk = paths[i:i + batch]
            listing = Path(tmp) / "list.txt"
            listing.write_text("\n".join(str(p) for p in chunk), encoding="utf-8")
            out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(script), "-ListFile", str(listing)],
                                 capture_output=True, check=True).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                name, _, text = line.partition("\t")
                results[name.strip()] = clean(text)
            print(f"  OCR {min(i + batch, len(paths))}/{len(paths)}", flush=True)
    return [results.get(p.name, "") for p in paths]


def same_line(a, b):
    """两帧是不是同一条字幕：完全相同，或只差一两个字（OCR 抖动）。"""
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def merge(texts, start, step, min_frames=2):
    """相邻相同的帧合并成一条字幕；只出现 1 帧的当作噪声丢掉。同一条里取出现次数最多的写法。"""
    cues, current = [], None
    for index, text in enumerate(texts):
        t = start + index * step
        if current and same_line(current["variants"][-1], text):
            current["variants"].append(text)
            current["end"] = t + step
            continue
        if current:
            cues.append(current)
        current = {"start": t, "end": t + step, "variants": [text]} if text else None
    if current:
        cues.append(current)
    out = []
    for cue in cues:
        if len(cue["variants"]) < min_frames:
            continue
        best = max(set(cue["variants"]), key=cue["variants"].count)
        out.append({"start": round(cue["start"], 2), "end": round(cue["end"], 2), "text": best})
    return out


def main():
    parser = argparse.ArgumentParser(description="内嵌字幕 OCR")
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--step", type=float, default=0.25, help="采样间隔（秒）")
    parser.add_argument("--crop", default="640:56:106:404", help="字幕条区域 w:h:x:y（原始分辨率）")
    parser.add_argument("--offset", type=float, default=0.0,
                        help="加到时间戳上的偏移，用于对齐到另一个版本的视频（秒）")
    parser.add_argument("--debug", action="store_true", help="保留截图到 runs/ocr_debug/")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("依赖 Windows 自带的 OCR，只能在 Windows 上运行")

    with tempfile.TemporaryDirectory() as tmp:
        frames_dir = ROOT / "runs" / "ocr_debug" if args.debug else Path(tmp)
        frames_dir.mkdir(parents=True, exist_ok=True)
        print("抽帧 ...", flush=True)
        frames = extract_frames(args.video, args.start, args.duration, args.step, args.crop, frames_dir)
        print(f"  {len(frames)} 帧", flush=True)
        texts = ocr(frames)
    cues = merge(texts, args.start + args.offset, args.step)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"video": args.video.name, "crop": args.crop, "step": args.step,
                                    "offset": args.offset, "cues": cues},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(cues)} 条字幕 → {args.out}")
    for cue in cues[:8]:
        print(f"  {cue['start']:7.2f}-{cue['end']:7.2f}  {cue['text']}")


if __name__ == "__main__":
    main()
