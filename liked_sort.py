# -*- coding: utf-8 -*-
"""
Liked Songs Sorter
Fetches your Spotify Liked Songs, enriches them with genre + mood data
from Claude AI and/or Last.fm, then saves the result as new sorted playlists.
"""

import os, sys, time, json, requests
from pathlib import Path
from collections import defaultdict, Counter
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("Run: python -m pip install spotipy python-dotenv requests")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ── Spotify scopes ────────────────────────────────────────────────────────────
SCOPES = " ".join([
    "user-library-read",
    "playlist-modify-private",
    "playlist-modify-public",
])

# ── Last.fm ───────────────────────────────────────────────────────────────────
LASTFM_BASE  = "http://ws.audioscrobbler.com/2.0/"
LASTFM_CACHE = Path(".lastfm_cache.json")

# ── Claude AI cache ───────────────────────────────────────────────────────────
AI_CACHE = Path(".ai_cache.json")

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

# ── Genre keyword map ─────────────────────────────────────────────────────────
# Indie/Alt must come BEFORE Pop so "indie pop" tags land in the right bucket.
MACRO_GENRES = [
    ("Hip-Hop / Rap",          ["hip hop", "hip-hop", "rap", "trap", "drill",
                                 "boom bap", "grime", "lo-fi hip hop"]),
    ("R&B / Soul",             ["r&b", "rnb", "soul", "neo soul", "funk",
                                 "rhythm and blues", "motown"]),
    ("Metal",                  ["metal", "deathcore", "metalcore",
                                 "black metal", "death metal"]),
    ("Electronic",             ["electronic", "edm", "house", "techno", "trance",
                                 "dubstep", "drum and bass", "drum n bass",
                                 "ambient", "synth", "electro", "breakbeat"]),
    ("Indie / Alternative",    ["indie", "alternative", "lo-fi", "shoegaze",
                                 "dream pop", "art pop", "chamber pop",
                                 "post punk", "new wave"]),
    ("Pop",                    ["pop"]),
    ("Rock",                   ["rock", "grunge", "punk", "emo",
                                 "post-rock", "garage", "hardcore"]),
    ("Country",                ["country", "bluegrass", "americana"]),
    ("Jazz",                   ["jazz", "bebop", "swing", "blues"]),
    ("Classical / Orchestral", ["classical", "orchestra", "opera",
                                 "chamber", "baroque", "symphony"]),
    ("Latin",                  ["latin", "reggaeton", "salsa",
                                 "bossa nova", "cumbia", "bachata"]),
    ("Folk",                   ["folk", "singer-songwriter", "acoustic"]),
    ("Reggae",                 ["reggae", "dub", "ska"]),
]

GENRE_LABELS = [label for label, _ in MACRO_GENRES] + ["Other"]

def macro_genre(tags: list[str]) -> str:
    """Map a list of tags/genre strings to one macro-genre label."""
    for label, keywords in MACRO_GENRES:
        for tag in tags:
            tag_l = tag.lower()
            if any(kw in tag_l for kw in keywords):
                return label
    return "Other"

# ── Mood keyword map ──────────────────────────────────────────────────────────
# These are common Last.fm user tags that describe mood/feel rather than genre.
MOOD_TAGS = {
    "Melancholic": [
        "melancholic", "melancholy", "sad", "sadness", "depressing",
        "depression", "lonely", "loneliness", "heartbreak", "bittersweet",
        "somber", "gloomy", "dark", "bleak", "moody", "tearjerker",
        "introspective", "reflective", "emotional", "wistful", "haunting",
    ],
    "Peaceful": [
        "chill", "chillout", "chill out", "relaxing", "relaxation",
        "mellow", "calm", "peaceful", "laid-back", "laid back",
        "easy listening", "soft", "soothing", "tranquil", "sleep",
        "study", "focus", "background", "slow", "gentle", "dreamy",
        "meditative", "serene",
    ],
    "Upbeat": [
        "happy", "happiness", "uplifting", "feel-good", "feel good",
        "cheerful", "joyful", "positive", "fun", "catchy", "summer",
        "sunshine", "upbeat", "danceable", "groovy", "infectious",
        "euphoric", "energetic", "dance", "party", "sing along",
    ],
    "Intense": [
        "aggressive", "intense", "angry", "powerful", "heavy", "driving",
        "epic", "workout", "gym", "pump up", "headbanger", "adrenaline",
        "fierce", "raw", "brutal", "hard", "loud", "fast", "rebellious",
    ],
}

