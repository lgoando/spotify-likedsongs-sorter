# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
cleanup_playlists.py
--------------------
Finds every playlist whose name matches a .txt file in playlists/,
then for each name:

  1. If multiple playlists share the same name (from repeated script runs),
     merges them all into one and deletes the extras.
  2. Removes any duplicate tracks within the surviving playlist.

Always shows a preview and asks for confirmation before changing anything.

Usage:
    python cleanup_playlists.py            # uses playlists/ folder
    python cleanup_playlists.py --dry-run  # preview only, no changes
"""

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("spotipy not found.  Run: python -m pip install spotipy python-dotenv")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Colours ───────────────────────────────────────────────────────────────────
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
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
SCOPES = " ".join([
    "playlist-read-private",
    "playlist-modify-public",
    "playlist-modify-private",
])
CACHE_PATH    = Path(".spotify_cache")
PLAYLISTS_DIR = Path("playlists")


# ── Auth ──────────────────────────────────────────────────────────────────────
def get_spotify_client() -> spotipy.Spotify:
    client_id     = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback")

    missing = [k for k, v in {
        "SPOTIFY_CLIENT_ID":     client_id,
        "SPOTIFY_CLIENT_SECRET": client_secret,
    }.items() if not v]

    if missing:
        print(f"{RED}Missing credentials:{RESET} {', '.join(missing)}")
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


# ── Spotify helpers ───────────────────────────────────────────────────────────
def get_all_user_playlists(sp: spotipy.Spotify) -> list[dict]:
    """Fetch every playlist owned by the current user."""
    me      = sp.current_user()["id"]
    items   : list[dict] = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items") or []:
            if pl and pl.get("owner", {}).get("id") == me:
                items.append(pl)
        next_url = results.get("next")
        results  = sp._get(next_url) if next_url else None
    return items


def get_playlist_items(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    """
    Return all playlist-item dicts (preserves order, skips local/podcast tracks).
    NOTE: newer Spotify API versions return the track under the key "item"
    instead of "track" — we handle both.
    """
    items: list[dict] = []
    results = sp.playlist_items(playlist_id, limit=100)
    while results:
        for item in results.get("items") or []:
            # "track" in older API responses, "item" in newer ones
            track = (item or {}).get("track") or (item or {}).get("item")
            if track and track.get("id") and not track.get("is_local"):
                items.append(item)
        next_url = results.get("next")
        results  = sp._get(next_url) if next_url else None
    return items


def replace_playlist_tracks(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Overwrite a playlist with exactly the given URIs (chunks of 100)."""
    sp.playlist_replace_items(playlist_id, [])
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist_id, uris[i : i + 100])
        time.sleep(0.2)


def delete_playlist(sp: spotipy.Spotify, playlist_id: str) -> None:
    """Unfollow (effectively delete) a playlist owned by the current user."""
    sp.current_user_unfollow_playlist(playlist_id)


# ── Core logic ────────────────────────────────────────────────────────────────
def dedup_items(items: list[dict]) -> tuple[list[str], list[str]]:
    """
    Walk items in order, keep the first occurrence of each track ID.
    Returns (kept_uris, duplicate_track_names).
    """
    seen      : set[str]  = set()
    kept_uris : list[str] = []
    dupes     : list[str] = []

    for item in items:
        track = item.get("track") or item.get("item")
        tid   = track["id"]
        uri   = track["uri"]
        name  = track["name"]
        artist = track["artists"][0]["name"] if track.get("artists") else "?"

        if tid in seen:
            dupes.append(f"{name} — {artist}")
        else:
            seen.add(tid)
            kept_uris.append(uri)

    return kept_uris, dupes


