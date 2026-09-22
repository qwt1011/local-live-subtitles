"""模型下载与解包。

评审把"模型资产管理靠手工"列为问题之一（ARCHITECTURE_REVIEW.md S7），
这个脚本是那一条的最小修复：把下载源、校验目标路径、解包位置都写进代码。

**为什么需要镜像列表**：实测 GitHub Releases 直连只有 ~2 KB/s（158 MB 要 20 小时以上），
`ghproxy.net` 47 KB/s，而 `gh-proxy.com` 776 KB/s、`hf-mirror.com` 669 KB/s、
ModelScope 4.1 MB/s。Argos ja->en 当年下载失败就是这个原因，
所以这里按速度排序逐个尝试，并且带失速检测（而不是无限等下去）。

用法：
    python tools/setup_models.py --list
    python tools/setup_models.py --engine sensevoice
    python tools/setup_models.py --engine sensevoice --url <自定义地址>
"""

import argparse
import socket
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models_catalog import CATALOG, GITHUB, MODELS_DIR, is_ready  # noqa: E402

# 按实测速度排序（见模块 docstring）。
MIRRORS = [
    "https://gh-proxy.com/{url}",
    "https://hf-mirror.com/{hf}",
    "https://ghproxy.net/{url}",
    "{url}",
]


def show_catalog():
    for name, entry in CATALOG.items():
        ready = "已就绪" if is_ready(entry) else "未安装"
        print(f"{name}  [{ready}]")
        print(f"  archive  : {entry['archive']}")
        print(f"  dir      : models/{entry['dir']}")
        print(f"  languages: {entry['languages']}")
        print(f"  note     : {entry['note']}")


def candidate_urls(entry, override=None):
    if override:
        return [override]
    direct = f"{GITHUB}/{entry['archive']}"
    hf = f"https://huggingface.co/{entry['hf']}/resolve/main/{entry['archive']}"
    seen = []
    for pattern in MIRRORS:
        url = pattern.format(url=direct, hf=hf)
        if url not in seen:
            seen.append(url)
    return seen


def download_once(url, target, stall_timeout=30, label=""):
    """下载单个 URL。超过 stall_timeout 没有任何数据就抛异常，交给下一个镜像。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "asmr-transcription-setup"})
    total = 0
    last_report = 0
    with urllib.request.urlopen(request, timeout=stall_timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        with target.open("wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
                total += len(block)
                if total - last_report >= (8 << 20):
                    last_report = total
                    if declared:
                        print(f"\r  {label} {total / 1048576:6.1f}/{declared / 1048576:.1f} MB", end="", flush=True)
                    else:
                        print(f"\r  {label} {total / 1048576:6.1f} MB", end="", flush=True)
    print()
    return total


def download(urls, target):
    partial = target.with_suffix(target.suffix + ".part")
    errors = []
    for index, url in enumerate(urls, start=1):
        for attempt in range(2):
            try:
                print(f"[{index}/{len(urls)}] {url}")
                written = download_once(url, partial, label=f"try{attempt + 1}")
                if written == 0:
                    raise IOError("下载到 0 字节")
                partial.replace(target)
                return
            except (urllib.error.URLError, socket.timeout, TimeoutError, IOError, OSError) as exc:
                errors.append(f"{url} -> {type(exc).__name__}: {exc}")
                print(f"  失败：{type(exc).__name__}: {exc}")
                partial.unlink(missing_ok=True)
    raise SystemExit("所有镜像都失败：\n  " + "\n  ".join(errors))


def install(name, override_url=None):
    entry = CATALOG[name]
    if is_ready(entry):
        print(f"{name} 已就绪：models/{entry['dir']}")
        return
    archive = MODELS_DIR / entry["archive"]
    if not archive.is_file():
        download(candidate_urls(entry, override_url), archive)
    else:
        print(f"已有压缩包，跳过下载：{archive.name}")
    print(f"解包 {archive.name}")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(MODELS_DIR)
    archive.unlink()
    if not is_ready(entry):
        raise SystemExit(f"解包后仍未找到 {entry['dir']}/{entry['model']}")
    print(f"完成：models/{entry['dir']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=sorted(CATALOG))
    parser.add_argument("--url", default=None, help="覆盖下载地址（单个 URL，不走镜像列表）")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list or not args.engine:
        show_catalog()
        return
    install(args.engine, args.url)


if __name__ == "__main__":
    main()
