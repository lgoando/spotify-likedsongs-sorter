# -*- coding: utf-8 -*-
"""
Export Liked Songs as plaintext
Outputs every liked track in the format: SONG - ARTIST
Saves to liked_songs.txt (or a path you specify) and prints to stdout.
"""

import os, sys
from pathlib import Path
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("Run: python -m pip install spotipy python-dotenv")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

SCOPES = "user-library-read"


def auth() -> spotipy.Spotify:
    cid  = os.getenv("SPOTIFY_CLIENT_ID")
    csec = os.getenv("SPOTIFY_CLIENT_SECRET")
    ruri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    if not cid or not csec:
        sys.exit(
            "Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env\n"
            "Copy .env.example -> .env and fill in the values."
        )

    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=cid, client_secret=csec,
            redirect_uri=ruri, scope=SCOPES,
            cache_path=".spotify_cache", open_browser=True,
        ),
        requests_timeout=15,
    )


def fetch_liked_songs(sp: spotipy.Spotify) -> list[tuple[str, str]]:
    """Return list of (song_title, artist_name) in the order Spotify returns them."""
    print("Fetching liked songs...", end="", flush=True)
    results = sp.current_user_saved_tracks(limit=50)
    songs: list[tuple[str, str]] = []

    while True:
        for item in results["items"]:
            track = item.get("track")
            if not track:
                continue
            title  = track.get("name", "Unknown")
            artist = track["artists"][0]["name"] if track.get("artists") else "Unknown"
            songs.append((title, artist))

        if not results["next"]:
            break
        results = sp.next(results)
        print(".", end="", flush=True)

    print(f" {len(songs)} tracks")
    return songs


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("liked_songs.txt")

    sp = auth()
    songs = fetch_liked_songs(sp)

    lines = [f"{title} - {artist}" for title, artist in songs]
    text  = "\n".join(lines)

    out_path.write_text(text, encoding="utf-8")
    print(f"\nSaved {len(lines)} tracks to {out_path}")

    # Also print to stdout so you can pipe it if you like
    print()
    print(text)


if __name__ == "__main__":
    main()
