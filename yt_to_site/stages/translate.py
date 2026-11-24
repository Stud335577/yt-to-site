"""Translate content to multiple languages via LLM.

Supports JSONL content translation and transcript translation.
Includes Arabic leakage detection and fallback model support.
"""

from __future__ import annotations

import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import PipelineConfig
from ..groq_client import chat, split_text
from groq import APIStatusError
from ..storage import JSONLStore, ProgressTracker



TRANSLATABLE_FIELDS = ["title", "full_title", "description", "content", "summary"]

LOCALE_NAMES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "tr": "Turkish",
    "ar": "Arabic",
    "ur": "Urdu",
    "id": "Indonesian",
    "ms": "Malay",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "pt": "Portuguese",
    "ru": "Russian",
    "hi": "Hindi",
}


def _get_locale_name(locale: str) -> str:
    return LOCALE_NAMES.get(locale, locale)


def _translate_text(text: str, target_locale: str, model: str, api_key: str) -> str:
    """Translate text to target language."""
    target_lang = _get_locale_name(target_locale)
    prompt = f"""Translate the following text to {target_lang}.

Rules:
- Translate naturally, not word-for-word
- Preserve markdown formatting (links, headings, bold, etc.)
- Preserve proper nouns and technical terms as-is when appropriate
- Do not invent hadith or Quran quotations. Do not produce translated Quran quotations; retain references only.
- Copy every QURANPLACEHOLDER token exactly, once, unchanged, in the same order.
- Return ONLY the translated text, no explanations

Text:
{text}"""

    return chat([{"role": "user", "content": prompt}], model, api_key, max_tokens=2048)


def _translate_with_retry(text: str, target_locale: str, model: str, fallback_model: str, api_key: str, max_retries: int = 3) -> str:
    """Use the configured model; shared Groq client handles rate-limit retries."""
    parts = []
    for chunk in split_text(text, 3000):
        try:
            parts.append(_translate_text(chunk, target_locale, model, api_key))
        except APIStatusError as exc:
            if exc.status_code not in (400, 404) or fallback_model == model:
                raise
            parts.append(_translate_text(chunk, target_locale, fallback_model, api_key))
    return "\n\n".join(parts)


def _translate_entry(entry: dict, target_locale: str, model: str, fallback_model: str, api_key: str) -> dict:
    """Translate all translatable fields in an entry."""
    translated = dict(entry)
    for field in TRANSLATABLE_FIELDS:
        value = entry.get(field)
        if value and isinstance(value, str) and len(value) > 0:
            translated[field] = _translate_with_retry(value, target_locale, model, fallback_model, api_key)
    return translated


def _translate_transcript_chunks(text: str, target_locale: str, model: str, fallback_model: str, api_key: str, max_chunk: int = 3000) -> str:
    """Translate bounded chunks while protecting Quran text."""
    # Protect verified Arabic from any change, including by the translation model.
    pattern = re.compile(r'<quran\s+ref="\d+:\d+(?:-\d+)?"\s*>.*?</quran>', re.DOTALL)
    verses = pattern.findall(text)
    for i, verse in enumerate(verses):
        text = text.replace(verse, f"QURANPLACEHOLDER{i}END", 1)
    if re.search(r"</?quran\b", text, re.IGNORECASE):
        raise ValueError("Malformed Quran markup")
    chunks = split_text(text, max_chunk)

    translated_parts = []
    for chunk in chunks:
        translated = _translate_with_retry(chunk, target_locale, model, fallback_model, api_key)
        translated_parts.append(translated)

    result = "\n\n".join(translated_parts)
    tokens = re.findall(r"QURANPLACEHOLDER\d+END", result)
    expected = [f"QURANPLACEHOLDER{i}END" for i in range(len(verses))]
    if tokens != expected:
        raise ValueError("Translation changed or omitted Quran placeholders")
    for token, verse in zip(expected, verses):
        result = result.replace(token, verse)
    return result


def run(config: PipelineConfig) -> dict:
    """Translate all content types to configured locales."""
    print("\n  Translate")
    print("  " + "─" * 38)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key and not config.dry_run:
        print("  Error: GROQ_API_KEY required")
        return {"error": "no API key"}

    store = JSONLStore(config.data_dir)
    total = 0
    errors = 0

    for locale in config.translation.locales:
        print(f"\n  Locale: {locale} ({_get_locale_name(locale)})")
        progress = ProgressTracker(config.data_dir, f"translate_{locale}")

        model = config.translation.model
        fallback = config.translation.fallback_model

        for content_type in config.content_types:
            source_entries = store.read(content_type)
            existing_translated = store.read(content_type, locale=locale)
            existing_ids = {e["id"] for e in existing_translated}

            pending = [e for e in source_entries if e["id"] not in existing_ids and not progress.is_done(e["id"])]
            print(f"    {content_type}: {len(pending)} pending")

            if config.dry_run:
                total += len(pending)
                continue

            for entry in pending:
                try:
                    translated = _translate_entry(entry, locale, model, fallback, api_key)
                    store.append(content_type, translated, locale=locale)
                    progress.mark_done(entry["id"])
                    total += 1
                except Exception as e:
                    errors += 1
                    print(f"      Error translating {entry['id']}: {e}")

                time.sleep(config.sleep)

        # Translate transcripts
        if "videos" in config.content_types:
            transcripts_dir = store.transcripts_dir()
            formatted_files = list(transcripts_dir.glob("*_formatted.md"))
            formatted_files = [f for f in formatted_files if f"_formatted_{locale}" not in f.name
                               and "_formatted_" not in f.name.replace("_formatted.md", "")]

            pending_transcripts = [
                f for f in formatted_files
                if not store.has_transcript(int(f.stem.replace("_formatted", "")), locale=locale)
            ]
            print(f"    transcripts: {len(pending_transcripts)} pending")

            if not config.dry_run:
                for f in pending_transcripts:
                    video_id = int(f.stem.replace("_formatted", ""))
                    text = f.read_text(encoding="utf-8")
                    try:
                        translated = _translate_transcript_chunks(text, locale, model, fallback, api_key)
                        store.write_transcript(video_id, translated, locale=locale)
                        total += 1
                    except Exception as e:
                        errors += 1
                        print(f"      Error translating transcript {video_id}: {e}")
                    time.sleep(config.sleep)

    print(f"\n  Translated: {total} items")
    return {"translated": total, **({"error": f"{errors} translations failed"} if errors else {})}
