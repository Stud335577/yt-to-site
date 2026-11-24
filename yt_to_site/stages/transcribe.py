"""Transcribe YouTube videos and format into structured markdown.

Two-phase process:
1. Retrieve source-language YouTube subtitles (or Groq Whisper audio transcription)
2. Formatting into structured markdown with headings via LLM

Optional: Quran verse validation (requires `quran-validator` package).
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import PipelineConfig
from ..groq_client import chat, split_text
from ..storage import JSONLStore, ProgressTracker
from .subtitles import get_transcript



FORMAT_SYSTEM_PROMPT = """You are a careful Arabic editor preparing a lesson from Sheikh Ibn Uthaymeen's explanation of Imam An-Nawawi's Forty Hadith.
Preserve the Sheikh's scholarly tone, reverence, distinctions and meaning. Do not invent words, rulings, citations, or attribution. If uncertain say so.
Write in Arabic. At the beginning, under the heading ## نص الحديث, quote the main hadith as it appears in the transcript; if it cannot be identified, state that it needs review and do not invent it. Then use structured headings for the explanation, benefits, and any qualifications grounded in the transcript.
All Quran quotations must be complete verses tagged as <quran ref="SURAH:AYAH">full Arabic verse</quran>, or <quran ref="SURAH:START-END">full Arabic verses</quran>. Give only citations, not a quotation, when unsure. Do not write untagged Quranic text or translated Quran quotations. Never put Quran text in a hadith quotation without tags. A reviewer must verify the hadith, citations and meaning.
If the response is too long, end with [continue]; when continuing do not repeat content.
"""

# Quran validation support (optional)
_quran_validator = None
_quran_system_prompt = ""


def _init_quran_validation():
    """Try to load quran-validator for optional Quran verse correction."""
    global _quran_validator, _quran_system_prompt
    if _quran_validator is not None:
        return True
    try:
        from quran_validator import QuranValidator, normalize_arabic, SYSTEM_PROMPTS
        _quran_validator = QuranValidator()
        _quran_system_prompt = SYSTEM_PROMPTS.get("xml", "")
        return True
    except ImportError:
        return False


def _fix_tagged_verses(text: str) -> str:
    """Reject invalid citations or mismatches; replace verified full verses with canonical text.

    Tags remain in storage to protect Quran text during translation. This checks
    tagged quotations only; independent scholarly review is still essential.
    """
    if not _init_quran_validation():
        raise RuntimeError("quran-validator is required when Quran validation is enabled")
    from quran_validator import normalize_arabic

    pattern = re.compile(r'<quran\s+ref="(\d+):(\d+)(?:-(\d+))?"\s*>(.*?)</quran>', re.DOTALL)

    def replace(match):
        surah, start = int(match.group(1)), int(match.group(2))
        end = int(match.group(3) or start)
        if not 1 <= surah <= 114 or not 1 <= start <= end or end - start > 20:
            raise ValueError("Invalid Quran reference")
        verses = []
        for ayah in range(start, end + 1):
            verse = _quran_validator.get_verse(surah, ayah)
            if not verse:
                raise ValueError(f"Invalid Quran reference {surah}:{ayah}")
            verses.append(verse.text)
        canonical = " ۝ ".join(verses)
        def compare(value):
            return re.sub(r"[^\u0621-\u064a\u0671-\u06d3]", "", normalize_arabic(value))
        if not compare(match.group(4)) or compare(match.group(4)) != compare(canonical):
            raise ValueError(f"Quran quotation does not match {surah}:{start}-{end}; review required")
        return f'<quran ref="{surah}:{start}' + (f'-{end}' if end != start else '') + f'">{canonical}</quran>'

    result = pattern.sub(replace, text)
    # Detect malformed/unclosed markup rather than silently stripping it.
    remainder = pattern.sub("", result)
    if re.search(r"</?quran\b", remainder, re.IGNORECASE):
        raise ValueError("Malformed Quran tag; review required")
    return result



def _clean_formatted_content(text: str) -> str:
    """Clean LLM formatting artifacts from transcript."""
    content = text.strip()

    # Remove markdown code blocks
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines)

    # Remove planning tags
    if "</formatting_plan>" in content:
        parts = content.split("</formatting_plan>")
        content = parts[-1].strip()

    # Remove continue markers
    content = content.replace("[continue]", "").replace("[Continue]", "").strip()

    return content


def _get_format_system_prompt(config: PipelineConfig) -> str:
    """Build the formatting system prompt, optionally including Quran tagging instructions."""
    prompt = FORMAT_SYSTEM_PROMPT
    if config.transcription.quran_validation and not _init_quran_validation():
        raise RuntimeError("quran-validator is required when Quran validation is enabled")
    return prompt


def format_transcript(raw_transcript: str, video_title: str, model: str, api_key: str, config: PipelineConfig) -> str:
    """Format raw transcript to structured markdown via LLM with continuation support."""
    system_prompt = _get_format_system_prompt(config)
    parts = []
    chunks = split_text(raw_transcript, 3000)
    if not chunks:
        raise ValueError("Empty transcript")
    for index, chunk in enumerate(chunks):
        instruction = ("This is the first part: put the main hadith at the top if identified."
                       if index == 0 else
                       "This is a later part of the same lesson: continue the explanation without repeating the main hadith or inventing transitions.")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{instruction}\nVideo title: {video_title}\n\nTranscript part {index + 1} of {len(chunks)}:\n{chunk}"},
        ]
        for _ in range(10):
            content = chat(messages, model, api_key, max_tokens=2048)
            has_continue = "[continue]" in content.lower()
            cleaned = _clean_formatted_content(content)
            if cleaned:
                parts.append(cleaned)
            if not has_continue:
                break
            messages.extend([{"role": "assistant", "content": content},
                             {"role": "user", "content": "Continue exactly where you stopped."}])
        else:
            raise ValueError("Formatting continuation limit reached; review and retry")
    result = "\n\n".join(parts)

    # Validate and fix Quran verses if enabled
    if config.transcription.quran_validation:
        result = _fix_tagged_verses(result)

    return result


def _process_video(video: dict, config: PipelineConfig, store: JSONLStore, progress: ProgressTracker) -> dict:
    """Retrieve subtitles or Groq Whisper transcription and format via LLM."""
    video_id = video["id"]
    youtube_id = video.get("youtube_id", "")
    title = video.get("title") or video.get("full_title") or f"Video {video_id}"

    if not youtube_id:
        return {"skipped": True}

    clean_yt_id = youtube_id.split("?")[0]
    groq_key = os.getenv("GROQ_API_KEY")

    if not groq_key:
        return {"error": "GROQ_API_KEY not set"}

    result = {"video_id": video_id, "youtube_id": clean_yt_id}

    # Step 1: Source-language captions or Groq Whisper audio transcription
    if not store.has_transcript(video_id, formatted=False):
        try:
            raw = get_transcript(clean_yt_id, config.channel.source_language,
                                 config.transcription.audio_fallback, config.transcription.whisper_model, groq_key)
            store.write_transcript(video_id, raw, formatted=False)
            result["transcribed"] = True
            time.sleep(config.sleep)
        except Exception as e:
            result["error"] = str(e)
            return result

    # Step 2: Format via LLM
    if not store.has_transcript(video_id, formatted=True):
        try:
            raw = store.read_transcript(video_id, formatted=False)
            formatted = format_transcript(raw, title, config.transcription.format_model, groq_key, config)
            store.write_transcript(video_id, formatted, formatted=True)
            result["formatted"] = True
            time.sleep(config.sleep)
        except Exception as e:
            result["error"] = str(e)
            return result

    progress.mark_done(video_id)
    return result


def run(config: PipelineConfig) -> dict:
    """Run transcription stage on all videos missing transcripts."""
    print("\n  Transcribe")
    print("  " + "─" * 38)

    store = JSONLStore(config.data_dir)
    progress = ProgressTracker(config.data_dir, "transcribe")

    # Check for Quran validation
    if config.transcription.quran_validation:
        if _init_quran_validation():
            print("  Quran validation: enabled")
        else:
            raise RuntimeError("quran-validator is required; install the quran extra")

    # Collect videos needing transcription
    videos = [v for v in store.stream("videos") if v.get("youtube_id") and not progress.is_done(v["id"])]
    print(f"  Pending: {len(videos)} videos")

    if config.dry_run:
        for v in videos[:10]:
            yt_id = v.get("youtube_id", "")[:11]
            title = (v.get("title") or "")[:40]
            print(f"    {yt_id} | {title}")
        return {"pending": len(videos)}

    if not videos:
        return {"processed": 0}

    workers = config.transcription.parallel_workers
    processed = 0
    errors = 0

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_process_video, v, config, store, progress): v for v in videos}
            for future in as_completed(futures):
                result = future.result()
                if not result.get("error"):
                    processed += 1
                else:
                    errors += 1
                    print(f"    Error: {result.get('youtube_id')}: {result['error']}")
    else:
        for video in videos:
            result = _process_video(video, config, store, progress)
            if not result.get("error"):
                processed += 1
            else:
                errors += 1
                print(f"    Error: {result.get('youtube_id')}: {result['error']}")

    print(f"  Processed: {processed}/{len(videos)}")
    return {"processed": processed, **({"error": f"{errors} videos failed"} if errors else {})}