MOOD_ORDER  = ["Melancholic", "Peaceful", "Upbeat", "Intense"]
MOOD_LABELS = MOOD_ORDER + ["Unknown"]

def mood_from_tags(tags: list[str]) -> str | None:
    """Infer a mood label from a list of Last.fm tags."""
    tags_lower = [t.lower() for t in tags]
    combined   = " ".join(tags_lower)
    for mood, keywords in MOOD_TAGS.items():
        if any(kw in combined for kw in keywords):
            return mood
    return None


# ── Claude AI system prompt ───────────────────────────────────────────────────
_genre_list = "\n".join(f"- {g}" for g in GENRE_LABELS)
AI_SYSTEM_PROMPT = f"""You are a music expert. Classify each track into exactly one genre and one mood.

Genre options (choose the single best match):
{_genre_list}

Mood options:
- Melancholic  (sad, dark, introspective, bittersweet, haunting)
- Peaceful     (chill, calm, relaxing, mellow, dreamy, ambient)
- Upbeat       (happy, energetic, danceable, cheerful, feel-good, party)
- Intense      (aggressive, powerful, heavy, workout, epic, adrenaline)
- Unknown      (only if you truly cannot determine the mood)

Use your knowledge of the artist, song title, and album to classify.
Respond with ONLY a valid JSON array. Each element must have exactly these fields:
{{"id": "<track id>", "genre": "<genre label>", "mood": "<mood label>"}}
No markdown, no code fences, no explanation — just the raw JSON array."""


# ── Claude AI classification ──────────────────────────────────────────────────

def classify_with_claude(tracks: list[dict], api_key: str) -> dict[str, dict]:
    """
    Use Claude to classify each track by genre and mood.
    Returns {track_id -> {"genre": ..., "mood": ...}}.
    Results are cached to .ai_cache.json so repeat runs are instant.
    """
    try:
        import anthropic
    except ImportError:
        print(f"{YLW}anthropic package not installed.{RST}")
        print(f"{DIM}Run: python -m pip install anthropic{RST}")
        return {}

    client = anthropic.Anthropic(api_key=api_key)

    # Load existing cache
    cache: dict[str, dict] = {}
    if AI_CACHE.exists():
        try:
            cache = json.loads(AI_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    uncached = [t for t in tracks if t.get("id") and t["id"] not in cache]

    if not uncached:
        print(f"{GRN}All {len(tracks)} tracks loaded from AI cache.{RST}")
        return cache

    cached_count = len(tracks) - len(uncached)
    print(f"{CYN}Classifying {len(uncached)} tracks with Claude AI"
          + (f" ({cached_count} already cached)" if cached_count else "")
          + f"...{RST}", flush=True)

    batch_size    = 50
    total_batches = (len(uncached) + batch_size - 1) // batch_size

    for batch_num, i in enumerate(range(0, len(uncached), batch_size), 1):
        batch = uncached[i : i + batch_size]
        tracks_data = [
            {
                "id":     t["id"],
                "artist": t["artists"][0]["name"] if t.get("artists") else "Unknown",
                "title":  t["name"],
                "album":  t["album"]["name"] if t.get("album") else "",
                "year":   (t["album"].get("release_date") or "")[:4] if t.get("album") else "",
            }
            for t in batch
        ]

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": AI_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # reused across all batches
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Classify these {len(batch)} tracks:\n"
                        f"{json.dumps(tracks_data, ensure_ascii=False)}"
                    ),
                }],
            )

            # Extract text content
            text = next((b.text for b in response.content if b.type == "text"), "")

            # Strip markdown code fences if Claude adds them despite instructions
            text = text.strip()
            if text.startswith("```"):
                # Remove opening fence line (```json or ```)
                text = text[text.find("\n") + 1:]
                # Remove closing fence
                if "```" in text:
                    text = text[: text.rfind("```")].strip()

            classifications = json.loads(text)
            ok = 0
            for c in classifications:
                tid = c.get("id")
                if not tid:
                    continue
                genre = c.get("genre", "Other")
                mood  = c.get("mood",  "Unknown")
                # Validate against known labels, fall back gracefully
                if genre not in GENRE_LABELS:
                    genre = "Other"
                if mood not in MOOD_LABELS:
                    mood = "Unknown"
                cache[tid] = {"genre": genre, "mood": mood}
                ok += 1

            print(f"  {GRN}Batch {batch_num}/{total_batches}:{RST} "
                  f"{ok}/{len(batch)} tracks classified")

        except json.JSONDecodeError as e:
            print(f"  {YLW}Batch {batch_num}: JSON parse error — {e}{RST}")
        except Exception as e:
            print(f"  {YLW}Batch {batch_num}: error — {e}{RST}")

        # Save after every batch so progress survives interruptions
        AI_CACHE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    classified = sum(1 for t in tracks if t.get("id") and t["id"] in cache)
    print(f"{GRN}Claude classification done "
          f"({classified}/{len(tracks)} tracks in cache).{RST}")
    return cache


