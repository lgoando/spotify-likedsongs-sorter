# -*- coding: utf-8 -*-
"""
Vibe Playlist Creator
Claude AI analyzes your Liked Songs, invents creative playlist themes
("Late Night Drive", "Hype Mode", "Sunday Morning Ease", …), then
curates 30–50 songs per theme and creates the playlists on Spotify.

Re-run options:
  • Add new liked songs to existing vibe playlists
  • Generate fresh vibes (Claude avoids themes you've already used)
"""

import os, sys, time, json, random
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("Run: python -m pip install spotipy python-dotenv anthropic")

try:
    import anthropic
except ImportError:
    sys.exit("Run: python -m pip install anthropic")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DESIGN_SAMPLE = 150   # tracks shown to Claude when designing vibes
NUM_VIBES     = 12    # how many vibes Claude proposes (user picks their favourites)
PLAYLIST_MIN  = 15    # drop a vibe if fewer songs end up in it
PLAYLIST_MAX  = 50    # cap each vibe playlist at this many songs

SCOPES = " ".join([
    "user-library-read",
    "playlist-modify-private",
    "playlist-modify-public",
])

VIBE_DESIGN_FILE    = Path(".vibe_design.json")    # active vibe themes
VIBE_CACHE_FILE     = Path(".vibe_cache.json")     # track → vibe_key
VIBE_PLAYLISTS_FILE = Path(".vibe_playlists.json") # vibe_key → Spotify playlist info
VIBE_HISTORY_FILE   = Path(".vibe_history.json")   # all vibe names ever created
AI_CACHE_FILE       = Path(".ai_cache.json")       # genre/mood from liked_sort.py

# ── Colours ───────────────────────────────────────────────────────────────────
def _c(code): return f"\033[{code}m"
BOLD=_c("1"); DIM=_c("2"); RED=_c("31"); GRN=_c("32")
YLW=_c("33"); CYN=_c("36"); RST=_c("0")

def enable_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# ── Spotify ───────────────────────────────────────────────────────────────────
sp: spotipy.Spotify

def auth() -> spotipy.Spotify:
    cid  = os.getenv("SPOTIFY_CLIENT_ID")
    csec = os.getenv("SPOTIFY_CLIENT_SECRET")
    ruri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    if not cid or not csec:
        print(f"{RED}Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env{RST}")
        sys.exit(1)
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=cid, client_secret=csec,
            redirect_uri=ruri, scope=SCOPES,
            cache_path=".spotify_cache", open_browser=True,
        ),
        requests_timeout=15,
    )


def fetch_liked_songs() -> list[dict]:
    print(f"{CYN}Fetching liked songs...{RST}", end="", flush=True)
    tracks, results = [], sp.current_user_saved_tracks(limit=50)
    while True:
        for item in results["items"]:
            t = item.get("track")
            if t and t.get("id"):
                tracks.append(t)
        if not results["next"]:
            break
        results = sp.next(results)
        print(".", end="", flush=True)
    print(f" {GRN}{len(tracks)} tracks{RST}")
    return tracks


def create_playlist(name: str, track_ids: list[str],
                    description: str = "Created by vibe_sort.py") -> str:
    """Create a private playlist via /me/playlists (works for new Spotify apps)."""
    pl = sp._post("me/playlists", payload={
        "name": name, "public": False, "description": description,
    })
    for i in range(0, len(track_ids), 100):
        sp.playlist_add_items(pl["id"],
                              [f"spotify:track:{tid}" for tid in track_ids[i:i+100]])
        time.sleep(0.15)
    return pl["external_urls"]["spotify"]


def add_to_playlist(playlist_id: str, track_ids: list[str]) -> None:
    """Add tracks to an existing playlist, skipping duplicates."""
    for i in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist_id,
                              [f"spotify:track:{tid}" for tid in track_ids[i:i+100]])
        time.sleep(0.15)


# ── Shared helpers ────────────────────────────────────────────────────────────

def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text[text.find("\n") + 1:]
        if "```" in text:
            text = text[:text.rfind("```")].strip()
    return text


def track_summary(t: dict, ai_cache: dict) -> dict:
    """Compact track dict for Claude, enriched with genre/mood if available."""
    tid    = t.get("id", "")
    cached = ai_cache.get(tid, {})
    d = {
        "id":     tid,
        "artist": t["artists"][0]["name"] if t.get("artists") else "Unknown",
        "title":  t["name"],
        "album":  t.get("album", {}).get("name", ""),
        "year":   (t.get("album", {}).get("release_date") or "")[:4],
    }
    if cached.get("genre"):  d["genre"] = cached["genre"]
    if cached.get("mood"):   d["mood"]  = cached["mood"]
    return d


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Phase 1: Vibe design ──────────────────────────────────────────────────────

