import os
import sys
import sqlite3
import shutil
import argparse
import json

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
from path_utils import find_available_targets

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
                c = data.get("content", "").replace("<USER_REQUEST>", "").replace("\r", " ").strip()
                return c[:45].replace("\n", " ")
        except Exception:
            pass
    return ""

def scan_target(target_name, base_dir, protect_ids=None):
    db_path = os.path.join(base_dir, "conversation_summaries.db")
    conv_dir = os.path.join(base_dir, "conversations")
    brain_dir = os.path.join(base_dir, "brain")
    implicit_dir = os.path.join(base_dir, "implicit")

    active_top, active_all, orphaned_db_rows = set(), set(), set()
    has_db = os.path.exists(db_path)

    if has_db:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        active_top = set(r[0] for r in c.execute("SELECT conversation_id FROM conversation_summaries WHERE parent_conversation_id = ''").fetchall())
        if protect_ids:
            active_top.update(protect_ids)
        active_all = set(active_top)
        added = True
        while added:
            parents = tuple(active_all)
            new_sub = set(r[0] for r in c.execute(f"SELECT conversation_id FROM conversation_summaries WHERE parent_conversation_id IN ({','.join('?' for _ in parents)})", list(parents)).fetchall())
            before = len(active_all)
            active_all.update(new_sub)
            added = (len(active_all) > before)
        all_db = set(r[0] for r in c.execute("SELECT conversation_id FROM conversation_summaries").fetchall())
        orphaned_db_rows = all_db - active_all
        conn.close()

    brain_dirs = [d for d in os.listdir(brain_dir) if os.path.isdir(os.path.join(brain_dir, d))] if os.path.exists(brain_dir) else []
    conv_files = [f for f in os.listdir(conv_dir) if f.endswith(".db")] if os.path.exists(conv_dir) else []
    implicit_files = [f for f in os.listdir(implicit_dir) if f.endswith(".pb")] if os.path.exists(implicit_dir) else []

    brain_to_remove, brain_bytes = [], 0
    for d in brain_dirs:
        if d == "tempmediaStorage" or d in active_all:
            continue
        sz = get_dir_size(os.path.join(brain_dir, d))
        prev = get_preview(os.path.join(brain_dir, d))
        brain_to_remove.append((d, sz, prev))
        brain_bytes += sz

    conv_to_remove, conv_bytes = [], 0
    for f in conv_files:
        cid = f[:-3]
        if cid not in active_all:
            fp = os.path.join(conv_dir, f)
            sz = get_file_size(fp) + get_file_size(fp + "-wal") + get_file_size(fp + "-shm")
            conv_to_remove.append((f, sz))
            conv_bytes += sz

    implicit_to_remove = [f for f in implicit_files if f[:-3] not in active_all]

    return {
        "name": target_name, "base_dir": base_dir, "db_path": db_path, "has_db": has_db,
        "brain_dir": brain_dir, "conv_dir": conv_dir, "implicit_dir": implicit_dir,
        "active_top": len(active_top), "active_sub": len(active_all - active_top), "active_all": len(active_all),
        "orphaned_db": orphaned_db_rows,
        "brain_remove": brain_to_remove, "brain_bytes": brain_bytes,
        "conv_remove": conv_to_remove, "conv_bytes": conv_bytes,
        "implicit_remove": implicit_to_remove
    }

def perform_clean(res):
    print(f"\n[EXECUTE] Cleaning {res['name']}...")
    for d, _, _ in res["brain_remove"]:
        p = os.path.join(res["brain_dir"], d)
        try:
            shutil.rmtree(p)
            print(f"  Deleted brain dir: {d[:8]}...")
        except Exception as e:
            print(f"  Failed to delete {d}: {e}")
    for f, _ in res["conv_remove"]:
        base = os.path.join(res["conv_dir"], f[:-3])
        for ext in [".db", ".db-wal", ".db-shm"]:
            fp = base + ext
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception as e:
                    print(f"  Failed to delete {fp}: {e}")
        print(f"  Deleted conv file: {f[:8]}...")
    for f in res.get("implicit_remove", []):
        fp = os.path.join(res["implicit_dir"], f)
        try:
            os.remove(fp)
            print(f"  Deleted implicit context: {f[:8]}...")
        except Exception:
            pass
    if res["has_db"] and res["orphaned_db"]:
        conn = sqlite3.connect(res["db_path"])
        c = conn.cursor()
        c.execute(f"DELETE FROM conversation_summaries WHERE conversation_id IN ({','.join('?' for _ in res['orphaned_db'])})", list(res["orphaned_db"]))
        conn.commit()
        c.execute("VACUUM;")
        conn.commit()
        conn.close()
        print(f"  Removed {len(res['orphaned_db'])} DB records & vacuumed.")

def main():
    parser = argparse.ArgumentParser(description="Antigravity & IDE Session Cleaner")
    parser.add_argument("--app-data", "-p", help="Explicit path to Antigravity data directory")
    parser.add_argument("--target", "-t", default="all", choices=["all", "antigravity", "ide", "antigravity-ide"], help="Target instances to clean")
    parser.add_argument("--execute", action="store_true", help="Actually perform deletion (default: dry-run)")
    parser.add_argument("--protect-id", "-k", action="append", default=[], help="Conversation IDs to protect")
    args = parser.parse_args()

    all_targets = find_available_targets(args.app_data)
    selected = []
    if args.target in ("all", "antigravity") and "antigravity" in all_targets:
        selected.append(scan_target("Antigravity (Main)", all_targets["antigravity"], protect_ids=args.protect_id))
    if args.target in ("all", "antigravity-ide", "ide") and "antigravity-ide" in all_targets:
        selected.append(scan_target("Antigravity IDE", all_targets["antigravity-ide"], protect_ids=args.protect_id))
    if not selected:
        for name, path in all_targets.items():
            selected.append(scan_target(name, path, protect_ids=args.protect_id))

    total_reclaim = sum(r["brain_bytes"] + r["conv_bytes"] for r in selected)
    print("=" * 65)
    print("           Antigravity & IDE Session Cleanup Report")
    print("=" * 65)
    for res in selected:
        print(f"\nTarget: {res['name']} ({res['base_dir']})")
        print(f"  Active Top Sessions (Preserved):  {res['active_top']}")
        print(f"  Active Subagent Sessions:         {res['active_sub']}")
        print(f"  Orphaned DB Records to Remove:    {len(res['orphaned_db'])}")
        print(f"  Orphaned brain/ Dirs to Remove:   {len(res['brain_remove'])} ({res['brain_bytes'] / (1024*1024):.2f} MB)")
        print(f"  Orphaned conversations/ to Remove:{len(res['conv_remove'])} ({res['conv_bytes'] / (1024*1024):.2f} MB)")
        if res["brain_remove"]:
            print("  Items preview:")
            for d, sz, prev in res["brain_remove"][:5]:
                print(f"    - [{d[:8]}] ({sz/1024:.1f} KB) {prev}")
            if len(res["brain_remove"]) > 5:
                print(f"    ... and {len(res['brain_remove'])-5} more")

    print("\n" + "=" * 65)
    print(f"Total Reclaimable Disk Space: {total_reclaim / (1024*1024):.2f} MB")
    print("=" * 65)

    if not args.execute:
        print("\n[DRY RUN MODE] No files were touched.")
        print("To clean all:       uv run clean_sessions.py --execute")
        print("To clean IDE only:  uv run clean_sessions.py --target ide --execute")
        return

    for res in selected:
        perform_clean(res)
    print("\nAll selected targets cleaned successfully!")

if __name__ == "__main__":
    main()