# ── Last.fm cache ─────────────────────────────────────────────────────────────

def load_lastfm_cache() -> dict:
    if LASTFM_CACHE.exists():
        try:
            return json.loads(LASTFM_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_lastfm_cache(cache: dict) -> None:
    LASTFM_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False),
                             encoding="utf-8")


# ── Last.fm fetch ─────────────────────────────────────────────────────────────

def fetch_lastfm_tags_for_artist(artist_name: str, api_key: str) -> list[str]:
    """Return the top tag names for one artist from Last.fm (up to 15)."""
    try:
        resp = requests.get(LASTFM_BASE, params={
            "method":      "artist.getTopTags",
            "artist":      artist_name,
            "api_key":     api_key,
            "format":      "json",
            "autocorrect": 1,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):   # Last.fm returns a dict for a single tag
            tags = [tags]
        return [t["name"] for t in tags[:15]]
    except Exception:
        return []


def fetch_lastfm_data(tracks: list[dict], api_key: str
                      ) -> tuple[dict[str, str], dict[str, str]]:
    """
    For every unique artist in `tracks`, fetch top tags from Last.fm.
    Returns:
        artist_genre_map  {artist_name -> macro_genre label}
        artist_mood_map   {artist_name -> mood label | None}
    Results are cached to .lastfm_cache.json so repeat runs are instant.
    """
    unique_artists: list[str] = []
    seen: set[str] = set()
    for t in tracks:
        if t.get("artists"):
            name = t["artists"][0].get("name", "")
            if name and name not in seen:
                seen.add(name)
                unique_artists.append(name)

    cache = load_lastfm_cache()
    missing = [a for a in unique_artists if a not in cache]

    if missing:
        print(f"{CYN}Fetching Last.fm tags for {len(missing)} artists "
              f"({len(unique_artists) - len(missing)} cached)...{RST}",
              end="", flush=True)
        for i, artist in enumerate(missing):
            tags = fetch_lastfm_tags_for_artist(artist, api_key)
            cache[artist] = tags
            if i % 10 == 9:
                print(".", end="", flush=True)
                save_lastfm_cache(cache)
            time.sleep(0.22)
        save_lastfm_cache(cache)
        print(f" {GRN}done{RST}")
    else:
        print(f"{GRN}All {len(unique_artists)} artists loaded from Last.fm cache.{RST}")

    genre_map: dict[str, str] = {}
    mood_map:  dict[str, str | None] = {}
    for artist in unique_artists:
        tags             = cache.get(artist, [])
        genre_map[artist] = macro_genre(tags)
        mood_map[artist]  = mood_from_tags(tags)

    return genre_map, mood_map


# ── Mood from audio features (Spotify, if available) ─────────────────────────

def mood_label_from_features(valence: float, energy: float) -> str:
    if valence >= 0.5 and energy >= 0.5: return "Upbeat"
    if valence >= 0.5 and energy <  0.5: return "Peaceful"
    if valence <  0.5 and energy >= 0.5: return "Intense"
    return "Melancholic"


def compute_track_moods(tracks: list[dict],
                        features:    dict[str, dict],
                        artist_mood: dict[str, str | None],
                        ai_cache:    dict[str, dict] | None = None,
                        ) -> dict[str, str]:
    """
    Build {track_id -> mood_label}.
    Priority: Spotify audio features > Claude AI > Last.fm artist-level mood.
    """
    result: dict[str, str] = {}
    for t in tracks:
        tid = t["id"]
        # 1. Spotify audio features (most reliable — actual signal analysis)
        f = features.get(tid)
        if f:
            result[tid] = mood_label_from_features(f["valence"], f["energy"])
            continue
        # 2. Claude AI (per-track knowledge)
        if ai_cache:
            ai_mood = ai_cache.get(tid, {}).get("mood", "Unknown")
            if ai_mood and ai_mood != "Unknown":
                result[tid] = ai_mood
                continue
        # 3. Last.fm artist-level mood (fallback)
        aname = t["artists"][0].get("name", "") if t.get("artists") else ""
        mood  = artist_mood.get(aname)
        if mood:
            result[tid] = mood
    return result


# ── Genre lookup helper ───────────────────────────────────────────────────────

def get_track_genre(t: dict,
                    genre_map:       dict[str, str],
                    track_genre_map: dict[str, str]) -> str:
    """
    Per-track genre (Claude AI) takes priority over artist-level (Last.fm).
    Falls back to "Other" if neither source has data.
    """
    tid = t.get("id", "")
    if tid and tid in track_genre_map:
        return track_genre_map[tid]
    aname = t["artists"][0].get("name", "") if t.get("artists") else ""
    return genre_map.get(aname, "Other")


# ── Spotify helpers ───────────────────────────────────────────────────────────
sp: spotipy.Spotify

def auth() -> spotipy.Spotify:
    cid  = os.getenv("SPOTIFY_CLIENT_ID")
    csec = os.getenv("SPOTIFY_CLIENT_SECRET")
    ruri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    if not cid or not csec:
        print(f"{RED}Missing SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env{RST}")
        print("\nSteps:")
        print("  1. https://developer.spotify.com/dashboard -> create an app")
        print("  2. Add  http://127.0.0.1:8888/callback  as a Redirect URI")
        print("  3. Copy .env.example -> .env and fill in the values")
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


def fetch_audio_features(tracks: list[dict]) -> dict[str, dict]:
    """Returns {} silently if Spotify has restricted access."""
    ids = [t["id"] for t in tracks]
    print(f"{CYN}Fetching Spotify audio features...{RST}", end="", flush=True)
    features: dict[str, dict] = {}
    for i in range(0, len(ids), 100):
        try:
            resp = sp.audio_features(ids[i:i+100])
        except spotipy.SpotifyException as e:
            if e.http_status in (401, 403):
                print(f" {YLW}unavailable (HTTP {e.http_status}){RST}")
                return {}
            raise
        if resp:
            for f in resp:
                if f:
                    features[f["id"]] = f
        print(".", end="", flush=True)
    print(f" {GRN}done{RST}")
    return features


def create_playlist(name: str, track_ids: list[str],
                    description: str = "Created by liked_sort.py") -> str:
    """Create one private playlist and return its Spotify URL."""
    # Use /me/playlists — the /users/{id}/playlists endpoint is 403 for new apps
    pl = sp._post("me/playlists", payload={
        "name": name, "public": False, "description": description,
    })
    for i in range(0, len(track_ids), 100):
        sp.playlist_add_items(pl["id"],
                              [f"spotify:track:{tid}" for tid in track_ids[i:i+100]])
        time.sleep(0.15)
    return pl["external_urls"]["spotify"]


def create_split_playlists(groups: dict[str, list[dict]],
                           name_prefix: str) -> list[tuple[str, str, int]]:
    """
    Create one playlist per key in `groups`.
    Returns list of (playlist_name, url, track_count).
    """
    results = []
    for label in sorted(groups.keys()):
        bucket  = groups[label]
        pl_name = f"{name_prefix} - {label}"
        url     = create_playlist(
            pl_name,
            [t["id"] for t in bucket],
            description=f"{label} — split from Liked Songs by liked_sort.py",
        )
        results.append((pl_name, url, len(bucket)))
        print(f"  {GRN}Created \"{pl_name}\" ({len(bucket)} tracks){RST}")
        time.sleep(0.1)
    return results


# ── Sort strategies ───────────────────────────────────────────────────────────

def sort_by_artist(tracks: list[dict]) -> list[dict]:
    return sorted(tracks, key=lambda t: (
        t["artists"][0]["name"].lower() if t.get("artists") else "",
        t["name"].lower(),
    ))


def sort_by_genre(tracks: list[dict],
                  genre_map: dict[str, str],
                  track_genre_map: dict[str, str]) -> list[dict]:
    """Group by macro-genre (alphabetical), then by artist+title within."""
    groups: defaultdict[str, list] = defaultdict(list)
    for t in tracks:
        groups[get_track_genre(t, genre_map, track_genre_map)].append(t)

    ordered = []
    for label in sorted(groups.keys()):
        bucket = sorted(groups[label], key=lambda t: (
            t["artists"][0]["name"].lower() if t.get("artists") else "",
            t["name"].lower(),
        ))
        ordered.extend(bucket)
    return ordered


def sort_by_mood(tracks: list[dict], track_moods: dict[str, str]) -> list[dict]:
    """
    Mood arc: Melancholic -> Peaceful -> Upbeat -> Intense.
    Tracks with no mood data go to the end.
    """
    groups:  defaultdict[str, list] = defaultdict(list)
    no_mood: list[dict] = []
    for t in tracks:
        m = track_moods.get(t["id"])
        if m:
            groups[m].append(t)
        else:
            no_mood.append(t)

    ordered = []
    for mood in MOOD_ORDER:
        ordered.extend(groups[mood])
    ordered.extend(no_mood)
    return ordered


def sort_by_genre_then_mood(tracks: list[dict],
                             genre_map: dict[str, str],
                             track_moods: dict[str, str],
                             track_genre_map: dict[str, str]) -> list[dict]:
    """Group by macro-genre; within each genre, apply mood arc."""
    genre_groups: defaultdict[str, list] = defaultdict(list)
    for t in tracks:
        genre_groups[get_track_genre(t, genre_map, track_genre_map)].append(t)

    ordered = []
    for genre_label in sorted(genre_groups.keys()):
        ordered.extend(sort_by_mood(genre_groups[genre_label], track_moods))
    return ordered


# ── Preview ───────────────────────────────────────────────────────────────────

def preview(tracks: list[dict],
            genre_map:       dict[str, str],
            track_moods:     dict[str, str],
            track_genre_map: dict[str, str],
            n: int = 20) -> None:
    print(f"\n{BOLD}Preview (first {min(n, len(tracks))} tracks):{RST}")
    print(f"  {'Title':<38} {'Artist':<22} {'Genre':<22} {'Mood'}{RST}")
    print(f"  {'-'*38} {'-'*22} {'-'*22} {'-'*12}")
    for t in tracks[:n]:
        aname = t["artists"][0]["name"] if t.get("artists") else "?"
        genre = get_track_genre(t, genre_map, track_genre_map)
        mood  = track_moods.get(t["id"], "—")
        title = t["name"][:37]
        print(f"  {title:<38} {DIM}{aname[:21]:<22} {genre[:21]:<22} {mood}{RST}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global sp
    enable_ansi()

    print(f"\n{BOLD}{CYN}Liked Songs Sorter{RST}")
    print(f"{DIM}Connecting...{RST}\n")
    sp = auth()

    me = sp.current_user()
    print(f"{GRN}Connected as {BOLD}{me['display_name']}{RST}\n")

    # ── Fetch Spotify data ────────────────────────────────────────────────────
    tracks   = fetch_liked_songs()
    features = fetch_audio_features(tracks)   # {} if restricted

    # ── Last.fm enrichment ────────────────────────────────────────────────────
    lastfm_key = os.getenv("LASTFM_API_KEY", "").strip()
    genre_map:   dict[str, str]        = {}   # artist_name -> genre
    artist_mood: dict[str, str | None] = {}   # artist_name -> mood

    if lastfm_key:
        genre_map, artist_mood = fetch_lastfm_data(tracks, lastfm_key)
    else:
        print(f"{DIM}No LASTFM_API_KEY — skipping Last.fm enrichment.{RST}")

    # ── Claude AI enrichment ──────────────────────────────────────────────────
    ai_key   = os.getenv("ANTHROPIC_API_KEY", "").strip()
    ai_cache: dict[str, dict] = {}             # track_id -> {genre, mood}
    track_genre_map: dict[str, str] = {}       # track_id -> genre  (from Claude)

    if ai_key:
        ai_cache = classify_with_claude(tracks, ai_key)
        if ai_cache:
            # Build per-track genre map from Claude's results
            track_genre_map = {
                tid: data["genre"]
                for tid, data in ai_cache.items()
                if data.get("genre") and data["genre"] != "Other"
            }
            # Also build artist-level genre_map via majority vote (used as fallback
            # for tracks Claude didn't classify)
            artist_votes: defaultdict[str, Counter] = defaultdict(Counter)
            for t in tracks:
                tid = t.get("id", "")
                if tid in ai_cache:
                    aname = t["artists"][0].get("name", "") if t.get("artists") else ""
                    if aname:
                        artist_votes[aname][ai_cache[tid]["genre"]] += 1
            for aname, votes in artist_votes.items():
                genre_map[aname] = votes.most_common(1)[0][0]
    elif not lastfm_key:
        print(f"{YLW}No ANTHROPIC_API_KEY or LASTFM_API_KEY found in .env.{RST}")
        print(f"{DIM}Genre/mood sorting will not be available.{RST}")
        print(f"{DIM}Get a free Last.fm key: https://www.last.fm/api/account/create{RST}")
        print(f"{DIM}Or add ANTHROPIC_API_KEY to .env for AI classification.{RST}\n")

    # ── Compute per-track mood (all sources combined) ─────────────────────────
    track_moods = compute_track_moods(tracks, features, artist_mood, ai_cache)
    has_genres  = bool(genre_map or track_genre_map)
    has_mood    = bool(track_moods)

    # ── Source summary ────────────────────────────────────────────────────────
    sources = []
    if features:        sources.append("Spotify audio features")
    if ai_cache:        sources.append("Claude AI")
    if lastfm_key and genre_map: sources.append("Last.fm")
    if sources:
        print(f"\n{DIM}Data sources: {', '.join(sources)}{RST}")

    # ── Build menu from what's actually available ─────────────────────────────
    # (label, sort_fn, uses_genre, uses_mood)
    all_modes = [
        ("By artist name (A-Z)",
         lambda: sort_by_artist(tracks),
         False, False),
        ("By genre",
         lambda: sort_by_genre(tracks, genre_map, track_genre_map),
         True, False),
        ("By mood  (Melancholic → Peaceful → Upbeat → Intense)",
         lambda: sort_by_mood(tracks, track_moods),
         False, True),
        ("By genre, then mood within each group",
         lambda: sort_by_genre_then_mood(tracks, genre_map, track_moods, track_genre_map),
         True, True),
    ]

    available   = [(l, fn, ng, nm) for l, fn, ng, nm in all_modes
                   if (not ng or has_genres) and (not nm or has_mood)]
    unavailable = [(l, ng, nm) for l, fn, ng, nm in all_modes
                   if (ng and not has_genres) or (nm and not has_mood)]

    print(f"\n{BOLD}Sort mode:{RST}")
    for i, (lbl, _, _, _) in enumerate(available, 1):
        print(f"  {i}. {lbl}")

    if unavailable:
        print(f"\n  {DIM}Unavailable (add ANTHROPIC_API_KEY or LASTFM_API_KEY to .env):")
        for lbl, ng, nm in unavailable:
            reasons = []
            if ng and not has_genres: reasons.append("no genre data")
            if nm and not has_mood:   reasons.append("no mood data")
            print(f"    - {lbl}  ({', '.join(reasons)})")
        print(RST, end="")

    while True:
        raw = input("\nChoice: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(available):
            choice = int(raw); break
        print(f"{RED}Invalid.{RST}")

    label, fn, uses_genre, uses_mood = available[choice - 1]

    # ── Output format ─────────────────────────────────────────────────────────
    can_split_genre = uses_genre and has_genres
    can_split_mood  = uses_mood  and has_mood

    print(f"\n{BOLD}Output format:{RST}")
    output_modes = [("One big playlist  (all songs sorted together)", "single")]
    if can_split_genre:
        output_modes.append(("Separate playlist per genre", "split_genre"))
    if can_split_mood and not can_split_genre:
        output_modes.append(("Separate playlist per mood", "split_mood"))

    for i, (desc, _) in enumerate(output_modes, 1):
        print(f"  {i}. {desc}")

    out_choice = 1
    if len(output_modes) > 1:
        while True:
            raw = input("\nChoice: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(output_modes):
                out_choice = int(raw); break
            print(f"{RED}Invalid.{RST}")

    _, out_mode = output_modes[out_choice - 1]

    # ── Sort the tracks ───────────────────────────────────────────────────────
    result = fn()

    # ── Preview + stats ───────────────────────────────────────────────────────
    preview(result, genre_map, track_moods, track_genre_map)

    if has_genres:
        genre_counts: defaultdict[str, int] = defaultdict(int)
        for t in result:
            genre_counts[get_track_genre(t, genre_map, track_genre_map)] += 1
        print(f"\n{BOLD}Genre breakdown:{RST}")
        for g, cnt in sorted(genre_counts.items(), key=lambda x: -x[1]):
            bar = "#" * round(cnt / len(result) * 30)
            print(f"  {g:<28} {cnt:>4}  {DIM}{bar}{RST}")

    if has_mood:
        mood_counts: defaultdict[str, int] = defaultdict(int)
        for t in result:
            mood_counts[track_moods.get(t["id"], "Unknown")] += 1
        print(f"\n{BOLD}Mood breakdown:{RST}")
        for m in MOOD_ORDER + ["Unknown"]:
            cnt = mood_counts.get(m, 0)
            if cnt:
                bar = "#" * round(cnt / len(result) * 30)
                print(f"  {m:<28} {cnt:>4}  {DIM}{bar}{RST}")

    # ── Name prefix ───────────────────────────────────────────────────────────
    default_prefix = "Liked Songs"
    raw_prefix = input(f"\nPlaylist name prefix [{default_prefix}]: ").strip()
    prefix = raw_prefix if raw_prefix else default_prefix

    # ── Confirm + create ──────────────────────────────────────────────────────
    if out_mode == "single":
        playlist_name = f"{prefix} - {label}"
        ok = input(f"Create \"{playlist_name}\" with {len(result)} tracks? [y/N] ").strip().lower()
        if ok != "y":
            print("Cancelled."); return
        print(f"{CYN}Creating playlist...{RST}")
        url = create_playlist(playlist_name, [t["id"] for t in result])
        print(f"\n{GRN}{BOLD}Done!{RST}")
        print(f"  {url}\n")

    elif out_mode == "split_genre":
        genre_groups: defaultdict[str, list] = defaultdict(list)
        for t in result:
            genre_groups[get_track_genre(t, genre_map, track_genre_map)].append(t)
        n_playlists = len(genre_groups)
        ok = input(
            f"Create {n_playlists} playlists "
            f"(e.g. \"{prefix} - Rock\", \"{prefix} - Pop\", ...)? [y/N] "
        ).strip().lower()
        if ok != "y":
            print("Cancelled."); return
        print(f"{CYN}Creating {n_playlists} playlists...{RST}")
        created = create_split_playlists(dict(genre_groups), prefix)
        print(f"\n{GRN}{BOLD}Done! Created {len(created)} playlists:{RST}")
        for pl_name, url, count in created:
            print(f"  {count:>4} tracks  {url}")

    elif out_mode == "split_mood":
        mood_groups: defaultdict[str, list] = defaultdict(list)
        for t in result:
            m = track_moods.get(t["id"], "Unknown")
            mood_groups[m].append(t)
        n_playlists = len(mood_groups)
        ok = input(
            f"Create {n_playlists} playlists "
            f"(e.g. \"{prefix} - Upbeat\", \"{prefix} - Melancholic\", ...)? [y/N] "
        ).strip().lower()
        if ok != "y":
            print("Cancelled."); return
        print(f"{CYN}Creating {n_playlists} playlists...{RST}")
        created = create_split_playlists(dict(mood_groups), prefix)
        print(f"\n{GRN}{BOLD}Done! Created {len(created)} playlists:{RST}")
        for pl_name, url, count in created:
            print(f"  {count:>4} tracks  {url}")


if __name__ == "__main__":
    main()