def design_vibes(tracks: list[dict],
                 ai_cache: dict,
                 client: anthropic.Anthropic,
                 avoid_names: list[str] | None = None) -> list[dict]:
    """
    Sample the library and ask Claude to invent NUM_VIBES creative vibe themes.
    `avoid_names` — vibe names from previous runs, passed to Claude to avoid repeating.
    Returns list of {key, name, description}.
    """
    sample      = random.sample(tracks, min(DESIGN_SAMPLE, len(tracks)))
    sample_data = [track_summary(t, ai_cache) for t in sample]

    avoid_block = ""
    if avoid_names:
        names_list = ", ".join(f'"{n}"' for n in avoid_names)
        avoid_block = (
            f"\nIMPORTANT: These vibe names have already been used. "
            f"Do NOT reuse them or create anything too similar:\n{names_list}\n"
        )

    print(f"  Sampling {len(sample)} songs and asking Claude to design vibes...",
          flush=True)

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"""Here is a sample of {len(sample_data)} songs from a user's Spotify liked library:
{json.dumps(sample_data, ensure_ascii=False)}
{avoid_block}
Design exactly {NUM_VIBES} creative, distinct playlist vibes for this library.

Rules:
- Each vibe gets an evocative name (2–4 words). NOT a genre label — think mood, aesthetic, activity, or feeling.
- Make each vibe clearly different from the others.
- Base them on what you actually see in this library.
- Each should work as a satisfying 30–50 song playlist.

Good vibe name examples: "Late Night Drive", "Sunday Morning Ease", "Rage and Release",
"Bittersweet Nostalgia", "Confident Strut", "Basement Party", "Daydream Sequence",
"Golden Hour Glow", "Pressure Drop", "Running From Something"

Return ONLY a JSON array, nothing else:
[{{"key": "snake_case_key", "name": "Human Name", "description": "One sentence describing who this playlist is for and when"}}]""",
        }],
    )

    raw   = next(b.text for b in response.content if b.type == "text")
    vibes = json.loads(strip_fences(raw))
    return [
        {"key": v["key"], "name": v["name"], "description": v["description"]}
        for v in vibes
        if v.get("key") and v.get("name") and v.get("description")
    ]


def pick_vibes(vibes: list[dict]) -> list[dict]:
    """Show the designed vibes and let the user select which ones to keep."""
    print()
    for i, v in enumerate(vibes, 1):
        print(f"  {BOLD}{i:>2}.{RST} {BOLD}{v['name']}{RST}")
        print(f"       {DIM}{v['description']}{RST}")

    print(f"\n  Enter the numbers you want, separated by commas.")
    print(f"  {DIM}Or press Enter to keep all {len(vibes)}.{RST}")

    while True:
        raw = input("\n  Your picks: ").strip()
        if not raw:
            return vibes  # keep all
        try:
            indices = [int(x.strip()) - 1 for x in raw.split(",")]
            selected = [vibes[i] for i in indices if 0 <= i < len(vibes)]
            if selected:
                return selected
        except (ValueError, IndexError):
            pass
        print(f"  {RED}Enter numbers like: 1, 3, 5{RST}")


# ── Phase 2: Song assignment ──────────────────────────────────────────────────

