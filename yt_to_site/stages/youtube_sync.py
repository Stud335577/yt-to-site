"""Fetch ordered playlist metadata using yt-dlp without an API key."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from ..config import PipelineConfig
from ..storage import JSONLStore


COUNTER_FILE = "youtube_id_counter.json"
ID_START = 100_000


def _load_counter(data_dir: Path) -> int:
    """Load the persistent ID counter."""
    counter_path = data_dir / COUNTER_FILE
    if counter_path.exists():
        try:
            with open(counter_path, "r") as f:
                return json.load(f).get("next_id", ID_START)
        except (json.JSONDecodeError, KeyError):
            pass
    return ID_START


def _save_counter(data_dir: Path, next_id: int) -> None:
    """Persist the ID counter."""
    counter_path = data_dir / COUNTER_FILE
    with open(counter_path, "w") as f:
        json.dump({"next_id": next_id}, f)


def fetch_playlist_videos(playlist_id: str, limit: int | None = None) -> list[dict]:
    """Fetch playlist entries in the order returned by YouTube, never upload order."""
    from yt_dlp import YoutubeDL

    options = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True,
               "ignoreerrors": False, "skip_download": True, "socket_timeout": 15, "retries": 1}
    if limit is not None:
        options["playlistend"] = limit
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") not in {"playlist", "multi_video"}:
            raise ValueError("Could not retrieve the playlist")
        videos = []
        for index, item in enumerate(info.get("entries") or [], start=1):
            if not item:
                raise ValueError(f"Playlist item {index} is unavailable")
            position = item.get("playlist_index")
            if position is not None and position != index:
                raise ValueError(f"Missing or out-of-order playlist item at position {index}")
            video_id = item.get("id")
            title = item.get("title") or ""
            if (not video_id or not title or title.lower().strip("[]") in {"private video", "deleted video"}
                    or item.get("availability") in {"private", "needs_auth", "subscriber_only", "premium_only"}):
                raise ValueError(f"Playlist item {index} is unavailable")
            upload_date = item.get("upload_date") or ""
            try:
                published = datetime.strptime(upload_date, "%Y%m%d").isoformat() if upload_date else ""
            except ValueError:
                published = ""
            thumbnails = item.get("thumbnails") or []
            thumbnail_url = item.get("thumbnail") or next(
                (thumb.get("url") for thumb in reversed(thumbnails) if thumb.get("url")), None
            )
            videos.append({"video_id": video_id, "title": title,
                           "description": item.get("description") or "",
                           "published_at": published, "thumbnail_url": thumbnail_url,
                           "position": index - 1})
            if limit and index >= limit:
                break
    if not videos:
        raise ValueError("Playlist is empty or inaccessible")
    if len({v["video_id"] for v in videos}) != len(videos):
        raise ValueError("Playlist contains duplicate videos; manual review required")
    return videos


def map_to_schema(yt_item: dict, item_id: int, source_language: str) -> dict:
    """Map YouTube API item to the content schema."""
    published = yt_item["published_at"].replace("Z", "")
    thumb_url = yt_item.get("thumbnail_url")

    return {
        "id": item_id,
        "title": yt_item["title"],
        "full_title": yt_item["title"],
        "description": yt_item["description"] or None,
        "content": None,
        "content_type": "video",
        "youtube_id": yt_item["video_id"],
        "youtube_url": f"https://www.youtube.com/watch?v={yt_item['video_id']}",
        "episode_number": yt_item["position"] + 1,
        "thumbnail_url": thumb_url,
        "is_series": False,
        "is_active": True,
        "parent_id": None,
        "authored_date": published,
        "summary": None,
        "source_language": source_language,
        "source": "youtube",
    }


def run(config: PipelineConfig) -> dict:
    """Sync playlist videos into the JSONL store."""

    print("\n  YouTube playlist sync")
    print("  " + "─" * 38)

    store = JSONLStore(config.data_dir)
    data_dir = Path(config.data_dir)

    # Get existing YouTube IDs from all video files
    existing_yt_ids: set[str] = set()
    for entry in store.stream("videos"):
        yt_id = entry.get("youtube_id")
        if yt_id:
            existing_yt_ids.add(yt_id.split("?")[0])
    for entry in store.stream("videos", series=True):
        yt_id = entry.get("youtube_id")
        if yt_id:
            existing_yt_ids.add(yt_id.split("?")[0])

    print(f"  Existing videos: {len(existing_yt_ids)}")

    # Fetch from YouTube
    try:
        channel_videos = fetch_playlist_videos(config.channel.playlist_id, config.limit)
    except Exception as e:
        print(f"  Playlist extraction error: {e}", file=sys.stderr)
        return {"error": str(e)}

    print(f"  Videos selected from playlist: {len(channel_videos)}")

    # Refuse mixed or stale datasets: downstream stages otherwise process extra lessons.
    selected_ids = {v["video_id"] for v in channel_videos}
    if existing_yt_ids - selected_ids:
        raise ValueError("Data directory contains videos outside the selected playlist/limit; use a fresh data_dir")
    # Keep episode numbers in sync even if the playlist is reordered.
    positions = {v["video_id"]: v["position"] + 1 for v in channel_videos}
    existing = store.read("videos")
    for entry in existing:
        entry["episode_number"] = positions[entry["youtube_id"].split("?")[0]]
    if existing and not config.dry_run:
        store.write("videos", sorted(existing, key=lambda e: e["episode_number"]))

    # Filter new only
    new_videos = [v for v in channel_videos if v["video_id"] not in existing_yt_ids]
    print(f"  New videos: {len(new_videos)}")

    if not new_videos:
        print("  Nothing to import.")
        return {"imported": 0}

    if config.dry_run:
        for v in new_videos[:10]:
            print(f"    - {v['title'][:60]} ({v['video_id']})")
        if len(new_videos) > 10:
            print(f"    ... and {len(new_videos) - 10} more")
        return {"would_import": len(new_videos)}

    # Assign IDs and append
    next_id = _load_counter(data_dir)
    count = 0
    for yt_item in new_videos:
        entry = map_to_schema(yt_item, next_id, config.channel.source_language)
        store.append("videos", entry)
        print(f"  + [{next_id}] {yt_item['title'][:60]}")
        next_id += 1
        count += 1

    store.write("videos", sorted(store.read("videos"), key=lambda e: e["episode_number"]))
    _save_counter(data_dir, next_id)
    print(f"\n  Imported {count} videos (IDs {next_id - count}–{next_id - 1})")
    return {"imported": count}
