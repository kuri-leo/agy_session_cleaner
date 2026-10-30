import os
import re
import sys
import sqlite3
import shutil
import stat
import argparse
import json
import pathlib

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from path_utils import find_available_targets

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SQL_CHUNK = 500
TARGET_ALIASES = {"ide": "antigravity-ide", "cli": "antigravity-cli"}
DISPLAY_NAMES = {
    "antigravity": "Antigravity (Main)",
    "antigravity-ide": "Antigravity IDE",
    "antigravity-cli": "Antigravity CLI",
}
WATCHED_PROCESSES = {
    "antigravity.exe",
    "antigravity ide.exe",
    "language_server.exe",
    "language_server_windows_x64.exe",
    "agy.exe",
}
# File categories removed from disk; each item is {"id", "path", "bytes", ...}.
FILE_CATEGORIES = [
    ("brain", "brain/ dirs"),
    ("conv", "conversations/*.db (+wal/shm)"),
    ("wal_shm", "standalone .db-wal/.db-shm"),
    ("implicit", "implicit/*.pb"),
    ("annotations", "annotations/*.pbtxt"),
    ("html_artifacts", "html_artifacts/ entries"),
    ("presence", "presence/*.lock"),
]


def is_uuid(name):
    return bool(UUID_RE.match(name))


def chunks(items, size=SQL_CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def get_file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def get_preview(brain_item_path):
    log_path = os.path.join(brain_item_path, ".system_generated", "logs", "transcript.jsonl")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                data = json.loads(f.readline())
                c = data.get("content", "")
                c = c.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "")
                c = c.replace("\r", " ").strip()
                return c[:45].replace("\n", " ")
        except Exception:
            pass
    return ""