def assign_vibes(tracks: list[dict],
                 vibes: list[dict],
                 ai_cache: dict,
                 client: anthropic.Anthropic) -> dict[str, str]:
    """
    Ask Claude to assign every track to a vibe (or "none").
    Returns {track_id -> vibe_key}.  Caches to .vibe_cache.json.
    """
    cache: dict[str, str] = load_json(VIBE_CACHE_FILE, {})

    valid_keys = {v["key"] for v in vibes} | {"none"}
    # Purge stale cache entries whose vibe key no longer exists
    cache = {tid: vk for tid, vk in cache.items() if vk in valid_keys}

    uncached = [t for t in tracks if t.get("id") and t["id"] not in cache]

    if not uncached:
        print(f"  {GRN}All {len(tracks)} tracks loaded from vibe cache.{RST}")
        return cache

    cached_count = len(tracks) - len(uncached)
    if cached_count:
        print(f"  {DIM}{cached_count} cached, classifying {len(uncached)} new songs...{RST}")

    vibes_text = json.dumps(
        [{"key": v["key"], "name": v["name"], "description": v["description"]}
         for v in vibes],
        ensure_ascii=False, indent=2,
    )

    system_prompt = f"""You are a music curator. Assign each track to the single best matching vibe.
Use "none" only if the track genuinely doesn't fit any vibe at all.

Vibes:
{vibes_text}

Return ONLY a JSON array: [{{"id": "track_id", "vibe": "vibe_key_or_none"}}]
No markdown, no explanation."""

    batch_size    = 50
    total_batches = (len(uncached) + batch_size - 1) // batch_size

    for batch_num, i in enumerate(range(0, len(uncached), batch_size), 1):
        batch       = uncached[i : i + batch_size]
        tracks_data = [track_summary(t, ai_cache) for t in batch]

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Assign these {len(batch)} tracks:\n"
                        f"{json.dumps(tracks_data, ensure_ascii=False)}"
                    ),
                }],
            )

            raw         = next(b.text for b in response.content if b.type == "text")
            assignments = json.loads(strip_fences(raw))
            ok = 0
            for a in assignments:
                tid  = a.get("id")
                vibe = a.get("vibe", "none")
                if not tid:
                    continue
                if vibe not in valid_keys:
                    vibe = "none"
                cache[tid] = vibe
                ok += 1

            print(f"  {GRN}Batch {batch_num}/{total_batches}:{RST} "
                  f"{ok}/{len(batch)} songs assigned")

        except json.JSONDecodeError as e:
            print(f"  {YLW}Batch {batch_num}: JSON error — {e}{RST}")
        except Exception as e:
            print(f"  {YLW}Batch {batch_num}: error — {e}{RST}")

        save_json(VIBE_CACHE_FILE, cache)

    return cache


# ── Flow A: First run (or fresh vibes) ───────────────────────────────────────

def run_fresh(tracks: list[dict],
              ai_cache: dict,
              client: anthropic.Anthropic,
              avoid_names: list[str]) -> None:
    """Design vibes, let user pick, assign songs, create playlists."""

    # ── Design ───────────────────────────────────────────────────────────────
    print(f"\n{BOLD}Step 1/2  —  Designing vibes{RST}")
    proposed = design_vibes(tracks, ai_cache, client, avoid_names or None)

    print(f"\n  {GRN}✓ Claude proposed {len(proposed)} vibes. Pick the ones you like:{RST}")
    vibes = pick_vibes(proposed)
    print(f"\n  {GRN}Keeping {len(vibes)} vibe{'s' if len(vibes) != 1 else ''}:{RST} "
          + ", ".join(f"{BOLD}{v['name']}{RST}" for v in vibes))

    save_json(VIBE_DESIGN_FILE, {"vibes": vibes})

    # Update history (so future fresh runs avoid these names)
    history: list[str] = load_json(VIBE_HISTORY_FILE, [])
    history.extend(v["name"] for v in vibes if v["name"] not in history)
    save_json(VIBE_HISTORY_FILE, history)

    # Clear old assignments (vibe keys changed)
    VIBE_CACHE_FILE.unlink(missing_ok=True)
    VIBE_PLAYLISTS_FILE.unlink(missing_ok=True)

    # ── Assign ────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}Step 2/2  —  Sorting {len(tracks)} songs into vibes{RST}")
    assignments = assign_vibes(tracks, vibes, ai_cache, client)

    _build_and_create_playlists(tracks, vibes, assignments)


# ── Flow B: Update existing playlists with new liked songs ───────────────────