def process_playlist_name(
    sp: spotipy.Spotify,
    name: str,
    all_playlists: list[dict],
    *,
    dry_run: bool,
) -> None:
    print(f"\n{BOLD}{CYAN}{'[DRY RUN] ' if dry_run else ''}Playlist: {name}{RESET}")

    # Find all playlists with this name (there may be duplicates from old runs)
    matches = [pl for pl in all_playlists if pl["name"].lower() == name.lower()]

    if not matches:
        print(f"  {YELLOW}No playlist named \"{name}\" found — skipping.{RESET}")
        return

    # ── Step 1: merge duplicate playlists ────────────────────────────────────
    if len(matches) > 1:
        print(f"  {YELLOW}Found {len(matches)} playlists with this name:{RESET}")
        for i, pl in enumerate(matches):
            print(f"    {DIM}{i + 1}.{RESET} id={pl['id']}")

        # Collect all tracks from all copies, in order (oldest playlist first)
        print(f"  {CYAN}Merging all copies into one...{RESET}")
        all_items: list[dict] = []
        for pl in matches:
            all_items.extend(get_playlist_items(sp, pl["id"]))

        kept_uris, dupes = dedup_items(all_items)

        print(f"  Tracks across all copies : {len(all_items)}")
        print(f"  After dedup              : {len(kept_uris)}")
        print(f"  Duplicates removed       : {len(dupes)}")

        if dupes:
            print(f"\n  {YELLOW}Duplicate tracks that will be removed:{RESET}")
            for d in dupes[:20]:
                print(f"    {DIM}- {d}{RESET}")
            if len(dupes) > 20:
                print(f"    {DIM}... and {len(dupes) - 20} more{RESET}")

        # Keep the first (oldest) playlist, delete the rest
        keeper   = matches[0]
        to_delete = matches[1:]

        print(f"\n  Keeping   : {keeper['id']}")
        print(f"  Deleting  : {', '.join(pl['id'] for pl in to_delete)}")

        if not dry_run:
            replace_playlist_tracks(sp, keeper["id"], kept_uris)
            for pl in to_delete:
                delete_playlist(sp, pl["id"])
            print(f"  {GREEN}Done — kept 1 playlist with {len(kept_uris)} unique tracks, deleted {len(to_delete)} extra copy/copies.{RESET}")
        else:
            print(f"  {DIM}Dry run — no changes made.{RESET}")

    # ── Step 2: single playlist, just dedup in place ──────────────────────────
    else:
        pl    = matches[0]
        items = get_playlist_items(sp, pl["id"])
        print(f"  {DIM}1 playlist found — fetched {len(items)} track(s).{RESET}")

        if not items:
            print(f"  {YELLOW}Could not fetch any tracks — skipping.{RESET}")
            return

        kept_uris, dupes = dedup_items(items)

        if not dupes:
            print(f"  {GREEN}No duplicates found — nothing to do.{RESET}")
            return

        print(f"  Duplicates found : {len(dupes)}")
        print(f"\n  {YELLOW}Tracks that will be removed:{RESET}")
        for d in dupes[:20]:
            print(f"    {DIM}- {d}{RESET}")
        if len(dupes) > 20:
            print(f"    {DIM}... and {len(dupes) - 20} more{RESET}")

        if not dry_run:
            replace_playlist_tracks(sp, pl["id"], kept_uris)
            print(f"  {GREEN}Done — {len(dupes)} duplicate(s) removed, {len(kept_uris)} tracks remain.{RESET}")
        else:
            print(f"  {DIM}Dry run — no changes made.{RESET}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Remove duplicate tracks/playlists created by txt_to_playlist.py.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without touching Spotify.",
    )
    return p


def main() -> None:
    enable_windows_ansi()
    args = build_arg_parser().parse_args()

    if not PLAYLISTS_DIR.exists():
        print(f"{RED}'{PLAYLISTS_DIR}/' folder not found.{RESET}")
        sys.exit(1)

    txt_files = sorted(PLAYLISTS_DIR.glob("*.txt"))
    if not txt_files:
        print(f"{YELLOW}No .txt files found in {PLAYLISTS_DIR}/.{RESET}")
        sys.exit(0)

    playlist_names = [p.stem for p in txt_files]

    print(f"\n{BOLD}{CYAN}cleanup_playlists{RESET}  —  Connecting to Spotify...\n")
    sp = get_spotify_client()

    try:
        me = sp.current_user()
        print(f"{GREEN}Connected as {BOLD}{me['display_name']}{RESET}")
    except spotipy.SpotifyException as exc:
        print(f"{RED}Auth error: {exc}{RESET}")
        sys.exit(1)

    print(f"\n{CYAN}Fetching your playlists...{RESET}")
    all_playlists = get_all_user_playlists(sp)
    print(f"Found {len(all_playlists)} playlists owned by you.")

    print(f"\nWill clean up: {', '.join(BOLD + n + RESET for n in playlist_names)}")

    if not args.dry_run:
        confirm = input(f"\n{YELLOW}Proceed? This will modify your Spotify playlists. [y/N]{RESET} ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            sys.exit(0)

    for name in playlist_names:
        try:
            process_playlist_name(sp, name, all_playlists, dry_run=args.dry_run)
        except spotipy.SpotifyException as exc:
            print(f"\n  {RED}Spotify API error:{RESET} {exc}")

    print(f"\n{BOLD}{GREEN}Done!{RESET}")


if __name__ == "__main__":
    main()
