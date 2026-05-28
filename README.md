# spotify-likedsongs-sorter

A collection of Python scripts for managing your Spotify library with Claude AI and Last.fm.

## Scripts

| Script | What it does |
|--------|-------------|
| `liked_sort.py` | Sorts your Liked Songs into playlists by genre, mood, or both |
| `vibe_sort.py` | Claude AI invents creative playlist themes and curates your songs into them |
| `txt_to_playlist.py` | Turns `.txt` song lists into Spotify playlists |
| `spotify_organizer.py` | Interactive tool to inspect, sort, deduplicate, and restructure playlists |
| `export_liked_songs.py` | Exports your Liked Songs to a plaintext file |
| `cleanup_playlists.py` | Merges duplicate playlists and removes duplicate tracks |
| `shuffle_playlists.py` | Randomly shuffles playlists |

---

## Setup

### 1. Install dependencies

```
python -m pip install spotipy python-dotenv requests anthropic
```

### 2. Create a Spotify app

1. Go to <https://developer.spotify.com/dashboard> and log in
2. Click **Create app**
3. Fill in any name/description
4. Under **Redirect URIs** add: `http://127.0.0.1:8888/callback`
5. Click **Save**, then copy your **Client ID** and **Client Secret**

### 3. Configure credentials

```
copy .env.example .env
```

Open `.env` and paste your values:

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback

ANTHROPIC_API_KEY=sk-ant-...   # for AI-powered scripts
LASTFM_API_KEY=abc123...       # free fallback for genre data
```

Get an Anthropic key at <https://console.anthropic.com/> and a Last.fm key at <https://www.last.fm/api/account/create>.

On first run any script will open a browser for Spotify login — the token is cached in `.spotify_cache` so you only do this once.

---

## liked_sort.py — Sort Liked Songs by genre/mood

Fetches all your Liked Songs, enriches them with genre and mood data via Claude AI and/or Last.fm, then creates sorted playlists.

```
python liked_sort.py
```

**Sort modes**

| # | Mode | What it does |
|---|------|-------------|
| 1 | By artist name | Alphabetical A–Z |
| 2 | By genre | Groups tracks into macro-genres (Hip-Hop, Pop, Rock, Electronic, …) |
| 3 | By mood arc | Melancholic → Peaceful → Upbeat → Intense |
| 4 | Genre then mood | Grouped by genre; within each group sorted by mood arc |

**Output formats:** one big sorted playlist, one playlist per genre, or one playlist per mood.

**Cost:** ~1200 tracks costs roughly $0.01–0.02 using Claude Haiku. Results are cached to `.ai_cache.json` so re-runs are free.

---

## vibe_sort.py — AI-designed playlist themes

Claude analyzes a sample of your Liked Songs, invents 12 creative playlist themes (e.g. "Late Night Drive", "Sunday Morning Ease", "Hype Mode"), then curates 15–50 songs per theme and creates the playlists on Spotify. You pick which themes to keep.

Re-running adds new liked songs to existing vibe playlists, or generates fresh themes (Claude avoids ones you've already used).

```
python vibe_sort.py
```

Requires `ANTHROPIC_API_KEY`.

---

## txt_to_playlist.py — Playlists from text files

Drop `.txt` files into the `playlists/` folder. Each file becomes one playlist; the filename (without `.txt`) is the playlist name.

**File format** (`playlists/road_trip.txt`):
```
# Comments and blank lines are ignored

Bohemian Rhapsody - Queen
Hotel California - Eagles
Stairway to Heaven - Led Zeppelin
```

```bash
python txt_to_playlist.py                          # all files in playlists/
python txt_to_playlist.py playlists/road_trip.txt  # specific file
python txt_to_playlist.py --dry-run                # preview without creating
python txt_to_playlist.py --public                 # make playlist(s) public
```

---

## spotify_organizer.py — Interactive playlist manager

Interactive menu for inspecting and restructuring your playlists.

```
python spotify_organizer.py
```

Features: list all playlists, sort by name/date/size, remove duplicates, merge playlists, split by decade, analyze, export to JSON.

---

## export_liked_songs.py — Export Liked Songs to text

Exports every liked track in `SONG - ARTIST` format — compatible with `txt_to_playlist.py`.

```
python export_liked_songs.py                    # saves to liked_songs.txt
python export_liked_songs.py my_songs.txt       # custom output path
```

---

## cleanup_playlists.py — Deduplicate playlists

Finds playlists matching your `.txt` files in `playlists/`, merges any duplicates (from repeated runs), and removes duplicate tracks. Always previews changes before applying.

```
python cleanup_playlists.py            # interactive with confirmation
python cleanup_playlists.py --dry-run  # preview only
```

---

## shuffle_playlists.py — Shuffle playlists

Randomly shuffles playlists matching your `.txt` files in `playlists/`.

```
python shuffle_playlists.py                            # shuffle all
python shuffle_playlists.py playlists/road_trip.txt    # specific playlist
python shuffle_playlists.py --dry-run                  # preview only
```

---

## Cache files

| File | Contents | Safe to delete? |
|------|---------|----------------|
| `.spotify_cache` | Spotify OAuth token | Yes — triggers re-login |
| `.ai_cache.json` | Claude genre/mood results per track | Yes — re-fetches (costs ~$0.01–0.02) |
| `.lastfm_cache.json` | Last.fm tag data per artist | Yes — re-fetches for free |
| `.vibe_cache.json` | Track → vibe assignments | Yes |
| `.vibe_design.json` | Active vibe themes | Yes — Claude re-designs on next run |
| `.vibe_history.json` | Previously used vibe names | Yes — Claude may reuse old names |
| `.vibe_playlists.json` | Vibe playlist IDs on Spotify | Yes — re-links on next run |
