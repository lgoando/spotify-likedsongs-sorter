# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
Spotify Playlist Organizer
Authenticate, inspect, sort, deduplicate, and restructure your playlists.
"""

import os
import sys
import time
import json
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

# ── Spotipy ──────────────────────────────────────────────────────────────────
try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("spotipy not found. Run: python -m pip install spotipy python-dotenv")

# ── UTF-8 stdout (fixes emoji + box-drawing on Windows) ──────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
])

CACHE_PATH = Path(".spotify_cache")

# ── ANSI colours (graceful fallback when VT not supported) ───────────────────
def _ansi(code: str) -> str:
    return f"\033[{code}m"

BOLD   = _ansi("1")
DIM    = _ansi("2")
RED    = _ansi("31")
GREEN  = _ansi("32")
YELLOW = _ansi("33")
CYAN   = _ansi("36")
RESET  = _ansi("0")

def enable_windows_ansi() -> None:
    """Enable ANSI escape codes on Windows (no-op on failure)."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass  # piped/redirected output — ANSI not needed

# ── Auth ─────────────────────────────────────────────────────────────────────

def get_spotify_client() -> spotipy.Spotify:
    client_id     = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")

    missing = [k for k, v in {
        "SPOTIFY_CLIENT_ID": client_id,
        "SPOTIFY_CLIENT_SECRET": client_secret,
    }.items() if not v]

    if missing:
        print(f"{RED}Missing credentials:{RESET} {', '.join(missing)}")
        print(f"\n{YELLOW}Steps to fix:{RESET}")
        print("  1. Go to https://developer.spotify.com/dashboard")
        print("  2. Create an app (or open an existing one)")
        print("  3. Copy Client ID and Client Secret")
        print(f"  4. Add  {CYAN}http://localhost:8888/callback{RESET}  as a Redirect URI in the app settings")
        print("  5. Copy .env.example -> .env and fill in the values")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        cache_path=str(CACHE_PATH),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager, requests_timeout=15)


# ── Module-level client (set in main()) ──────────────────────────────────────
_sp: spotipy.Spotify


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_all_playlists() -> list[dict]:
    playlists: list[dict] = []
    results = _sp.current_user_playlists(limit=50)
    playlists.extend(results["items"])
    while results["next"]:
        results = _sp.next(results)
        playlists.extend(results["items"])
    return [p for p in playlists if p]  # strip None entries


def get_playlist_tracks(playlist_id: str) -> list[dict]:
    """Return a flat list of playlist-item dicts (skips local/None tracks)."""
    raw: list[dict] = []
    results = _sp.playlist_items(
        playlist_id,
        fields="items(added_at,track(id,name,artists,album,duration_ms,popularity,explicit)),next",
        limit=100,
    )
    raw.extend(results["items"])
    while results["next"]:
        results = _sp.next(results)
        raw.extend(results["items"])

    # Local files have no id; podcast episodes arrive as None tracks
    return [item for item in raw if item.get("track") and item["track"].get("id")]


def get_audio_features(track_ids: list[str]) -> dict[str, dict]:
    """
    Batch-fetch audio features (max 100 per request).
    Returns {track_id: features_dict}.
    Note: Spotify deprecated this endpoint for apps created after Nov 2024.
    Returns an empty dict (with a warning) if the API denies access.
    """
    features: dict[str, dict] = {}
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i : i + 100]
        try:
            resp = _sp.audio_features(chunk)
        except spotipy.SpotifyException as exc:
            if exc.http_status in (403, 401):
                print(
                    f"\n{YELLOW}Warning:{RESET} Spotify has restricted audio-features access "
                    f"for this app (HTTP {exc.http_status}).\n"
                    "  Apps created after November 2024 cannot use this endpoint.\n"
                    "  Sort/analyze by audio features will be unavailable."
                )
                return {}
            raise
        if resp:
            for f in resp:
                if f:
                    features[f["id"]] = f
    return features


