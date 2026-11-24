"""Pipeline orchestrator — runs stages in sequence with skip controls."""

from __future__ import annotations

from .config import PipelineConfig
from .groq_client import configure_rate_limit
from .stages import youtube_sync, transcribe, summarize, translate, interlink, seo


STAGES = [
    ("youtube_sync", youtube_sync),
    ("transcribe", transcribe),
    ("summarize", summarize),
    ("translate", translate),
    ("interlink", interlink),
    ("seo", seo),
]


def run_pipeline(config: PipelineConfig, skip: set[str] | None = None) -> dict:
    """Run the full pipeline with optional stage skipping.

    Args:
        config: Pipeline configuration
        skip: Set of stage names to skip (e.g. {"translate", "interlink"})

    Returns:
        Dict of stage results
    """
    configure_rate_limit(config.sleep)
    skip = skip or set()
    results = {}

    print("\n  PIPELINE")
    print("  " + "=" * 38)

    if config.dry_run:
        print("  Mode: DRY RUN")

    for name, module in STAGES:
        if name == "interlink" and config.transcription.quran_validation and name not in skip:
            print("\n  Skipped: interlink (protect verified Quran text)")
            results[name] = {"skipped": True}
            continue
        if name in skip:
            print(f"\n  Skipped: {name}")
            results[name] = {"skipped": True}
            continue

        try:
            results[name] = module.run(config)
            if results[name].get("error") or (name == "youtube_sync" and results[name].get("skipped")):
                results[name]["error"] = results[name].get("error", "YouTube sync skipped")
                break
        except KeyboardInterrupt:
            print(f"\n\n  Interrupted during {name}. Progress saved.")
            results[name] = {"interrupted": True}
            break
        except Exception as e:
            print(f"\n  Error in {name}: {e}")
            results[name] = {"error": str(e)}
            break

    print("\n  " + "=" * 38)
    print("  Pipeline stopped with errors" if any(r.get("error") for r in results.values()) else "  Pipeline complete")

    return results
