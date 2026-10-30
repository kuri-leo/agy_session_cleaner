import os
import sys

def get_candidate_bases():
    candidates = []
    for env_var in ["ANTIGRAVITY_APP_DATA", "ANTIGRAVITY_DATA_DIR", "ANTIGRAVITY_HOME"]:
        val = os.environ.get(env_var)
        if val:
            candidates.append(os.path.expanduser(val))
    gemini_home = os.environ.get("GEMINI_HOME")
    if gemini_home:
        candidates.append(os.path.expanduser(gemini_home))

    candidates.append(os.path.expanduser("~/.gemini"))
    if sys.platform == "win32":
        for var in ["USERPROFILE", "APPDATA", "LOCALAPPDATA"]:
            p = os.environ.get(var)
            if p:
                candidates.append(os.path.join(p, ".gemini"))
                candidates.append(p)
    elif sys.platform == "darwin":
        candidates.append(os.path.expanduser("~/Library/Application Support"))
        candidates.append(os.path.expanduser("~/Library/Application Support/Google"))
    else:
        candidates.append(os.path.expanduser("~/.config"))
        candidates.append(os.path.expanduser("~/.local/share"))

    seen = set()
    result = []
    for c in candidates:
        norm = os.path.normpath(c)
        if norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            result.append(norm)
    return result

def is_valid_antigravity_dir(path):
    if not path or not os.path.isdir(path):
        return False
    has_db = os.path.isfile(os.path.join(path, "conversation_summaries.db"))
    has_brain = os.path.isdir(os.path.join(path, "brain"))
    has_conv = os.path.isdir(os.path.join(path, "conversations"))
    return has_db or has_brain or has_conv

TARGET_NAMES = ["antigravity", "antigravity-ide", "antigravity-cli"]

def find_available_targets(override_path=None):
    if override_path:
        norm = os.path.normpath(os.path.expanduser(override_path))
        if is_valid_antigravity_dir(norm):
            name = os.path.basename(norm) or "custom"
            return {name: norm}
        # Also accept a parent directory (e.g. ~/.gemini) that contains target dirs.
        children = {}
        for tname in TARGET_NAMES:
            p = os.path.join(norm, tname)
            if is_valid_antigravity_dir(p):
                children[tname] = os.path.normpath(p)
        if children:
            return children
        raise FileNotFoundError(f"Specified directory invalid or not found: {override_path}")

    targets = {}
    target_names = TARGET_NAMES
    for base in get_candidate_bases():
        for tname in target_names:
            if tname not in targets:
                p = os.path.join(base, tname)
                if is_valid_antigravity_dir(p):
                    targets[tname] = os.path.normpath(p)

    if not targets:
        def_p = os.path.expanduser("~/.gemini/antigravity")
        if is_valid_antigravity_dir(def_p):
            targets["antigravity"] = os.path.normpath(def_p)

    return targets

def find_antigravity_path(override_path=None, verbose=True):
    targets = find_available_targets(override_path)
    if "antigravity" in targets:
        if verbose:
            print(f"[INFO] Auto-detected Antigravity path: {targets['antigravity']}")
        return targets["antigravity"]
    if targets:
        first_key = next(iter(targets))
        if verbose:
            print(f"[INFO] Using detected path ({first_key}): {targets[first_key]}")
        return targets[first_key]
    raise FileNotFoundError("[ERROR] Could not locate any Antigravity data directory.")
