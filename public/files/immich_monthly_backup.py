#!/usr/bin/env python3
"""
immich_monthly_backup.py
------------------------
Back up Immich originals to S3 as one zip per user per month, uploading only
the folders that have changed since the last run.

Immich's storage template files every original into:

    <library>/<user>/YYYY-MM/<files>

Each run scans every <user>/<YYYY-MM> folder, computes a cheap fingerprint
from file metadata (path + size + mtime -- no file contents are read, so it
is fast even for large folders), and compares it to the fingerprint stored
from the previous run. Changed or new folders are zipped and uploaded to:

    s3://<bucket>/<s3-prefix><user>/<YYYY-MM>.zip

Unchanged folders are skipped. So the first run uploads everything; after
that each run only re-uploads months where photos were added. No month
targeting or "last month" logic required -- a frozen account's old months
simply never change and are skipped.

Optional --user / --month filters restrict a manual run. --force re-uploads
regardless of fingerprint. --dry-run shows what would happen without uploading.

Needs: awscli configured with the given profile. No other dependencies.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def which(name):
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Discover user/month folders and fingerprint them
# ---------------------------------------------------------------------------

def iter_month_dirs(library_root, user_filter=None, month_filter=None):
    """Yield (user, 'YYYY-MM', Path) for every month folder under the library."""
    for user_dir in sorted(library_root.iterdir()):
        if not user_dir.is_dir():
            continue
        if user_filter and user_dir.name != user_filter:
            continue
        for month_dir in sorted(user_dir.iterdir()):
            if not month_dir.is_dir() or not MONTH_RE.match(month_dir.name):
                continue
            if month_filter and month_dir.name != month_filter:
                continue
            yield user_dir.name, month_dir.name, month_dir


def fingerprint(folder):
    """Fast fingerprint of a folder from file metadata only (no content read).

    Returns (hexdigest, file_count, total_bytes).
    """
    h = hashlib.sha256()
    count = 0
    size = 0
    for p in sorted(folder.rglob("*"), key=lambda x: str(x)):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(folder).as_posix()
        h.update(rel.encode("utf-8", "replace"))
        h.update(str(st.st_size).encode())
        h.update(str(int(st.st_mtime)).encode())
        count += 1
        size += st.st_size
    return h.hexdigest(), count, size


def build_zip(folder, zip_path, arc_prefix):
    """Zip a folder's files (no compression) with arcnames <arc_prefix>/<rel>."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED,
                         allowZip64=True) as zf:
        for p in sorted(folder.rglob("*"), key=lambda x: str(x)):
            if p.is_file():
                rel = p.relative_to(folder).as_posix()
                zf.write(p, f"{arc_prefix}/{rel}")


# ---------------------------------------------------------------------------
# S3 via awscli
# ---------------------------------------------------------------------------

def s3_upload(zip_path, bucket, key, profile, aws_bin, dry_run):
    cmd = [aws_bin, "s3", "cp", str(zip_path), f"s3://{bucket}/{key}",
           "--profile", profile]
    if dry_run:
        log("DRY-RUN upload: " + " ".join(cmd))
        return
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"aws upload failed:\n{proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# State (fingerprints from the previous run)
# ---------------------------------------------------------------------------

def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library-path",
                    default="/opt/immich-app/library/library",
                    help="Immich library folder containing the per-user account "
                         "dirs. Default: /opt/immich-app/library/library")
    ap.add_argument("--user", default=None,
                    help="Only process this user account folder. Default: all.")
    ap.add_argument("--month", default=None,
                    help="Only process this month (YYYY-MM). Default: all.")
    ap.add_argument("--bucket", default="bucket-immich-backup",
                    help="S3 bucket. Default: bucket-immich-backup")
    ap.add_argument("--s3-prefix", default="",
                    help="Key prefix inside the bucket, e.g. 'immich/'. Default: none")
    ap.add_argument("--profile", default="profile-immich-backup",
                    help="AWS CLI profile. Default: profile-immich-backup")
    ap.add_argument("--aws-bin", default="aws",
                    help="Path to aws binary (use full path in cron). Default: aws")
    ap.add_argument("--workdir", default=tempfile.gettempdir(),
                    help="Where to build temporary zips. Default: system temp")
    ap.add_argument("--state-file",
                    default=str(Path(__file__).resolve().parent /
                                ".immich_backup_state.json"),
                    help="Where fingerprints are stored between runs. "
                         "Default: alongside this script.")
    ap.add_argument("--force", action="store_true",
                    help="Upload every folder regardless of fingerprint.")
    ap.add_argument("--keep-local-zip", action="store_true",
                    help="Keep built zips in --workdir instead of deleting them.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan and report, but do not upload or update state.")
    args = ap.parse_args()

    root = Path(args.library_path).expanduser().resolve()
    if not root.is_dir():
        log(f"ERROR: library path does not exist: {root}")
        return 2

    if args.aws_bin == "aws" and not which("aws") and not args.dry_run:
        log("ERROR: 'aws' not found on PATH. Install awscli or pass --aws-bin.")
        return 2

    state = load_state(args.state_file)
    log(f"Library: {root}")
    log(f"State file: {args.state_file} ({len(state)} folders recorded)")

    uploaded = skipped = failed = 0
    uploaded_bytes = 0

    for user, month, folder in iter_month_dirs(root, args.user, args.month):
        key_id = f"{user}/{month}"
        fp, count, size = fingerprint(folder)
        if count == 0:
            continue

        if not args.force and state.get(key_id) == fp:
            skipped += 1
            continue

        key = f"{args.s3_prefix}{user}/{month}.zip"
        log(f"CHANGED {key_id}: {count} files, {size/1_048_576:.1f} MiB "
            f"-> s3://{args.bucket}/{key}")
        zip_path = Path(args.workdir) / f"{user}_{month}.zip"
        try:
            build_zip(folder, zip_path, f"{user}/{month}")
            s3_upload(zip_path, args.bucket, key, args.profile,
                      args.aws_bin, args.dry_run)
            if not args.dry_run:
                state[key_id] = fp
            uploaded += 1
            uploaded_bytes += size
        except Exception as e:
            log(f"{key_id}: FAILED -- {e}")
            failed += 1
        finally:
            if zip_path.exists() and not args.keep_local_zip:
                zip_path.unlink(missing_ok=True)

    if not args.dry_run:
        save_state(args.state_file, state)

    log(f"Done. uploaded={uploaded} ({uploaded_bytes/1_048_576:.1f} MiB), "
        f"skipped={skipped}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
