# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
txt_to_playlist.py
------------------
Turn one or more plain-text files into Spotify playlists.

File format (one song per line):
    SONG - ARTIST
    # lines starting with # are comments and are skipped
    (blank lines are also skipped)

The playlist name is taken from the filename (without extension).

Usage:
    # Process a specific file
    python txt_to_playlist.py playlists/my_playlist.txt

    # Process multiple files
    python txt_to_playlist.py playlists/road_trip.txt playlists/gym.txt

    # Process EVERY .txt file in the playlists/ folder
    python txt_to_playlist.py

Options:
    --public          Make the created playlist(s) public (default: private)
    --no-create       Don't create a new playlist; add to an existing one instead
    --dry-run         Search for tracks and report matches without touching Spotify
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

# ── Rate-limit retry wrapper ──────────────────────────────────────────────────
def _call_with_retry(fn, *args, max_retries: int = 5, **kwargs):
    """
    Call fn(*args, **kwargs). On a 429 Too Many Requests response, sleep for
    the Retry-After value (or an exponential back-off) and try again.
    """
    delay = 1
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as exc:
            if exc.http_status != 429:
                raise
            # Respect the Retry-After header when present
            retry_after = int(
                getattr(exc, "headers", {}).get("Retry-After", 0) or 0
            )
            wait = retry_after if retry_after > 0 else delay
            # If Spotify wants us to wait more than 5 minutes it's a quota
            # ban — there's no point hanging; tell the user and stop.
            if wait > 300:
                print(
                    f"\n  {RED}Spotify rate limit is very long ({wait}s ≈ "
                    f"{wait // 3600}h {(wait % 3600) // 60}m).{RESET}\n"
                    f"  {YELLOW}This usually means the daily API quota has been "
                    f"exhausted.\n"
                    f"  Try again in ~{wait // 3600 + 1} hour(s).{RESET}\n"
                )
                sys.exit(1)
            print(
                f"  {YELLOW}Rate limited by Spotify — waiting {wait}s "
                f"(attempt {attempt + 1}/{max_retries})…{RESET}"
            )
            time.sleep(wait)
            delay = min(delay * 2, 60)  # exponential back-off, cap at 60 s
    # Final attempt — let any exception propagate
    return fn(*args, **kwargs)

# ── UTF-8 stdout (fixes emoji + box-drawing on Windows) ──────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Colour helpers ────────────────────────────────────────────────────────────
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
        print(f"\n{YELLOW}Steps to fix:{RESET}")
        print("  1. Go to https://developer.spotify.com/dashboard")
        print("  2. Create or open an app")
        print("  3. Copy Client ID and Client Secret")
        print(f"  4. Add  {CYAN}http://localhost:8888/callback{RESET}  as a Redirect URI")
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


# Accepted separators: ASCII hyphen ' - ' or em dash ' — ' (U+2014) or en dash ' – ' (U+2013)
_SEPARATORS = [" — ", " – ", " - "]

# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_txt(path: Path) -> list[tuple[str, str]]:
    """
    Read a txt file and return a list of (song, artist) tuples.
    Lines starting with # and blank lines are ignored.
    Accepts any of these separators between song and artist:
        ' - '  (ASCII hyphen-minus)
        ' — '  (em dash, U+2014)
        ' – '  (en dash, U+2013)
    """
    entries: list[tuple[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Find the first matching separator
        sep = next((s for s in _SEPARATORS if s in line), None)
        if sep is None:
            print(f"  {YELLOW}Line {lineno} skipped{RESET} (no separator found): {DIM}{line}{RESET}")
            continue
        # Split on the FIRST occurrence only
        song, artist = line.split(sep, 1)
        entries.append((song.strip(), artist.strip()))
    return entries


# ── Spotify helpers ───────────────────────────────────────────────────────────
def search_track(sp: spotipy.Spotify, song: str, artist: str) -> str | None:
    """
    Search for a track by song + artist.
    Returns the Spotify track URI or None if not found.
    Tries two queries: exact artist field first, then a broader free-text search.
    A small delay is inserted between requests to stay well under the rate limit.
    """
    queries = [
        f"track:{song} artist:{artist}",
        f"{song} {artist}",
    ]
    for query in queries:
        results = _call_with_retry(sp.search, q=query, type="track", limit=1)
        items   = results.get("tracks", {}).get("items", [])
        if items:
            return items[0]["uri"]
        time.sleep(0.15)  # brief pause between the two fallback queries
    time.sleep(0.1)        # pace between songs to avoid bursting the API
    return None


def find_existing_playlist(sp: spotipy.Spotify, name: str) -> str | None:
    """
    Return the ID of the first playlist owned by the current user whose name
    matches `name` exactly (case-insensitive), or None if not found.
    """
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items") or []:
            if pl and pl.get("name", "").lower() == name.lower():
                return pl["id"]
        results = sp.next(results) if results.get("next") else None
    return None


def create_playlist(sp: spotipy.Spotify, name: str, public: bool) -> str:
    """
    Create a new playlist for the current user via /me/playlists and return its ID.
    (Using /me/ avoids the 403 that the older /users/{id}/playlists endpoint gives.)
    """
    payload = {
        "name":          name,
        "public":        public,
        "collaborative": False,
        "description":   "Created by txt_to_playlist.py",
    }
    pl = sp._post("me/playlists", payload=payload)
    return pl["id"]


def get_playlist_track_uris(sp: spotipy.Spotify, playlist_id: str) -> set[str]:
    """Return the set of track URIs already in a playlist (all pages)."""
    uris: set[str] = set()
    # No `fields` filter — keep full response so sp.next() paginates correctly
    results = sp.playlist_items(playlist_id, limit=100)
    while results:
        for item in results.get("items") or []:
            # "track" in older API responses, "item" in newer ones
            track = (item or {}).get("track") or (item or {}).get("item")
            if track and track.get("uri"):
                uris.add(track["uri"])
        next_url = results.get("next")
        results  = sp._get(next_url) if next_url else None
    return uris


def add_tracks_to_playlist(
    sp: spotipy.Spotify,
    playlist_id: str,
    uris: list[str],
) -> None:
    """Add track URIs to a playlist in chunks of 100 (Spotify API limit)."""
    for i in range(0, len(uris), 100):
        _call_with_retry(sp.playlist_add_items, playlist_id, uris[i : i + 100])
        time.sleep(0.2)


# ── Core logic ────────────────────────────────────────────────────────────────
def process_file(
    sp: spotipy.Spotify,
    path: Path,
    user_id: str,
    *,
    public: bool = False,
    dry_run: bool = False,
) -> None:
    playlist_name = path.stem  # filename without extension

    print(f"\n{BOLD}{CYAN}{'[DRY RUN] ' if dry_run else ''}Processing:{RESET} {path.name}")
    print(f"  Playlist name : {BOLD}{playlist_name}{RESET}")

    entries = parse_txt(path)
    if not entries:
        print(f"  {YELLOW}No valid entries found — skipping.{RESET}")
        return

    print(f"  Songs to find : {len(entries)}\n")

    # ── Search Spotify for every entry ───────────────────────────────────────
    found_uris : list[str] = []
    not_found  : list[str] = []

    for song, artist in entries:
        uri = search_track(sp, song, artist)
        if uri:
            found_uris.append(uri)
            print(f"  {GREEN}✓{RESET} {song}  {DIM}— {artist}{RESET}")
        else:
            not_found.append(f"{song} — {artist}")
            print(f"  {RED}✗{RESET} {song}  {DIM}— {artist}{RESET}  {DIM}(not found){RESET}")

    total  = len(entries)
    n_ok   = len(found_uris)
    n_miss = len(not_found)
    print(f"\n  Found {GREEN}{n_ok}/{total}{RESET}", end="")
    if n_miss:
        print(f"  ({RED}{n_miss} not found{RESET})", end="")
    print()

    if dry_run:
        if not_found:
            print(f"\n  {YELLOW}Not-found songs:{RESET}")
            for entry in not_found:
                print(f"    - {entry}")
        print(f"\n  {DIM}Dry run — no playlist created/updated.{RESET}")
        return

    if not found_uris:
        print(f"  {YELLOW}Nothing to add — skipping.{RESET}")
        return

    # ── Find or create the playlist ───────────────────────────────────────────
    playlist_id = find_existing_playlist(sp, playlist_name)
    is_new      = playlist_id is None

    if is_new:
        playlist_id = create_playlist(sp, playlist_name, public=public)
        already_in  : set[str] = set()
        print(f"  {CYAN}No existing playlist found — created new one.{RESET}")
    else:
        already_in = get_playlist_track_uris(sp, playlist_id)
        print(f"  {CYAN}Found existing playlist ({len(already_in)} tracks already inside).{RESET}")

    # ── Diff: only add tracks not already present ─────────────────────────────
    new_uris = [u for u in found_uris if u not in already_in]
    skipped  = len(found_uris) - len(new_uris)

    if skipped:
        print(f"  {DIM}{skipped} track(s) already in playlist — skipping.{RESET}")

    if not new_uris:
        print(f"  {GREEN}All tracks already present — nothing to add.{RESET}")
    else:
        add_tracks_to_playlist(sp, playlist_id, new_uris)
        action     = "Created" if is_new else "Updated"
        visibility = "public"  if public  else "private"
        extra      = f" ({visibility})" if is_new else ""
        print(f"\n  {GREEN}{action}{extra} \"{playlist_name}\" — {len(new_uris)} new track(s) added.{RESET}")

    if not_found:
        print(f"\n  {YELLOW}Songs that couldn't be found on Spotify:{RESET}")
        for entry in not_found:
            print(f"    {DIM}- {entry}{RESET}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Create Spotify playlists from plain-text song lists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help=(
            "One or more .txt files to process. "
            f"If omitted, every .txt in ./{PLAYLISTS_DIR}/ is used."
        ),
    )
    p.add_argument(
        "--public",
        action="store_true",
        default=False,
        help="Make created playlists public (default: private).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Search for tracks and report results without creating any playlist.",
    )
    return p


def main() -> None:
    enable_windows_ansi()
    args = build_arg_parser().parse_args()

    # Resolve which files to process
    if args.files:
        paths = [Path(f) for f in args.files]
        for p in paths:
            if not p.exists():
                print(f"{RED}File not found:{RESET} {p}")
                sys.exit(1)
            if p.suffix.lower() != ".txt":
                print(f"{YELLOW}Warning:{RESET} {p.name} doesn't look like a .txt file — processing anyway.")
    else:
        if not PLAYLISTS_DIR.exists():
            print(f"{YELLOW}No files given and '{PLAYLISTS_DIR}/' folder not found.{RESET}")
            print(f"Create the folder and add .txt files, or pass file paths directly.")
            sys.exit(0)
        paths = sorted(PLAYLISTS_DIR.glob("*.txt"))
        if not paths:
            print(f"{YELLOW}No .txt files found in {PLAYLISTS_DIR}/.{RESET}")
            sys.exit(0)
        print(f"{DIM}No files specified — using all .txt files in {PLAYLISTS_DIR}/{RESET}")
        for p in paths:
            print(f"  {DIM}• {p.name}{RESET}")

    # Connect
    print(f"\n{BOLD}{CYAN}txt_to_playlist{RESET}  —  Connecting to Spotify...\n")
    sp = get_spotify_client()

    try:
        me = sp.current_user()
        print(f"{GREEN}Connected as {BOLD}{me['display_name']}{RESET}")
    except spotipy.SpotifyException as exc:
        print(f"{RED}Auth error: {exc}{RESET}")
        sys.exit(1)

    user_id = me["id"]

    # Process each file
    for path in paths:
        try:
            process_file(
                sp, path, user_id,
                public=args.public,
                dry_run=args.dry_run,
            )
        except spotipy.SpotifyException as exc:
            print(f"\n{RED}Spotify API error while processing {path.name}:{RESET} {exc}")

    print(f"\n{BOLD}{GREEN}Done!{RESET}")


if __name__ == "__main__":
    main()
