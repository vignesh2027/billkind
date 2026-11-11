"""Back up the database and the uploaded bill documents.

Usage: python -m scripts.backup_db [--dir BACKUP_DIR] [--keep N]

Writes billkind-YYYYmmdd-HHMM.db (an online SQLite backup, consistent while the app is running, WAL included) and
uploads-YYYYmmdd-HHMM.tar.gz next to it, then keeps only the newest N of each. BACKUP_DIR defaults to a backups
folder beside the database. Copy the folder off the machine (for example to object storage) to survive the loss
of the disk.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import tarfile
from pathlib import Path

from app.config import settings


def backup(dest: Path, keep: int = 14, now: dt.datetime | None = None) -> tuple[Path, Path | None]:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M")
    db_out = dest / f"billkind-{stamp}.db"
    src = sqlite3.connect(settings.db_path)
    out = sqlite3.connect(db_out)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    uploads = Path(settings.upload_dir)
    tar_out = None
    if uploads.is_dir():
        tar_out = dest / f"uploads-{stamp}.tar.gz"
        with tarfile.open(tar_out, "w:gz") as tar:
            tar.add(uploads, arcname="uploads")
    for pattern in ("billkind-*.db", "uploads-*.tar.gz"):
        for old in sorted(dest.glob(pattern))[:-keep]:
            old.unlink()
    return db_out, tar_out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("BACKUP_DIR") or str(Path(settings.db_path).parent / "backups"))
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args()
    db_out, tar_out = backup(Path(args.dir), args.keep)
    print(f"backed up {db_out}" + (f" and {tar_out}" if tar_out else ""))


if __name__ == "__main__":
    main()
