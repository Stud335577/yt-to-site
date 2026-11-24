"""Retrieve source-language YouTube subtitles or transcribe audio with Groq Whisper.

Automatic captions and transcription are imperfect and must be reviewed.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.networking import Request

from ..groq_client import transcribe_file


class NoCaptionsError(RuntimeError):
    """No supported source-language caption track was available."""


def _parse_json3(payload: str) -> str:
    document = json.loads(payload)
    parts = []
    for event in document.get("events", []):
        content = "".join(segment.get("utf8", "") for segment in event.get("segs", []))
        if content.strip():
            parts.append(content.strip())
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _parse_vtt(payload: str) -> str:
    """Flatten WebVTT cues, removing overlapping rolling-caption words."""
    words: list[str] = []
    for block in re.split(r"\n\s*\n", payload.replace("\r\n", "\n")):
        lines = block.splitlines()
        timestamp = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timestamp is None:
            continue
        content = " ".join(lines[timestamp + 1:])
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
        current = content.split()
        overlap = 0
        for n in range(min(len(words), len(current)), 0, -1):
            if words[-n:] == current[:n]:
                overlap = n
                break
        words.extend(current[overlap:])
    return " ".join(words)


def _matching_tracks(tracks: dict, language: str, automatic: bool) -> list[dict]:
    language = language.lower().replace("_", "-")
    langs = [key for key in tracks if key.lower().replace("_", "-") == language
             or key.lower().replace("_", "-").startswith(language + "-")]
    # yt-dlp sometimes exposes the original auto-generated track as ar-orig.
    def rank(key: str) -> tuple[int, str]:
        key = key.lower().replace("_", "-")
        if automatic and key == language + "-orig":
            return (0, key)
        return (1 if key == language else 2, key)
    langs.sort(key=rank)
    candidates = []
    for lang in langs:
        formats = tracks.get(lang) or []
        for ext in ("json3", "vtt"):
            candidates.extend(fmt for fmt in formats if fmt.get("ext") == ext and fmt.get("url"))
    return candidates


def fetch_captions(video_id: str, language: str) -> str:
    """Prefer authored source captions, then auto captions; never silently use English."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    with YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 15, "retries": 1}) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Could not retrieve YouTube video metadata")
        tracks = (_matching_tracks(info.get("subtitles") or {}, language, False)
                  + _matching_tracks(info.get("automatic_captions") or {}, language, True))
        if not tracks:
            raise NoCaptionsError(f"No {language} captions available for video {video_id}")
        for track in tracks:
            try:
                # Use yt-dlp's HTTP stack so its request headers and cookie handling apply.
                with ydl.urlopen(Request(track["url"], headers=info.get("http_headers") or {})) as response:
                    payload = response.read().decode("utf-8-sig")
                text = _parse_json3(payload) if track["ext"] == "json3" else _parse_vtt(payload)
                if text:
                    return text
            except Exception:
                # Try another supported caption format/track, but never save empty text.
                continue
        raise RuntimeError(f"Could not download or parse {language} captions for video {video_id}; retry or supply a reviewed transcript")


def transcribe_groq(video_id: str, language: str, model: str, api_key: str) -> str:
    """Download audio temporarily, split it into short MP3s, then call Groq Whisper."""
    with tempfile.TemporaryDirectory(prefix="yt-to-site-audio-") as directory:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "bestaudio/best", "outtmpl": str(Path(directory) / "audio.%(ext)s"),
                "socket_timeout": 15, "retries": 1}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            if not info:
                raise RuntimeError(f"Could not download audio for video {video_id}")
            filename = (info.get("requested_downloads") or [{}])[0].get("filepath") or ydl.prepare_filename(info)
        # Short 24kbps segments stay comfortably below Groq's free-tier file size limit.
        try:
            subprocess.run([
                imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(filename), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k",
                "-f", "segment", "-segment_time", "600", "-reset_timestamps", "1",
                str(Path(directory) / "segment_%04d.mp3"),
            ], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Could not prepare audio for video {video_id} with ffmpeg") from exc
        segments = sorted(Path(directory).glob("segment_*.mp3"))
        if not segments:
            raise RuntimeError(f"No audio segments produced for video {video_id}")
        texts = []
        for segment in segments:
            if segment.stat().st_size > 24_000_000:
                raise ValueError("Audio segment exceeds the Groq free-tier upload limit")
            text = transcribe_file(segment, language, model, api_key)
            if text:
                texts.append(text)
        transcript = " ".join(texts).strip()
        if not transcript:
            raise RuntimeError(f"Groq audio transcription was empty for video {video_id}")
        return transcript


def get_transcript(video_id: str, language: str, audio_fallback: bool = True,
                   whisper_model: str = "whisper-large-v3", api_key: str | None = None) -> str:
    try:
        return fetch_captions(video_id, language)
    except NoCaptionsError as exc:
        if audio_fallback:
            if not api_key:
                raise ValueError("GROQ_API_KEY required for audio fallback") from exc
            return transcribe_groq(video_id, language, whisper_model, api_key)
        raise NoCaptionsError(f"{exc}. Add a reviewed raw transcript or enable Groq audio fallback; see README.") from exc