def pick_playlist(prompt: str = "Choose a playlist") -> dict | None:
    playlists = get_all_playlists()
    if not playlists:
        print(f"{YELLOW}No playlists found.{RESET}")
        return None

    print(f"\n{BOLD}{prompt}:{RESET}")
    for i, p in enumerate(playlists, 1):
        owner = p.get("owner", {}).get("display_name", "?")
        count = p.get("tracks", {}).get("total", "?")
        print(f"  {DIM}{i:>3}.{RESET} {p['name']:<40} {DIM}{count} tracks  [{owner}]{RESET}")

    while True:
        raw = input("\nEnter number (or 0 to cancel): ").strip()
        if raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(playlists):
            return playlists[int(raw) - 1]
        print(f"{RED}Invalid choice.{RESET}")


def confirm(msg: str) -> bool:
    return input(f"{YELLOW}{msg} [y/N]{RESET} ").strip().lower() == "y"


# ── Sort key factories ────────────────────────────────────────────────────────

def sort_key_artist(item: dict):
    t = item["track"]
    return (t["artists"][0]["name"].lower(), t["album"]["name"].lower(), t["name"].lower())

def sort_key_title(item: dict):
    return item["track"]["name"].lower()

def sort_key_album(item: dict):
    t = item["track"]
    return (t["album"]["name"].lower(), t["artists"][0]["name"].lower())

def sort_key_release(item: dict):
    return item["track"]["album"].get("release_date") or "0000"  # ISO strings sort correctly

def sort_key_added(item: dict):
    return item.get("added_at") or ""

def sort_key_popularity(item: dict):
    return -(item["track"].get("popularity") or 0)  # highest first

def sort_key_duration(item: dict):
    return item["track"].get("duration_ms") or 0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _apply_new_order(playlist_id: str, sorted_items: list[dict]) -> None:
    """
    Replace playlist contents with sorted_items.
    Uses replace-then-add strategy (avoids reorder-position API bugs with >100 tracks).
    """
    uris = [f"spotify:track:{item['track']['id']}" for item in sorted_items]

    # Wipe the playlist first
    _sp.playlist_replace_items(playlist_id, [])

    # Re-add in chunks of 100
    for i in range(0, len(uris), 100):
        _sp.playlist_add_items(playlist_id, uris[i : i + 100])
        time.sleep(0.2)  # stay within rate limits


# ── Operations ────────────────────────────────────────────────────────────────

def op_list_playlists() -> None:
    playlists = get_all_playlists()
    me = _sp.current_user()
    print(f"\n{BOLD}Playlists for {me['display_name']}:{RESET}")
    total_tracks = 0
    for p in playlists:
        count = p.get("tracks", {}).get("total", 0)
        total_tracks += count
        pub    = "" if p.get("public") else f" {DIM}[private]{RESET}"
        collab = f" {CYAN}[collab]{RESET}" if p.get("collaborative") else ""
        print(f"  {p['name']:<45} {count:>5} tracks{pub}{collab}")
    print(f"\n  {BOLD}Total:{RESET} {len(playlists)} playlists, ~{total_tracks} tracks")