def list_dir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def check_running_processes():
    """Return a list of running Antigravity processes, or None if detection failed.

    None means "unknown" and is treated like "running" by the execute gate (fail closed).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        TH32CS_SNAPPROCESS = 0x00000002
        invalid = ctypes.c_void_p(-1).value
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == invalid:
            return None
        found = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                return None
            while True:
                if entry.szExeFile.lower() in WATCHED_PROCESSES:
                    found.append(f"{entry.szExeFile} (PID {entry.th32ProcessID})")
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
        return found
    except Exception:
        return None


def load_db(db_path, protect_ids, timeout=5.0):
    """Read the summaries DB read-only and compute the active (preserved) ID set.

    Raises sqlite3.Error if the DB cannot be read.
    """
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        c = conn.cursor()
        all_db = {r[0] for r in c.execute("SELECT conversation_id FROM conversation_summaries") if r[0] is not None}
        roots = {r[0] for r in c.execute(
            "SELECT conversation_id FROM conversation_summaries "
            "WHERE parent_conversation_id = '' OR parent_conversation_id IS NULL") if r[0] is not None}
        # Match --protect-id case-insensitively and use the DB's spelling so descendants are found.
        by_lower = {cid.lower(): cid for cid in all_db}
        top = roots | {by_lower.get(p.lower(), p) for p in protect_ids}
        active = set(top)
        frontier = list(top)
        while frontier:
            nxt = []
            for ch in chunks(frontier):
                q = ("SELECT conversation_id FROM conversation_summaries WHERE parent_conversation_id IN (%s)"
                     % ",".join("?" * len(ch)))
                for (cid,) in c.execute(q, ch):
                    if cid is not None and cid not in active:
                        active.add(cid)
                        nxt.append(cid)
            frontier = nxt
        fts_counts = None
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='conversations_fts'").fetchone():
            cols = [r[1] for r in c.execute("PRAGMA table_info(conversations_fts)")]
            if "conversation_id" in cols:
                fts_counts = dict(c.execute(
                    "SELECT conversation_id, COUNT(*) FROM conversations_fts GROUP BY conversation_id").fetchall())
        return {"all_db": all_db, "top": top, "active": active, "fts_counts": fts_counts}
    finally:
        conn.close()


def _file_items(dir_path, suffix, active):
    """Files named <uuid><suffix> whose UUID is not active (active: lower-cased IDs)."""
    out = []
    for f in list_dir(dir_path):
        p = os.path.join(dir_path, f)
        stem = f[:-len(suffix)]
        if f.endswith(suffix) and is_uuid(stem) and stem.lower() not in active and os.path.isfile(p):
            out.append({"id": stem, "path": p, "bytes": get_file_size(p)})
    return out


def collect_items(base_dir, active, include_cli_implicit=False):
    items = {cat: [] for cat, _ in FILE_CATEGORIES}
    active = {a.lower() for a in active}  # every comparison below uses name.lower()

    brain_dir = os.path.join(base_dir, "brain")
    for d in list_dir(brain_dir):
        p = os.path.join(brain_dir, d)
        if is_uuid(d) and d.lower() not in active and os.path.isdir(p):
            items["brain"].append({"id": d, "path": p, "bytes": get_dir_size(p), "preview": get_preview(p)})

    conv_dir = os.path.join(base_dir, "conversations")
    names = set(list_dir(conv_dir))
    names_lower = {n.lower() for n in names}
    for f in sorted(names):
        p = os.path.join(conv_dir, f)
        if f.endswith(".db"):
            cid = f[:-3]
            if is_uuid(cid) and cid.lower() not in active and os.path.isfile(p):
                paths = [p] + [p + ext for ext in ("-wal", "-shm") if os.path.exists(p + ext)]
                items["conv"].append({"id": cid, "path": p, "paths": paths,
                                      "bytes": sum(get_file_size(x) for x in paths)})
        elif f.endswith(".db-wal") or f.endswith(".db-shm"):
            base_db = f[:-4]
            cid = base_db[:-3]
            if (base_db.lower() not in names_lower and is_uuid(cid) and cid.lower() not in active
                    and os.path.isfile(p)):
                items["wal_shm"].append({"id": cid, "path": p, "bytes": get_file_size(p)})

    cli_implicit_skipped = 0
    implicit_dir = os.path.join(base_dir, "implicit")
    if os.path.basename(os.path.normpath(base_dir)).lower() == "antigravity-cli":
        # CLI implicit/*.pb stems are not conversation IDs; skip unless explicitly opted in.
        for f in list_dir(implicit_dir):
            p = os.path.join(implicit_dir, f)
            if not (f.endswith(".pb") and os.path.isfile(p)):
                continue
            if include_cli_implicit and f[:-3].lower() not in active:
                items["implicit"].append({"id": f[:-3], "path": p, "bytes": get_file_size(p)})
            else:
                cli_implicit_skipped += 1
    else:
        items["implicit"] = _file_items(implicit_dir, ".pb", active)

    items["annotations"] = _file_items(os.path.join(base_dir, "annotations"), ".pbtxt", active)
    items["presence"] = _file_items(os.path.join(base_dir, "presence"), ".lock", active)

    html_dir = os.path.join(base_dir, "html_artifacts")
    for e in list_dir(html_dir):
        p = os.path.join(html_dir, e)
        if os.path.isdir(p):
            if is_uuid(e) and e.lower() not in active:
                items["html_artifacts"].append({"id": e, "path": p, "bytes": get_dir_size(p)})
        else:
            stem = os.path.splitext(e)[0]
            if is_uuid(stem) and stem.lower() not in active:
                items["html_artifacts"].append({"id": stem, "path": p, "bytes": get_file_size(p)})
    return items, cli_implicit_skipped


def has_session_data(base_dir):
    brain_dir = os.path.join(base_dir, "brain")
    if any(is_uuid(d) and os.path.isdir(os.path.join(brain_dir, d)) for d in list_dir(brain_dir)):
        return True
    return bool(list_dir(os.path.join(base_dir, "conversations")))


def scan_target(key, base_dir, protect_ids=(), allow_empty_db=False, include_cli_implicit=False):
    db_path = os.path.join(base_dir, "conversation_summaries.db")
    res = {
        "key": key, "name": DISPLAY_NAMES.get(key, key), "base_dir": base_dir, "db_path": db_path,
        "has_db": False, "status": "ok", "message": "", "active_top": 0, "active_sub": 0,
        "orphaned_db": set(), "fts_counts": None, "fts_orphan_rows": 0, "fts_stale_rows": 0,
        "items": {cat: [] for cat, _ in FILE_CATEGORIES}, "cli_implicit_skipped": 0,
    }
    protect = {p.strip() for p in (protect_ids or ()) if p and p.strip()}
    res["protect"] = protect

    def refuse(reason):
        if has_session_data(base_dir):
            res["status"] = "error"
            res["message"] = reason + " Aborting this target to prevent data loss (override: --allow-empty-db)."
        else:
            res["status"] = "skipped"
            res["message"] = "no data (" + reason.rstrip(".").lower() + ", brain/ and conversations/ are empty)"
        return res

    if os.path.isfile(db_path):
        try:
            info = load_db(db_path, protect)
        except sqlite3.Error as e:
            res["status"] = "error"
            res["message"] = f"Cannot read database {db_path}: {e}. Aborting this target."
            return res
        res["has_db"] = True
        if not info["all_db"] and not allow_empty_db:
            return refuse("Database contains 0 sessions.")
        active, top = info["active"], info["top"]
        res["orphaned_db"] = info["all_db"] - active
        res["fts_counts"] = info["fts_counts"]
        if info["fts_counts"] is not None:
            for cid, n in info["fts_counts"].items():
                if cid in res["orphaned_db"]:
                    res["fts_orphan_rows"] += n
                elif cid is not None and cid not in info["all_db"]:
                    res["fts_stale_rows"] += n
    else:
        if not allow_empty_db:
            return refuse("Database not found.")
        active, top = protect, protect

    res["items"], res["cli_implicit_skipped"] = collect_items(base_dir, active, include_cli_implicit)
    res["active_top"] = len(top)
    res["active_sub"] = len(active - top)
    return res


def total_bytes(res):
    return sum(item["bytes"] for cat, _ in FILE_CATEGORIES for item in res["items"][cat])


def _retry_writable(func, path, exc):
    """rmtree error handler: clear a read-only attribute and retry once; otherwise re-raise."""
    if isinstance(exc, tuple):  # onerror (Python < 3.12) passes exc_info
        exc = exc[1]
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


def delete_path(p):
    """Permanently delete a file or directory. Returns None on success, else an error string."""
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            if sys.version_info >= (3, 12):
                shutil.rmtree(p, onexc=_retry_writable)
            else:
                shutil.rmtree(p, onerror=_retry_writable)
        else:
            try:
                os.remove(p)
            except PermissionError as e:
                _retry_writable(os.remove, p, e)
        return None
    except FileNotFoundError:
        return None
    except OSError as e:
        return str(e)


def revalidate(res, timeout=5.0):
    """Re-read the DB just before deleting; drop items/rows whose ID has become active since the scan.

    Returns (res, n_dropped). Raises sqlite3.Error if the DB can no longer be read.
    """
    if not res["has_db"]:
        return res, 0
    active = {a.lower() for a in load_db(res["db_path"], res.get("protect", ()), timeout)["active"]}
    items = {cat: [i for i in lst if i["id"].lower() not in active] for cat, lst in res["items"].items()}
    orphaned = {o for o in res["orphaned_db"] if o.lower() not in active}
    dropped = (sum(len(v) for v in res["items"].values()) - sum(len(v) for v in items.values())
               + len(res["orphaned_db"]) - len(orphaned))
    return dict(res, items=items, orphaned_db=orphaned), dropped


def perform_clean(res, db_timeout=10.0):
    print(f"\n[EXECUTE] Cleaning {res['name']}...")
    out = {"deleted": {cat: 0 for cat, _ in FILE_CATEGORIES}, "failed": [], "bytes": 0,
           "db_rows": 0, "fts_rows": 0, "db_rows_kept": 0, "db_error": None, "revived": 0}
    try:
        res, out["revived"] = revalidate(res, db_timeout)
    except sqlite3.Error as e:
        out["db_error"] = str(e)
        print(f"  [ERROR] Cannot re-read {res['db_path']} before deleting: {e}. Nothing deleted for this target.")
        return out
    if out["revived"]:
        print(f"  [INFO] {out['revived']} item(s) became active since the scan and were kept.")
    failed_ids = set()
    for cat, _ in FILE_CATEGORIES:
        for item in res["items"][cat]:
            ok = True
            for p in item.get("paths", [item["path"]]):
                size = get_dir_size(p) if os.path.isdir(p) else get_file_size(p)
                err = delete_path(p)
                if err:
                    print(f"  [WARN] Could not delete {p}: {err}. Skipped.")
                    out["failed"].append((p, err))
                    ok = False
                    break  # e.g. keep -wal/-shm when the .db itself is locked
                out["bytes"] += size
            if ok:
                out["deleted"][cat] += 1
            else:
                failed_ids.add(item["id"])

    orphaned = res["orphaned_db"]
    if res["has_db"] and (orphaned or res["fts_stale_rows"]):
        # Keep DB rows for IDs whose files could not be removed, so a later run can retry.
        failed_lower = {f.lower() for f in failed_ids}
        ids = sorted(o for o in orphaned if o.lower() not in failed_lower)
        out["db_rows_kept"] = len(orphaned) - len(ids)
        has_fts = res["fts_counts"] is not None
        try:
            conn = sqlite3.connect(res["db_path"], timeout=db_timeout)
            try:
                c = conn.cursor()
                try:
                    for ch in chunks(ids):
                        ph = ",".join("?" * len(ch))
                        out["db_rows"] += c.execute(
                            f"DELETE FROM conversation_summaries WHERE conversation_id IN ({ph})", ch).rowcount
                        if has_fts:
                            out["fts_rows"] += c.execute(
                                f"DELETE FROM conversations_fts WHERE conversation_id IN ({ph})", ch).rowcount
                    if has_fts:
                        out["fts_rows"] += c.execute(
                            "DELETE FROM conversations_fts WHERE conversation_id NOT IN "
                            "(SELECT conversation_id FROM conversation_summaries "
                            "WHERE conversation_id IS NOT NULL)").rowcount
                    conn.commit()
                except sqlite3.Error:
                    conn.rollback()
                    out["db_rows"] = out["fts_rows"] = 0
                    raise
                if out["db_rows"] or out["fts_rows"]:
                    try:
                        conn.execute("VACUUM")
                    except sqlite3.Error as e:
                        print(f"  [WARN] VACUUM skipped: {e}")
            finally:
                conn.close()
        except sqlite3.Error as e:
            out["db_error"] = str(e)
            print(f"  [WARN] Database update failed for {res['db_path']}: {e}. DB rows left unchanged.")
    return out


def _mb(n):
    return f"{n / (1024 * 1024):.2f} MB"


def has_work(res):
    return res["status"] == "ok" and bool(any(res["items"][c] for c, _ in FILE_CATEGORIES)
                                          or res["orphaned_db"] or res["fts_stale_rows"])


def print_report(results):
    print("=" * 65)
    print("           Antigravity Session Cleanup Report")
    print("=" * 65)
    for res in results:
        print(f"\nTarget: {res['name']} ({res['base_dir']})")
        if res["status"] == "skipped":
            print(f"  [SKIPPED] {res['message']}")
            continue
        if res["status"] == "error":
            print(f"  [ERROR] {res['message']}")
            continue
        if not res["has_db"]:
            print("  [WARN] No database; running with --allow-empty-db (only --protect-id IDs are preserved).")
        def row(label, value):
            print(f"  {label + ':':<44}{value}")

        row("Active top-level sessions (kept)", res["active_top"])
        row("Active subagent sessions (kept)", res["active_sub"])
        row("Orphaned DB rows to remove", len(res["orphaned_db"]))
        if res["fts_counts"] is None:
            row("Orphaned FTS rows to remove", "n/a (no conversations_fts)")
        else:
            row("Orphaned FTS rows to remove", f"{res['fts_orphan_rows'] + res['fts_stale_rows']}"
                f" ({res['fts_stale_rows']} without any summary row)")
        for cat, label in FILE_CATEGORIES:
            items = res["items"][cat]
            row(label + " to remove", f"{len(items)} ({_mb(sum(i['bytes'] for i in items))})")
        if res["cli_implicit_skipped"]:
            row("CLI implicit/*.pb kept", f"{res['cli_implicit_skipped']} (use --include-cli-implicit to delete)")
        brain = res["items"]["brain"]
        if brain:
            print("  brain/ preview:")
            for item in brain[:5]:
                print(f"    - [{item['id'][:8]}] ({item['bytes'] / 1024:.1f} KB) {item['preview']}")
            if len(brain) > 5:
                print(f"    ... and {len(brain) - 5} more")
    total = sum(total_bytes(r) for r in results if r["status"] == "ok")
    print("\n" + "=" * 65)
    print(f"Total reclaimable disk space: {_mb(total)} (permanent deletion; VACUUM savings not included)")
    print("=" * 65)


def build_parser():
    parser = argparse.ArgumentParser(description="Antigravity / IDE / CLI session cleaner")
    parser.add_argument("--app-data", "-p",
                        help="Data directory: one target dir (e.g. ~/.gemini/antigravity) or a parent containing them")
    parser.add_argument("--target", "-t", default="all",
                        choices=["all", "antigravity", "ide", "antigravity-ide", "cli", "antigravity-cli"],
                        help="Target to clean (default: all)")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default: dry-run report only)")
    parser.add_argument("--protect-id", "-k", action="append", default=[],
                        help="Conversation ID to keep (its subagents are kept too); repeatable")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the interactive confirmation prompt")
    parser.add_argument("--force", action="store_true",
                        help="Execute even if Antigravity processes are running (or detection failed)")
    parser.add_argument("--allow-empty-db", action="store_true",
                        help="Proceed when conversation_summaries.db is missing or empty "
                             "(everything not --protect-id'd is treated as orphaned)")
    parser.add_argument("--include-cli-implicit", action="store_true",
                        help="Also delete antigravity-cli implicit/*.pb files (skipped by default)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        all_targets = find_available_targets(args.app_data)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1

    resolved = TARGET_ALIASES.get(args.target, args.target)
    if resolved == "all":
        keys = list(all_targets)
    elif resolved in all_targets:
        keys = [resolved]
    else:
        print(f"[ERROR] Target '{resolved}' not found. Detected targets: {', '.join(all_targets) or 'none'}")
        return 1
    if not keys:
        print("[ERROR] No Antigravity data directory found.")
        return 1

    results = [scan_target(k, all_targets[k], args.protect_id, args.allow_empty_db, args.include_cli_implicit)
               for k in keys]
    print_report(results)
    had_error = any(r["status"] == "error" for r in results)

    if not args.execute:
        print("\n[DRY RUN] Nothing was deleted.")
        print("To clean all:       uv run clean_sessions.py --execute")
        print("To clean IDE only:  uv run clean_sessions.py --target ide --execute")
        return 1 if had_error else 0

    work = [r for r in results if has_work(r)]
    if not work:
        print("\nNothing to clean.")
        return 1 if had_error else 0

    running = check_running_processes()
    if running is None or running:
        if running is None:
            print("\n[WARN] Could not determine whether Antigravity processes are running.")
        else:
            print("\n[WARN] Antigravity processes are running: " + ", ".join(running))
        if not args.force:
            print("[ERROR] Refusing to delete. Close Antigravity (IDE/CLI/language servers) or pass --force.")
            return 1
        print("[WARN] --force given; continuing anyway.")

    if not args.yes:
        try:
            answer = input("\nPermanently delete the items listed above? This cannot be undone. [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was deleted.")
            return 1

    freed, failures = 0, 0
    for res in work:
        out = perform_clean(res)
        freed += out["bytes"]
        failures += len(out["failed"]) + (1 if out["db_error"] else 0)
        deleted = ", ".join(f"{label}: {out['deleted'][cat]}" for cat, label in FILE_CATEGORIES)
        print(f"  Deleted -> {deleted}")
        print(f"  DB rows removed: {out['db_rows']}, FTS rows removed: {out['fts_rows']}"
              + (f", DB rows kept because files could not be deleted: {out['db_rows_kept']}"
                 if out["db_rows_kept"] else ""))
    print(f"\nFreed {_mb(freed)} (permanently deleted). Failures: {failures}.")
    return 1 if (had_error or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
