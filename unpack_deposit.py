#!/usr/bin/env python3
"""Unpack the Zenodo deposit into the layout expected by the collection/QA scripts.

    python unpack_deposit.py --deposit path/to/downloaded/zenodo/files

Creates `_chain/multichain/` and `_enrich/{multichain,mint,reputation,coding}/`
next to this file. Existing files in those trees are overwritten.
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ZIPS = {
    "census_mc_t1.zip": ROOT / "_chain" / "multichain",
    "census_mc_t2.zip": ROOT / "_chain" / "multichain",
    "panels_mc_t1.zip": ROOT / "_enrich" / "multichain",
    "panels_mc_t2.zip": ROOT / "_enrich" / "multichain" / "mc_t2",
    "mint_mc_t1.zip": ROOT / "_enrich" / "mint",
    "mint_mc_t2.zip": ROOT / "_enrich" / "mint",
    "reputation_mc_t1.zip": ROOT / "_enrich" / "reputation" / "mc_t1",
    "reputation_mc_t2.zip": ROOT / "_enrich" / "reputation" / "mc_t2",
    "coding.zip": ROOT / "_enrich" / "coding",
}

OPTIONAL_ZIPS = {
    "certainty_coding.zip": ROOT / "_enrich" / "coding" / "certainty",
}

OPTIONAL_LOOSE = {
    "p2_results.json": ROOT / "_enrich" / "multichain" / "mc_t3p" / "p2_results.json",
}

# Run-level files live at the reputation zip root but belong one level up.
REPUTATION_LIFT = ("run_reputation_mc_t1.json", "run_reputation_mc_t2.json", "deployment_check.json")


def unpack(deposit: Path) -> None:
    missing = [n for n in ZIPS if not (deposit / n).is_file()]
    if missing:
        raise SystemExit("deposit folder is missing: " + ", ".join(missing))
    for name, dest in ZIPS.items():
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(deposit / name) as z:
            z.extractall(dest)
            print(f"  {name} -> {dest.relative_to(ROOT)} ({len(z.namelist())} files)")
        if name.startswith("reputation_"):
            for fn in REPUTATION_LIFT:
                src = dest / fn
                if src.is_file():
                    src.replace(ROOT / "_enrich" / "reputation" / fn)
    for name, dest in OPTIONAL_ZIPS.items():
        if (deposit / name).is_file():
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(deposit / name) as z:
                z.extractall(dest)
                print(f"  {name} -> {dest.relative_to(ROOT)} ({len(z.namelist())} files)")
    for name, dest in OPTIONAL_LOOSE.items():
        src = deposit / name
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            print(f"  {name} -> {dest.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deposit", type=Path, required=True, help="folder with the downloaded Zenodo v2.0.0 deposit files")
    args = ap.parse_args()
    unpack(args.deposit.resolve())
    print("done. next: python _enrich/scripts/mc_qa_audit.py --label mc_t2 --cards 0")


if __name__ == "__main__":
    main()
