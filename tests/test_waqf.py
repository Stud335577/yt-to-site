from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from quran_validator import QuranValidator

from yt_to_site.cli import main
from yt_to_site.config import PipelineConfig
from yt_to_site.pipeline import run_pipeline
from yt_to_site.stages import transcribe, translate, youtube_sync
from yt_to_site.storage import JSONLStore


def config(tmp_path):
    return PipelineConfig.from_dict({"channel": {"source_language": "ar"}, "data_dir": str(tmp_path), "sleep": 0})


def test_playlist_order_and_limit(monkeypatch):
    options = {}
    class Youtube:
        def __init__(self, opts): options.update(opts)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            assert "list=playlist" in url and not download
            return {"_type": "playlist", "entries": [
                {"id": "a", "title": "Lesson 1", "playlist_index": 1},
                {"id": "b", "title": "Lesson 2", "playlist_index": 2},
                {"id": "c", "title": "Lesson 3", "playlist_index": 3},
            ]}
    monkeypatch.setattr("yt_dlp.YoutubeDL", Youtube)
    videos = youtube_sync.fetch_playlist_videos("playlist", 2)
    assert options["playlistend"] == 2
    assert [v["video_id"] for v in videos] == ["a", "b"]
    assert [youtube_sync.map_to_schema(v, i, "ar")["episode_number"] for i, v in enumerate(videos)] == [1, 2]


def test_playlist_fails_on_holes_or_unavailable_items(monkeypatch):
    class Youtube:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download): return {"_type": "playlist", "entries": [
            {"id": "a", "title": "Lesson 1", "playlist_index": 1},
            {"id": "b", "title": "Lesson 3", "playlist_index": 3},
        ]}
    monkeypatch.setattr("yt_dlp.YoutubeDL", Youtube)
    with pytest.raises(ValueError, match="out-of-order"):
        youtube_sync.fetch_playlist_videos("playlist")


def test_sync_limit_rejects_preexisting_extra(tmp_path, monkeypatch):
    c = config(tmp_path); c.limit = 2
    JSONLStore(tmp_path).append("videos", {"id": 1, "youtube_id": "old"})
    monkeypatch.setattr(youtube_sync, "fetch_playlist_videos", lambda *args: [{"video_id": "a", "position": 0}])
    with pytest.raises(ValueError, match="fresh data_dir"):
        youtube_sync.run(c)


def test_quran_canonical_and_failure_cases():
    verse = QuranValidator().get_verse(1, 1).text
    assert verse in transcribe._fix_tagged_verses(f'<quran ref="1:1">{verse}</quran>')
    with pytest.raises(ValueError, match="does not match"):
        transcribe._fix_tagged_verses('<quran ref="1:1">خطأ</quran>')
    with pytest.raises(ValueError):
        transcribe._fix_tagged_verses('<quran ref="1:999">خطأ</quran>')
    with pytest.raises(ValueError, match="Malformed"):
        transcribe._fix_tagged_verses('<quran ref="oops">خطأ</quran>')


def test_missing_validator_fails_closed(monkeypatch):
    monkeypatch.setattr(transcribe, "_init_quran_validation", lambda: False)
    with pytest.raises(RuntimeError): transcribe._fix_tagged_verses("anything")


def test_translation_preserves_and_checks_verse(monkeypatch):
    verse = '<quran ref="1:1">بِسْمِ ٱللَّهِ</quran>'
    monkeypatch.setattr(translate, "_translate_with_retry", lambda text, *args: text.replace("heading", "translated"))
    result = translate._translate_transcript_chunks(f'## heading\n{verse}', "en", "m", "f", "k")
    assert verse in result
    monkeypatch.setattr(translate, "_translate_with_retry", lambda *args: "lost")
    with pytest.raises(ValueError, match="placeholder"):
        translate._translate_transcript_chunks(verse, "en", "m", "f", "k")


def test_entry_translation_errors_do_not_silently_pass(monkeypatch):
    def fail(*args): raise ValueError("broken")
    monkeypatch.setattr(translate, "_translate_with_retry", fail)
    with pytest.raises(ValueError): translate._translate_entry({"title": "title"}, "en", "m", "f", "k")


def test_pipeline_stops_after_failed_sync(tmp_path, monkeypatch):
    c = config(tmp_path)
    from yt_to_site import pipeline
    called = []
    monkeypatch.setattr(pipeline, "STAGES", [("youtube_sync", SimpleNamespace(run=lambda _: {"error": "bad"})), ("transcribe", SimpleNamespace(run=lambda _: called.append(True)))])
    results = run_pipeline(c)
    assert results["youtube_sync"]["error"] == "bad"
    assert not called


def test_cli_limit_cannot_skip_sync(tmp_path):
    path = tmp_path / "config.yaml"; path.write_text("channel: {}\n")
    result = CliRunner().invoke(main, ["run", str(path), "--limit", "2", "--skip", "youtube_sync"])
    assert result.exit_code != 0
    assert "requires youtube_sync" in result.output


def test_waqf_example_config():
    c = PipelineConfig.from_yaml("config.example.yaml")
    assert c.channel.playlist_id == "PLXS0usquloW_VC2gPb6nu1FFQTxWc1Ww_"
    assert c.channel.source_language == "ar"
    assert c.translation.locales == ["en", "es", "fr", "id", "ur"]
    assert c.transcription.quran_validation



