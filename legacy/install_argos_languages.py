import threading
import time
from pathlib import Path

import argostranslate.package
import argostranslate.translate


def main() -> None:
    wanted = {("ja", "en"), ("en", "zh")}
    print("Updating Argos package index...")
    try:
        argostranslate.package.update_package_index()
    except Exception as exc:
        print(f"Index update failed, continuing with cached index: {exc}")
    packages = argostranslate.package.get_available_packages()
    selected = [p for p in packages if (p.from_code, p.to_code) in wanted]
    if len(selected) != len(wanted):
        found = sorted({(p.from_code, p.to_code) for p in packages})
        raise SystemExit(f"Missing language package; available matches: {found}")
    cache = Path(__file__).resolve().parents[1] / "argos_packages"
    cache.mkdir(exist_ok=True)
    installed = set()
    for language in argostranslate.translate.get_installed_languages():
        for translation in language.translations_to:
            installed.add((translation.from_lang.code, translation.to_lang.code))
    for package in selected:
        if (package.from_code, package.to_code) in installed:
            print(f"Skipping installed {package.from_code}->{package.to_code}")
            continue
        print(f"Downloading {package.from_code}->{package.to_code}...")
        result = {}
        error = {}

        def download() -> None:
            try:
                result["path"] = package.download()
            except Exception as exc:
                error["exception"] = exc

        worker = threading.Thread(target=download, daemon=True)
        worker.start()
        started = time.monotonic()
        while worker.is_alive():
            worker.join(timeout=10)
            if worker.is_alive():
                elapsed = int(time.monotonic() - started)
                print(f"Still downloading {package.from_code}->{package.to_code}: {elapsed}s elapsed...", flush=True)
        if "exception" in error:
            raise error["exception"]
        path = result["path"]
        print(f"Installing {path}")
        argostranslate.package.install_from_path(path)
    print("Language packages installed.")


if __name__ == "__main__":
    main()