def op_view_tracks() -> None:
    pl = pick_playlist("View tracks in which playlist")
    if not pl:
        return
    print(f"\n{BOLD}Tracks in \"{pl['name']}\":{RESET}")
    items = get_playlist_tracks(pl["id"])
    for i, item in enumerate(items, 1):
        t      = item["track"]
        artist = t["artists"][0]["name"]
        mins, secs = divmod((t.get("duration_ms") or 0) // 1000, 60)
        print(f"  {DIM}{i:>4}.{RESET} {t['name']:<45} {DIM}{artist:<30} {mins}:{secs:02d}{RESET}")
    print(f"\n  {len(items)} tracks total.")


def op_sort_playlist() -> None:
    pl = pick_playlist("Sort which playlist")
    if not pl:
        return

    options = [
        ("Artist -> Album -> Title",           sort_key_artist),
        ("Track title",                        sort_key_title),
        ("Album name",                         sort_key_album),
        ("Release date (oldest first)",        sort_key_release),
        ("Date added (oldest first)",          sort_key_added),
        ("Popularity (most popular first)",    sort_key_popularity),
        ("Duration (shortest first)",          sort_key_duration),
        ("Tempo / BPM  [needs audio features]", None),
        ("Energy       [needs audio features]", None),
        ("Danceability [needs audio features]", None),
    ]

    print(f"\n{BOLD}Sort by:{RESET}")
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")

    while True:
        raw = input("Choice: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            choice = int(raw)
            break
        print(f"{RED}Invalid.{RESET}")

    # Options 4-7 have the direction embedded in the label; ask for the rest
    needs_direction = choice not in (4, 5, 6, 8, 9, 10)
    reverse = False
    if needs_direction:
        reverse = input("Reverse order? [y/N] ").strip().lower() == "y"

    print(f"\n{CYAN}Fetching tracks...{RESET}")
    items = get_playlist_tracks(pl["id"])
    if not items:
        print(f"{YELLOW}Playlist is empty.{RESET}")
        return

    if choice <= 7:
        key_fn       = options[choice - 1][1]
        sorted_items = sorted(items, key=key_fn, reverse=reverse)
    else:
        feat_name = ["tempo", "energy", "danceability"][choice - 8]
        print(f"{CYAN}Fetching audio features...{RESET}")
        ids      = [item["track"]["id"] for item in items]
        features = get_audio_features(ids)

        if not features:
            print(f"{YELLOW}No audio features available — cannot sort by {feat_name}.{RESET}")
            return

        def audio_key(item: dict):
            f = features.get(item["track"]["id"])
            return f[feat_name] if f else 0.0

        sorted_items = sorted(items, key=audio_key, reverse=reverse)

    print(f"\n{BOLD}Preview (first 10 after sort):{RESET}")
    for item in sorted_items[:10]:
        t = item["track"]
        print(f"  {t['name']:<45} {DIM}{t['artists'][0]['name']}{RESET}")

    if not confirm(f"\nApply this sort to \"{pl['name']}\"?"):
        print("Cancelled.")
        return

    _apply_new_order(pl["id"], sorted_items)
    print(f"{GREEN}Playlist sorted.{RESET}")


def op_remove_duplicates() -> None:
    pl = pick_playlist("Remove duplicates from which playlist")
    if not pl:
        return

    print(f"{CYAN}Fetching tracks...{RESET}")
    items        = get_playlist_tracks(pl["id"])
    seen         : set[str] = set()
    unique_items : list[dict] = []
    dupe_names   : list[str] = []

    for item in items:
        tid = item["track"]["id"]
        if tid in seen:
            dupe_names.append(item["track"]["name"])
        else:
            seen.add(tid)
            unique_items.append(item)

    if not dupe_names:
        print(f"{GREEN}No duplicates found!{RESET}")
        return

    print(f"\n{YELLOW}Found {len(dupe_names)} duplicate(s):{RESET}")
    for name in dupe_names[:20]:
        print(f"  - {name}")
    if len(dupe_names) > 20:
        print(f"  ... and {len(dupe_names) - 20} more")

    if not confirm(f"\nRemove {len(dupe_names)} duplicate(s)?"):
        print("Cancelled.")
        return

    _apply_new_order(pl["id"], unique_items)
    print(f"{GREEN}Removed {len(dupe_names)} duplicate(s).{RESET}")


def op_merge_playlists() -> None:
    print(f"\n{BOLD}Merge playlists into a new playlist{RESET}")
    print("Select source playlists one at a time. Enter 0 when done.\n")

    sources: list[dict] = []
    while True:
        pl = pick_playlist(f"Add source playlist #{len(sources) + 1} (0 to finish)")
        if pl is None:
            if len(sources) < 2:
                print(f"{YELLOW}Please select at least 2 playlists.{RESET}")
                continue
            break
        if any(s["id"] == pl["id"] for s in sources):
            print(f"{YELLOW}Already added.{RESET}")
        else:
            sources.append(pl)
            print(f"  {GREEN}+{RESET} Added \"{pl['name']}\"")

    name = input("\nNew playlist name: ").strip()
    if not name:
        print("Cancelled.")
        return

    dedup = confirm("Remove duplicates during merge?")

    print(f"\n{CYAN}Fetching tracks...{RESET}")
    all_items : list[dict] = []
    seen_ids  : set[str]   = set()
    for src in sources:
        src_items = get_playlist_tracks(src["id"])
        added = 0
        for item in src_items:
            tid = item["track"]["id"]
            if dedup and tid in seen_ids:
                continue
            seen_ids.add(tid)
            all_items.append(item)
            added += 1
        print(f"  Loaded \"{src['name']}\" -> {added} tracks added")

    me     = _sp.current_user()
    new_pl = _sp.user_playlist_create(
        user=me["id"],
        name=name,
        public=False,
        description="Created by Spotify Organizer",
    )
    uris = [f"spotify:track:{i['track']['id']}" for i in all_items]
    for i in range(0, len(uris), 100):
        _sp.playlist_add_items(new_pl["id"], uris[i : i + 100])
        time.sleep(0.2)

    print(f"\n{GREEN}Created \"{name}\" with {len(all_items)} tracks.{RESET}")


def op_split_by_decade() -> None:
    pl = pick_playlist("Split which playlist by decade")
    if not pl:
        return

    print(f"{CYAN}Fetching tracks...{RESET}")
    items = get_playlist_tracks(pl["id"])

    decades: defaultdict[str, list] = defaultdict(list)
    for item in items:
        rd = item["track"]["album"].get("release_date") or ""
        if rd and len(rd) >= 4 and rd[:4].isdigit():
            year   = int(rd[:4])
            decade = f"{(year // 10) * 10}s"
        else:
            decade = "Unknown"
        decades[decade].append(item)

    # Sort decades chronologically; push "Unknown" to the end
    def decade_sort_key(k: str) -> tuple:
        return (1, k) if k == "Unknown" else (0, k)

    print(f"\n{BOLD}Decades found:{RESET}")
    for decade in sorted(decades.keys(), key=decade_sort_key):
        print(f"  {decade}: {len(decades[decade])} tracks")

    if not confirm("\nCreate a new playlist for each decade?"):
        print("Cancelled.")
        return

    me = _sp.current_user()
    for decade in sorted(decades.keys(), key=decade_sort_key):
        tracks  = decades[decade]
        pl_name = f"{pl['name']} - {decade}"
        new_pl  = _sp.user_playlist_create(
            user=me["id"],
            name=pl_name,
            public=False,
            description=f"Split from \"{pl['name']}\" by Spotify Organizer",
        )
        uris = [f"spotify:track:{i['track']['id']}" for i in tracks]
        for i in range(0, len(uris), 100):
            _sp.playlist_add_items(new_pl["id"], uris[i : i + 100])
            time.sleep(0.2)
        print(f"  {GREEN}Created \"{pl_name}\" ({len(tracks)} tracks){RESET}")


def op_analyze_playlist() -> None:
    pl = pick_playlist("Analyze which playlist")
    if not pl:
        return

    print(f"{CYAN}Fetching tracks...{RESET}")
    items = get_playlist_tracks(pl["id"])
    if not items:
        print(f"{YELLOW}Playlist is empty.{RESET}")
        return

    ids = [item["track"]["id"] for item in items]
    print(f"{CYAN}Fetching audio features...{RESET}")
    features = get_audio_features(ids)

    artists    : defaultdict[str, int]   = defaultdict(int)
    decades    : defaultdict[str, int]   = defaultdict(int)
    total_ms   = 0
    feat_totals: defaultdict[str, float] = defaultdict(float)
    feat_count = 0

    for item in items:
        t = item["track"]
        artists[t["artists"][0]["name"]] += 1
        rd   = t["album"].get("release_date") or ""
        if rd and len(rd) >= 4 and rd[:4].isdigit():
            year = int(rd[:4])
            decades[f"{(year // 10) * 10}s"] += 1
        total_ms += t.get("duration_ms") or 0

        f = features.get(t["id"])
        if f:
            for k in ("tempo", "energy", "danceability", "valence", "acousticness"):
                feat_totals[k] += f[k]
            feat_count += 1

    total_min       = total_ms // 60000
    hours, mins     = divmod(total_min, 60)
    top_artists     = sorted(artists.items(), key=lambda x: -x[1])[:10]

    print(f"\n{BOLD}=== Analysis: \"{pl['name']}\" ==={RESET}")
    print(f"\n  Tracks   : {len(items)}")
    print(f"  Duration : {hours}h {mins}m")

    print(f"\n  {BOLD}Top artists:{RESET}")
    max_count = top_artists[0][1] if top_artists else 1
    for name, count in top_artists:
        bar_len = round(count / max_count * 20)
        bar     = "#" * bar_len
        print(f"    {name:<35} {count:>3}  {DIM}{bar}{RESET}")

    print(f"\n  {BOLD}By decade:{RESET}")
    for decade, count in sorted(decades.items()):
        print(f"    {decade}: {count} tracks")

    if feat_count:
        print(f"\n  {BOLD}Audio features (averages):{RESET}")
        for feat in ("tempo", "energy", "danceability", "valence", "acousticness"):
            avg = feat_totals[feat] / feat_count
            if feat == "tempo":
                print(f"    {'Tempo':<20} {avg:>6.1f} BPM")
            else:
                filled  = round(avg * 20)
                bar     = "[" + "#" * filled + "-" * (20 - filled) + "]"
                print(f"    {feat.capitalize():<20} {avg:.2f}  {bar}")
    elif ids:
        print(f"\n  {DIM}(audio features unavailable for this app){RESET}")


def op_export_json() -> None:
    pl = pick_playlist("Export which playlist to JSON")
    if not pl:
        return

    print(f"{CYAN}Fetching tracks...{RESET}")
    items = get_playlist_tracks(pl["id"])

    output = {
        "playlist": {
            "id":    pl["id"],
            "name":  pl["name"],
            "owner": pl.get("owner", {}).get("display_name"),
            "total": pl.get("tracks", {}).get("total"),
        },
        "tracks": [
            {
                "position":     idx,
                "added_at":     item.get("added_at"),
                "id":           item["track"]["id"],
                "name":         item["track"]["name"],
                "artists":      [a["name"] for a in item["track"]["artists"]],
                "album":        item["track"]["album"]["name"],
                "release_date": item["track"]["album"].get("release_date"),
                "duration_ms":  item["track"].get("duration_ms"),
                "popularity":   item["track"].get("popularity"),
                "explicit":     item["track"].get("explicit"),
            }
            for idx, item in enumerate(items, 1)
        ],
    }

    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in pl["name"])
    filename  = f"{safe_name}.json"
    Path(filename).write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{GREEN}Exported {len(items)} tracks to {filename}{RESET}")


# ── Menu ──────────────────────────────────────────────────────────────────────

MENU = [
    ("List all playlists",                op_list_playlists),
    ("View tracks in a playlist",         op_view_tracks),
    ("Sort a playlist",                   op_sort_playlist),
    ("Remove duplicates from a playlist", op_remove_duplicates),
    ("Merge playlists into a new one",    op_merge_playlists),
    ("Split playlist by decade",          op_split_by_decade),
    ("Analyze a playlist",                op_analyze_playlist),
    ("Export playlist to JSON",           op_export_json),
]


def main() -> None:
    global _sp
    enable_windows_ansi()

    print(f"\n{BOLD}{CYAN}Spotify Playlist Organizer{RESET}")
    print(f"{DIM}Connecting to Spotify...{RESET}\n")

    _sp = get_spotify_client()

    try:
        me = _sp.current_user()
        print(f"{GREEN}Connected as {BOLD}{me['display_name']}{RESET}")
    except spotipy.SpotifyException as exc:
        print(f"{RED}Auth error: {exc}{RESET}")
        sys.exit(1)

    while True:
        print(f"\n{BOLD}What would you like to do?{RESET}")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {CYAN}{i}{RESET}. {label}")
        print(f"  {CYAN}0{RESET}. Quit")

        raw = input("\n> ").strip()
        if raw == "0":
            print(f"\n{DIM}Goodbye!{RESET}")
            break
        if raw.isdigit() and 1 <= int(raw) <= len(MENU):
            try:
                MENU[int(raw) - 1][1]()
            except spotipy.SpotifyException as exc:
                print(f"\n{RED}Spotify API error:{RESET} {exc}")
            except KeyboardInterrupt:
                print("\nCancelled.")
        else:
            print(f"{RED}Invalid choice.{RESET}")


if __name__ == "__main__":
    main()