def run_update(tracks: list[dict],
               vibes: list[dict],
               ai_cache: dict,
               client: anthropic.Anthropic) -> None:
    """Classify songs that weren't in the last run and add them to existing playlists."""

    playlists_meta: dict = load_json(VIBE_PLAYLISTS_FILE, {})
    if not playlists_meta:
        print(f"{YLW}No saved playlist IDs found — can't update existing playlists.{RST}")
        print(f"{DIM}Run a fresh vibe sort first so the playlist IDs are saved.{RST}")
        return

    print(f"\n{BOLD}Assigning new songs to vibes...{RST}")
    assignments = assign_vibes(tracks, vibes, ai_cache, client)

    track_by_id = {t["id"]: t for t in tracks if t.get("id")}
    vibe_groups: defaultdict[str, list] = defaultdict(list)
    for tid, vkey in assignments.items():
        if vkey != "none" and tid in track_by_id:
            vibe_groups[vkey].append(track_by_id[tid])

    # Find tracks that are newly assigned (not in the playlist yet)
    # We use the vibe cache written *before* this run to detect new ones
    # (anything that was already cached and assigned is already in the playlist)
    existing_cache: dict = {}
    # Load the cache as it was before assign_vibes() was called —
    # assign_vibes only adds new tracks so anything in cache = already processed.
    # The simplest proxy: tracks with ids that were in the old cache already.
    old_cache: dict = load_json(VIBE_CACHE_FILE, {})

    print(f"\n{BOLD}Adding new songs to existing playlists:{RST}")
    total_added = 0
    for v in vibes:
        key   = v["key"]
        meta  = playlists_meta.get(key)
        if not meta:
            print(f"  {DIM}{v['name']}: no saved playlist ID, skipping{RST}")
            continue

        pl_id   = meta["playlist_id"]
        bucket  = vibe_groups.get(key, [])
        # Only songs that got assigned this run for the first time would be
        # "new" — but since assign_vibes uses a cache, newly added liked songs
        # will be the only uncached ones, meaning all assignments for those
        # tracks are fresh. Cross-reference: a track is "new" if it wasn't
        # in the vibe cache before this session started.
        # (We reload VIBE_CACHE_FILE which now includes both old + new.)
        new_tracks = [t for t in bucket if t["id"] not in old_cache]

        if not new_tracks:
            print(f"  {DIM}{v['name']}: no new songs{RST}")
            continue

        print(f"  {GRN}{v['name']}:{RST} adding {len(new_tracks)} new songs...",
              end="", flush=True)
        try:
            add_to_playlist(pl_id, [t["id"] for t in new_tracks])
            total_added += len(new_tracks)
            print(f" {GRN}done{RST}")
        except Exception as e:
            print(f" {RED}failed: {e}{RST}")

    print(f"\n{GRN}{BOLD}Done! Added {total_added} songs across "
          f"{len(vibes)} playlists.{RST}\n")


# ── Shared: build groups → create playlists ───────────────────────────────────

