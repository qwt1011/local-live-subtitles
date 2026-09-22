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

from app.models_catalog import CATALOG, GITHUB, HF_MIRRORS, MODELS_DIR, is_ready  # noqa: E402

# 按实测速度排序（见模块 docstring）。
MIRRORS = [
    "https://gh-proxy.com/{url}",
    "https://hf-mirror.com/{hf}",
    "https://ghproxy.net/{url}",
    "{url}",
]


def human(size):
    return f"{size / 1048576:.1f} MB" if size else "?"


def show_catalog():
    for name, entry in CATALOG.items():
        ready = "已就绪" if is_ready(entry) else "未安装"
        print(f"{name}  [{ready}]")
        if entry.get("kind") == "hf":
            print(f"  repo     : {entry['repo']}")
            print(f"  files    : {', '.join(entry['files'])}")
        else:
            print(f"  archive  : {entry['archive']}")
        print(f"  dir      : models/{entry['dir']}")
        print(f"  languages: {entry['languages']}")
        print(f"  note     : {entry['note']}")
        print()


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


def hf_urls(repo, name):
    return [pattern.format(repo=repo, file=name) for pattern in HF_MIRRORS]


def download_once(url, target, stall_timeout=60, label=""):
    """下载单个 URL，支持断点续传。

    2.35GB 的单个权重文件在不稳定的链路上必须能续传——huggingface_hub 自带的
    xet 传输在这个网络下一小时只走了 86% 就报
    `CAS Client Error: Format error: I/O error`，而且没有可用的续传残留。
    超过 stall_timeout 没有任何数据就抛异常，交给下一个镜像。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    resume_from = target.stat().st_size if target.is_file() else 0
    headers = {"User-Agent": "asmr-transcription-setup"}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=stall_timeout) as response:
        if resume_from and getattr(response, "status", 200) != 206:
            # 服务器不支持续传，只能从头来。
            print(f"  {label} 服务端不支持续传（status={response.status}），重新下载")
            resume_from = 0
        declared = int(response.headers.get("Content-Length") or 0) + resume_from

        written = resume_from
        last_report = written
        mode = "ab" if resume_from else "wb"
        with target.open(mode) as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if written - last_report >= (16 << 20):
                    last_report = written
                    if declared:
                        print(f"\r  {label} {written / 1048576:7.1f}/{declared / 1048576:.1f} MB",
                              end="", flush=True)
                    else:
                        print(f"\r  {label} {written / 1048576:7.1f} MB", end="", flush=True)
    print()
    if declared and written < declared:
        raise IOError(f"提前结束：{written}/{declared} 字节")
    return written


def download(urls, target):
    partial = target.with_suffix(target.suffix + ".part")
    errors = []
    for index, url in enumerate(urls, start=1):
        # 同一个文件在不同镜像上内容相同，所以 .part 可以跨镜像续传。
        for attempt in range(3):
            try:
                print(f"[{index}/{len(urls)}] {url}"
                      + (f"（续传，已有 {partial.stat().st_size / 1048576:.1f} MB）"
                         if partial.is_file() and partial.stat().st_size else ""))
                written = download_once(url, partial, label=f"try{attempt + 1}")
                if written == 0:
                    raise IOError("下载到 0 字节")
                partial.replace(target)
                return
            except (urllib.error.URLError, socket.timeout, TimeoutError, IOError, OSError) as exc:
                errors.append(f"{url} -> {type(exc).__name__}: {exc}")
                print(f"  失败：{type(exc).__name__}: {exc}（保留已下载部分）")
    raise SystemExit("所有镜像都失败：\n  " + "\n  ".join(errors[-6:]))


def install_hf(entry, override_url=None):
    """按文件下载 HuggingFace 仓库（用于已经转换好的 CTranslate2 模型）。"""
    directory = MODELS_DIR / entry["dir"]
    directory.mkdir(parents=True, exist_ok=True)
    for name in entry["files"]:
        target = directory / name
        if target.is_file() and target.stat().st_size > 0:
            print(f"跳过已存在：{name}")
            continue
        urls = [override_url] if override_url else hf_urls(entry["repo"], name)
        print(f"下载 {name}")
        download(urls, target)
    missing = [name for name in entry["required"] if not (directory / name).is_file()]
    if missing:
        raise SystemExit(f"缺少必需文件：{missing}")
    print(f"完成：models/{entry['dir']}")


def install(name, override_url=None):
    entry = CATALOG[name]
    if is_ready(entry):
        print(f"{name} 已就绪：models/{entry['dir']}")
        return
    if entry.get("kind") == "hf":
        install_hf(entry, override_url)
        return

    archive = MODELS_DIR / entry["archive"]
    if not archive.is_file():
        download(candidate_urls(entry, override_url), archive)
    else:
        print(f"已有压缩包，跳过下载：{archive.name}")
    print(f"解包 {archive.name}")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(MODELS_DIR, filter="data")
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
