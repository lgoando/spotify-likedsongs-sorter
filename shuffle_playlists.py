# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
shuffle_playlists.py
--------------------
Randomly shuffles every playlist whose name matches a .txt file in playlists/.

Usage:
    python shuffle_playlists.py              # shuffle all
    python shuffle_playlists.py --dry-run    # preview only
    python shuffle_playlists.py playlists/road_trip.txt   # one file
"""

import argparse
import os
import random
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
def _ansi(c): return f"\033[{c}m"
BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = (
    _ansi("1"), _ansi("2"), _ansi("31"), _ansi("32"),
    _ansi("33"), _ansi("36"), _ansi("0"),
)
def enable_windows_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)
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
    missing = [k for k, v in {"SPOTIFY_CLIENT_ID": client_id,
                               "SPOTIFY_CLIENT_SECRET": client_secret}.items() if not v]
    if missing:
        print(f"{RED}Missing credentials:{RESET} {', '.join(missing)}")
        sys.exit(1)
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=client_id, client_secret=client_secret,
        redirect_uri=redirect_uri, scope=SCOPES,
        cache_path=str(CACHE_PATH), open_browser=True,
    ), requests_timeout=15)

# ── Helpers ───────────────────────────────────────────────────────────────────
def find_playlist(sp: spotipy.Spotify, name: str) -> dict | None:
    """Return the first owned playlist matching `name` (case-insensitive)."""
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items") or []:
            if pl and pl.get("name", "").lower() == name.lower():
                return pl
        next_url = results.get("next")
        results  = sp._get(next_url) if next_url else None
    return None

def get_track_uris(sp: spotipy.Spotify, playlist_id: str) -> list[str]:
    """Return ordered list of track URIs in a playlist."""
    uris: list[str] = []
    results = sp.playlist_items(playlist_id, limit=100)
    while results:
        for item in results.get("items") or []:
            # Spotify API uses "track" in older responses, "item" in newer ones
            track = (item or {}).get("track") or (item or {}).get("item")
            if track and track.get("uri") and not track.get("is_local"):
                uris.append(track["uri"])
        next_url = results.get("next")
        results  = sp._get(next_url) if next_url else None
    return uris

def replace_playlist_tracks(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Overwrite a playlist with exactly the given URIs."""
    sp.playlist_replace_items(playlist_id, [])
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist_id, uris[i : i + 100])
        time.sleep(0.2)

# ── Core ──────────────────────────────────────────────────────────────────────
def shuffle_playlist(sp: spotipy.Spotify, name: str, *, dry_run: bool) -> None:
    print(f"\n{BOLD}{CYAN}{'[DRY RUN] ' if dry_run else ''}Shuffling:{RESET} {BOLD}{name}{RESET}")

    pl = find_playlist(sp, name)
    if not pl:
        print(f"  {YELLOW}No playlist named \"{name}\" found — skipping.{RESET}")
        return

    uris = get_track_uris(sp, pl["id"])
    if not uris:
        print(f"  {YELLOW}Playlist is empty — skipping.{RESET}")
        return

    print(f"  {len(uris)} tracks found.")

    shuffled = uris[:]
    random.shuffle(shuffled)

    if dry_run:
        print(f"  {DIM}First 5 after shuffle would be:{RESET}")
        for uri in shuffled[:5]:
            print(f"    {DIM}{uri}{RESET}")
        print(f"  {DIM}Dry run — no changes made.{RESET}")
        return

    replace_playlist_tracks(sp, pl["id"], shuffled)
    print(f"  {GREEN}Done — {len(shuffled)} tracks shuffled.{RESET}")

# ── CLI ───────────────────────────────────────────────────────────────────────
def main() -> None:
    enable_windows_ansi()

    p = argparse.ArgumentParser(description="Shuffle Spotify playlists from txt files.")
    p.add_argument("files", nargs="*", metavar="FILE",
                   help="Specific .txt file(s) to use. Defaults to all in playlists/.")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview without making changes.")
    args = p.parse_args()

    if args.files:
        paths = [Path(f) for f in args.files]
        for path in paths:
            if not path.exists():
                print(f"{RED}File not found:{RESET} {path}")
                sys.exit(1)
    else:
        if not PLAYLISTS_DIR.exists():
            print(f"{RED}'{PLAYLISTS_DIR}/' not found.{RESET}")
            sys.exit(1)
        paths = sorted(PLAYLISTS_DIR.glob("*.txt"))
        if not paths:
            print(f"{YELLOW}No .txt files in {PLAYLISTS_DIR}/.{RESET}")
            sys.exit(0)

    names = [p.stem for p in paths]

    print(f"\n{BOLD}{CYAN}shuffle_playlists{RESET}  —  Connecting to Spotify...\n")
    sp = get_spotify_client()
    try:
        me = sp.current_user()
        print(f"{GREEN}Connected as {BOLD}{me['display_name']}{RESET}")
    except spotipy.SpotifyException as exc:
        print(f"{RED}Auth error: {exc}{RESET}")
        sys.exit(1)

    print(f"\nWill shuffle: {', '.join(BOLD + n + RESET for n in names)}")

    if not args.dry_run:
        if input(f"\n{YELLOW}Proceed? [y/N]{RESET} ").strip().lower() != "y":
            print("Cancelled.")
            sys.exit(0)

    for name in names:
        try:
            shuffle_playlist(sp, name, dry_run=args.dry_run)
        except spotipy.SpotifyException as exc:
            print(f"\n  {RED}Spotify error:{RESET} {exc}")

    print(f"\n{BOLD}{GREEN}Done!{RESET}")

if __name__ == "__main__":
    main()
