#!/usr/bin/env python3
"""
clean_output.py — usuwa pliki z output/, strategy_wyckoff_9_4/ i strategy_wyckoff_9_5/.

Użycie:
  python clean_output.py           # usuwa wszystko
  python clean_output.py --dry-run # tylko pokazuje co zostanie usunięte
  python clean_output.py --cache   # usuwa tylko cache (JSON), nie raporty HTML
"""

import os
import sys
import glob

_ROOT = os.path.dirname(os.path.abspath(__file__))

TARGETS = [
    {"dir": os.path.join(_ROOT, "output"),                              "patterns": ["*.html", "*.json", "*.zip"], "label": "output/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_4", "output"),     "patterns": ["*.html", "*.json", "*.zip"], "label": "strategy_wyckoff_9_4/output/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_4", "results"),    "patterns": ["*.html"],                    "label": "strategy_wyckoff_9_4/results/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_4", "cache"),      "patterns": ["*.json"],                    "label": "strategy_wyckoff_9_4/cache/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_5", "output"),     "patterns": ["*.html", "*.json", "*.zip"], "label": "strategy_wyckoff_9_5/output/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_5", "results"),    "patterns": ["*.html"],                    "label": "strategy_wyckoff_9_5/results/"},
    {"dir": os.path.join(_ROOT, "strategy_wyckoff_9_5", "cache"),      "patterns": ["*.json"],                    "label": "strategy_wyckoff_9_5/cache/"},
    {"dir": os.path.join(_ROOT, "research", "cache"),                   "patterns": ["*.json"],                    "label": "research/cache/"},
    {"dir": os.path.join(_ROOT, "research", "output"),                  "patterns": ["*.html", "*.json"],          "label": "research/output/"},
]

CACHE_ONLY_DIRS = {"strategy_wyckoff_9_4/cache/", "strategy_wyckoff_9_5/cache/", "research/cache/"}


def collect_files(cache_only=False):
    found = []
    for t in TARGETS:
        if cache_only and t["label"] not in CACHE_ONLY_DIRS:
            continue
        if not os.path.isdir(t["dir"]):
            continue
        for pat in t["patterns"]:
            for f in glob.glob(os.path.join(t["dir"], pat)):
                found.append((t["label"], f))
    return found


def main():
    dry_run    = "--dry-run" in sys.argv
    cache_only = "--cache"   in sys.argv

    files = collect_files(cache_only)

    if not files:
        print("Brak plików do usunięcia.")
        return

    scope = "cache JSON" if cache_only else "wszystkie outputy"
    mode  = "[DRY RUN] " if dry_run else ""
    print(f"{mode}Zakres: {scope}\n")

    by_dir: dict = {}
    for label, path in files:
        by_dir.setdefault(label, []).append(path)

    total_size = 0
    for label, paths in sorted(by_dir.items()):
        size = sum(os.path.getsize(p) for p in paths)
        total_size += size
        print(f"  {label}  ({len(paths)} plików, {size / 1024:.0f} KB)")
        for p in sorted(paths):
            print(f"    {os.path.basename(p)}")

    print(f"\nRazem: {len(files)} plików, {total_size / 1024:.0f} KB")

    if dry_run:
        print("\n(dry run — nic nie usunięto)")
        return

    confirm = input("\nUsunąć? [t/N]: ").strip().lower()
    if confirm != "t":
        print("Anulowano.")
        return

    removed = 0
    for _, path in files:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            print(f"  BŁĄD: {path} — {e}")

    print(f"\nUsunięto {removed}/{len(files)} plików.")


if __name__ == "__main__":
    main()
