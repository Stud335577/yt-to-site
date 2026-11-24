"""Generate one-sentence summaries for content items.

Summaries serve as semantic anchors for interlinking and SEO descriptions.
"""

from __future__ import annotations

import os
import sys
import time

from ..config import PipelineConfig
from ..groq_client import chat
from ..storage import JSONLStore, ProgressTracker



SUMMARY_PROMPT = """Summarize the following text in exactly ONE complex sentence (max 2 lines).

Write in the same language as the source. Do not quote Quran or hadith text or invent claims.
Do NOT just describe the general topic. Explicitly name specific arguments, entities,
and terminology discussed to serve as keyword anchors for future hyperlinking.

Title: {title}

Content:
{content}

Return only the summary sentence, no other text or markdown."""


def _generate_summary(content: str, title: str, model: str, api_key: str) -> str:
    """Call LLM to generate a one-sentence summary."""
    prompt = SUMMARY_PROMPT.format(title=title, content=content[:3000])

    return chat([{"role": "user", "content": prompt}], model, api_key, max_tokens=500).strip("\"'")


def _summarize_with_retry(content: str, title: str, model: str, api_key: str, max_retries: int = 3) -> str:
    """Generate a summary with the shared Groq retry policy."""
    return _generate_summary(content, title, model, api_key)


def run(config: PipelineConfig) -> dict:
    """Generate summaries for all content items missing them."""
    print("\n  Summarize")
    print("  " + "─" * 38)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key and not config.dry_run:
        print("  Error: GROQ_API_KEY required")
        return {"error": "no API key"}

    store = JSONLStore(config.data_dir)
    progress = ProgressTracker(config.data_dir, "summarize")
    total = 0
    errors = 0

    for content_type in config.content_types:
        entries = store.read(content_type)
        pending = [e for e in entries if not e.get("summary") and not progress.is_done(e["id"])]

        print(f"  {content_type}: {len(pending)} pending")

        if config.dry_run:
            total += len(pending)
            continue

        entries_by_id = {e["id"]: e for e in entries}

        for entry in pending:
            content = entry.get("content") or entry.get("description") or ""
            title = entry.get("title") or entry.get("full_title") or ""

            # For videos, use transcript if available
            if entry.get("content_type") == "video":
                transcript = store.read_transcript(entry["id"])
                if transcript:
                    content = transcript

            if not content or len(content) < 50:
                progress.mark_done(entry["id"])
                continue

            try:
                summary = _summarize_with_retry(content, title, config.summarization.model, api_key)
                entries_by_id[entry["id"]]["summary"] = summary
                progress.mark_done(entry["id"])
                total += 1

                # Save incrementally
                store.write(content_type, list(entries_by_id.values()))

            except Exception as e:
                errors += 1
                print(f"    Error summarizing {entry['id']}: {e}")

            time.sleep(config.sleep)

    print(f"  Generated: {total} summaries")
    return {"generated": total, **({"error": f"{errors} summaries failed"} if errors else {})}