def test_subtitle_preference_and_text_extraction(monkeypatch):
    import io
    from yt_to_site.stages import subtitles
    requested = []
    class Youtube:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            return {"subtitles": {"ar": [{"ext": "json3", "url": "https://caption/manual"}]},
                    "automatic_captions": {"ar": [{"ext": "json3", "url": "https://caption/auto"}]}}
        def urlopen(self, request):
            requested.append(request.url)
            return io.BytesIO('{"events":[{"segs":[{"utf8":"السلام "},{"utf8":"عليكم"}]},{"segs":[{"utf8":" ورحمة الله"}]}]}'.encode())
    monkeypatch.setattr(subtitles, "YoutubeDL", Youtube)
    assert subtitles.fetch_captions("video", "ar") == "السلام عليكم ورحمة الله"
    assert requested == ["https://caption/manual"]


def test_subtitles_auto_vtt_and_rolling_cues(monkeypatch):
    import io
    from yt_to_site.stages import subtitles
    class Youtube:
        def __init__(self, options): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            return {"subtitles": {}, "automatic_captions": {"ar-orig": [{"ext": "vtt", "url": "https://caption/auto"}]}}
        def urlopen(self, request):
            return io.BytesIO(('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nالسلام عليكم\n\n'
                               '00:00:01.000 --> 00:00:02.000\nعليكم ورحمة الله\n').encode())
    monkeypatch.setattr(subtitles, "YoutubeDL", Youtube)
    assert subtitles.fetch_captions("video", "ar") == "السلام عليكم ورحمة الله"


def test_missing_captions_optional_fallback(monkeypatch):
    from yt_to_site.stages import subtitles
    def no_caption(*args): raise subtitles.NoCaptionsError("missing")
    monkeypatch.setattr(subtitles, "fetch_captions", no_caption)
    monkeypatch.setattr(subtitles, "transcribe_groq", lambda *args: "Groq transcript")
    with pytest.raises(subtitles.NoCaptionsError, match="Groq audio fallback"):
        subtitles.get_transcript("video", "ar", False)
    assert subtitles.get_transcript("video", "ar", True, "whisper-large-v3", "test") == "Groq transcript"


def test_caption_download_failure_does_not_trigger_fallback(monkeypatch):
    from yt_to_site.stages import subtitles
    def fail(*args): raise RuntimeError("network problem")
    monkeypatch.setattr(subtitles, "fetch_captions", fail)
    monkeypatch.setattr(subtitles, "transcribe_groq", lambda *args: pytest.fail("unexpected fallback"))
    with pytest.raises(RuntimeError, match="network problem"):
        subtitles.get_transcript("video", "ar", True, api_key="test")


def test_transcribe_without_legacy_keys(tmp_path, monkeypatch):
    from yt_to_site.storage import ProgressTracker
    c = config(tmp_path)
    store = JSONLStore(tmp_path)
    monkeypatch.delenv("CLIPSCRIPT_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setattr(transcribe, "get_transcript", lambda *args: "Arabic source text")
    monkeypatch.setattr(transcribe, "format_transcript", lambda *args: "formatted")
    result = transcribe._process_video({"id": 1, "youtube_id": "video", "title": "lesson"}, c, store, ProgressTracker(tmp_path, "transcribe"))
    assert not result.get("error")
    assert store.read_transcript(1, formatted=False) == "Arabic source text"
    assert store.read_transcript(1) == "formatted"


def test_audio_fallback_download_and_cleanup(monkeypatch):
    import wave
    from pathlib import Path
    from yt_to_site.stages import subtitles
    state = {}
    class Youtube:
        def __init__(self, options): state["output"] = options["outtmpl"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            filename = state["output"].replace("%(ext)s", "wav")
            with wave.open(filename, "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b"\x00\x00" * 16000)
            state["filename"] = filename
            return {"requested_downloads": [{"filepath": filename}]}
    def transcribe_file(path, language, model, key):
        assert path.exists() and path.stat().st_size > 0
        assert (language, model, key) == ("ar", "whisper-large-v3", "test")
        state["segment"] = path
        return "نص عربي"
    monkeypatch.setattr(subtitles, "YoutubeDL", Youtube)
    monkeypatch.setattr(subtitles, "transcribe_file", transcribe_file)
    assert subtitles.transcribe_groq("video", "ar", "whisper-large-v3", "test") == "نص عربي"
    assert not Path(state["filename"]).exists()
    assert not state["segment"].exists()


def test_failed_caption_fetch_does_not_write_transcript(tmp_path, monkeypatch):
    from yt_to_site.storage import ProgressTracker
    c = config(tmp_path)
    store = JSONLStore(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "test")
    def fail(*args): raise RuntimeError("no subtitles")
    monkeypatch.setattr(transcribe, "get_transcript", fail)
    result = transcribe._process_video({"id": 1, "youtube_id": "video"}, c, store, ProgressTracker(tmp_path, "transcribe"))
    assert result["error"] == "no subtitles"
    assert not store.has_transcript(1, formatted=False)


def test_sync_imports_without_youtube_api_key(tmp_path, monkeypatch):
    c = config(tmp_path)
    c.limit = 2
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    videos = [
        {"video_id": f"id{i}", "title": f"Lesson {i+1}", "description": "", "published_at": "", "position": i}
        for i in range(2)
    ]
    monkeypatch.setattr(youtube_sync, "fetch_playlist_videos", lambda playlist_id, limit: videos)
    assert youtube_sync.run(c)["imported"] == 2
    entries = JSONLStore(tmp_path).read("videos")
    assert [e["episode_number"] for e in entries] == [1, 2]
    assert entries[0]["youtube_url"] == "https://www.youtube.com/watch?v=id0"