def _build_and_create_playlists(tracks: list[dict],
                                 vibes: list[dict],
                                 assignments: dict[str, str]) -> None:
    track_by_id = {t["id"]: t for t in tracks if t.get("id")}
    vibe_groups: defaultdict[str, list] = defaultdict(list)
    for tid, vkey in assignments.items():
        if vkey != "none" and tid in track_by_id:
            vibe_groups[vkey].append(track_by_id[tid])

    # ── Trim / drop ───────────────────────────────────────────────────────────
    print(f"\n{BOLD}Playlist breakdown:{RST}")
    ready: list[dict] = []

    for v in vibes:
        key    = v["key"]
        bucket = vibe_groups.get(key, [])
        n      = len(bucket)

        if n < PLAYLIST_MIN:
            print(f"  {DIM}{v['name']:<30} {n:>4} songs  skipped (< {PLAYLIST_MIN}){RST}")
            continue

        if n > PLAYLIST_MAX:
            bucket = random.sample(bucket, PLAYLIST_MAX)
            note   = f"trimmed to {PLAYLIST_MAX}"
            colour = YLW
        else:
            note   = ""
            colour = GRN

        random.shuffle(bucket)
        print(f"  {colour}{v['name']:<30}{RST} {len(bucket):>4} songs"
              + (f"  {DIM}{note}{RST}" if note else ""))
        ready.append({"vibe": v, "tracks": bucket})

    unassigned = sum(
        1 for t in tracks
        if assignments.get(t.get("id", ""), "none") == "none"
    )
    print(f"\n  {DIM}{unassigned} songs didn't match any vibe{RST}")

    if not ready:
        print(f"\n{RED}No vibes had enough songs. "
              f"Try regenerating vibes.{RST}")
        return

    # ── Playlist name prefix ──────────────────────────────────────────────────
    default_prefix = "My Vibes"
    raw = input(f"\nPlaylist name prefix [{default_prefix}]: ").strip()
    prefix = raw if raw else default_prefix

    # ── Confirm ───────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Will create {len(ready)} playlists:{RST}")
    for rv in ready:
        pl_name = f"{prefix} — {rv['vibe']['name']}"
        print(f"    \"{pl_name}\"  ({len(rv['tracks'])} tracks)")

    ok = input(f"\nCreate these {len(ready)} playlists? [y/N] ").strip().lower()
    if ok != "y":
        print("Cancelled.")
        return

    # ── Create ────────────────────────────────────────────────────────────────
    print(f"\n{CYN}Creating playlists on Spotify...{RST}")
    playlists_meta: dict = {}
    created = []

    for rv in ready:
        v         = rv["vibe"]
        pl_name   = f"{prefix} — {v['name']}"
        pl_desc   = f"{v['description']} | Made by vibe_sort.py"
        track_ids = [t["id"] for t in rv["tracks"]]
        try:
            url = create_playlist(pl_name, track_ids, description=pl_desc)
            pl_id = url.split("/")[-1]   # extract playlist ID from URL
            playlists_meta[v["key"]] = {
                "playlist_id":   pl_id,
                "playlist_name": pl_name,
                "url":           url,
            }
            created.append((pl_name, url, len(track_ids)))
            print(f"  {GRN}✓{RST} \"{pl_name}\"  ({len(track_ids)} tracks)")
        except Exception as e:
            print(f"  {RED}✗{RST} \"{pl_name}\"  failed: {e}")
        time.sleep(0.2)

    # Save playlist IDs so the update flow can add songs later
    save_json(VIBE_PLAYLISTS_FILE, playlists_meta)

    print(f"\n{GRN}{BOLD}Done! Created {len(created)} vibe playlists:{RST}")
    for pl_name, url, count in created:
        print(f"  {count:>3} tracks  {url}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global sp
    enable_ansi()

    print(f"\n{BOLD}{CYN}♦ Vibe Playlist Creator{RST}")
    print(f"{DIM}Claude AI invents playlist themes from your library "
          f"and curates the songs.{RST}\n")

    ai_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not ai_key:
        print(f"{RED}ANTHROPIC_API_KEY not found in .env{RST}")
        print("Get one at https://console.anthropic.com/")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=ai_key)

    print(f"{DIM}Connecting to Spotify...{RST}")
    sp = auth()
    me = sp.current_user()
    print(f"{GRN}Connected as {BOLD}{me['display_name']}{RST}\n")

    tracks = fetch_liked_songs()

    # Load genre/mood context from liked_sort.py cache if available
    ai_cache: dict[str, dict] = {}
    if AI_CACHE_FILE.exists():
        try:
            ai_cache = json.loads(AI_CACHE_FILE.read_text(encoding="utf-8"))
            pct = round(len(ai_cache) / len(tracks) * 100) if tracks else 0
            print(f"{DIM}Loaded genre/mood context for "
                  f"{len(ai_cache)}/{len(tracks)} tracks ({pct}%) from AI cache.{RST}")
        except Exception:
            pass

    history: list[str] = load_json(VIBE_HISTORY_FILE, [])

    # ── Decide what to do ─────────────────────────────────────────────────────
    saved      = load_json(VIBE_DESIGN_FILE, {})
    saved_vibes: list[dict] = saved.get("vibes", [])

    if saved_vibes:
        print(f"\n{BOLD}You already have vibe playlists:{RST}")
        for v in saved_vibes:
            print(f"  {BOLD}• {v['name']}{RST}  {DIM}{v['description']}{RST}")

        print(f"\n{BOLD}What would you like to do?{RST}")
        print(f"  {BOLD}1.{RST} Add new liked songs to these playlists")
        print(f"  {BOLD}2.{RST} Generate fresh vibes  "
              f"{DIM}(Claude won't reuse: "
              + ", ".join(f'\"{n}\"' for n in history[:4])
              + (" …" if len(history) > 4 else "") + f"){RST}")
        print(f"  {BOLD}3.{RST} Start completely from scratch  "
              f"{DIM}(clear history too){RST}")

        while True:
            raw = input("\nChoice [1/2/3]: ").strip()
            if raw in ("1", "2", "3"):
                mode = int(raw); break
            print(f"{RED}Enter 1, 2, or 3.{RST}")

        if mode == 1:
            run_update(tracks, saved_vibes, ai_cache, client)
            return

        elif mode == 2:
            # Fresh vibes, Claude avoids history
            run_fresh(tracks, ai_cache, client, history)
            return

        elif mode == 3:
            # Full reset
            for f in (VIBE_DESIGN_FILE, VIBE_CACHE_FILE,
                      VIBE_PLAYLISTS_FILE, VIBE_HISTORY_FILE):
                f.unlink(missing_ok=True)
            print(f"{YLW}History cleared.{RST}")
            run_fresh(tracks, ai_cache, client, [])
            return

    else:
        # First run
        run_fresh(tracks, ai_cache, client, history)


if __name__ == "__main__":
    main()
