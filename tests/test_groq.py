from types import SimpleNamespace

import httpx
import pytest
from groq import RateLimitError, BadRequestError

from yt_to_site import groq_client
from yt_to_site.stages import transcribe
from yt_to_site.config import PipelineConfig


def status_error(cls, code, headers=None):
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(code, headers=headers or {}, request=request)
    return cls("test error", response=response, body={})


def test_retry_429_honors_retry_after(monkeypatch):
    events = []
    monkeypatch.setattr(groq_client, "_pace", lambda: events.append("pace"))
    monkeypatch.setattr(groq_client.time, "sleep", lambda seconds: events.append(seconds))
    def operation():
        events.append("request")
        if events.count("request") == 1:
            raise status_error(RateLimitError, 429, {"retry-after": "7"})
        return "ok"
    assert groq_client.request(operation) == "ok"
    assert events == ["pace", "request", 7.0, "pace", "request"]


def test_daily_quota_delay_fails_without_long_sleep(monkeypatch):
    monkeypatch.setattr(groq_client, "_pace", lambda: None)
    monkeypatch.setattr(groq_client.time, "sleep", lambda _: pytest.fail("must not sleep for hours"))
    def operation(): raise status_error(RateLimitError, 429, {"retry-after": "3600"})
    with pytest.raises(RuntimeError, match="resume"):
        groq_client.request(operation)


def test_non_retryable_error_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(groq_client, "_pace", lambda: None)
    def operation():
        calls.append(True)
        raise status_error(BadRequestError, 400)
    with pytest.raises(BadRequestError): groq_client.request(operation)
    assert len(calls) == 1


def test_shared_pacing(monkeypatch):
    times = iter([10.0, 10.0, 10.5, 13.0])
    sleeps = []
    monkeypatch.setattr(groq_client.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(groq_client.time, "sleep", sleeps.append)
    monkeypatch.setattr(groq_client, "_last_request", None)
    monkeypatch.setattr(groq_client, "_min_interval", 3.0)
    groq_client._pace(); groq_client._pace()
    assert sleeps == [2.5]


def test_groq_completion_and_truncation(monkeypatch):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=" article "))])
    api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    monkeypatch.setattr(groq_client, "client", lambda _: api)
    monkeypatch.setattr(groq_client, "_pace", lambda: None)
    assert groq_client.chat([{"role": "user", "content": "prompt"}], "model", "key") == "article"
    response.choices[0].finish_reason = "length"
    with pytest.raises(ValueError, match="truncated"):
        groq_client.chat([], "model", "key")


def test_formatting_uses_bounded_groq_chunks(monkeypatch):
    calls = []
    monkeypatch.setattr(transcribe, "chat", lambda messages, model, key, max_tokens: calls.append(messages) or "explanation")
    config = PipelineConfig.from_dict({"channel": {"source_language": "ar"}, "transcription": {"quran_validation": False}})
    source = "كلمة " * 1800
    result = transcribe.format_transcript(source, "عنوان", "model", "key", config)
    assert len(calls) > 1
    assert all(len(call[1]["content"]) < 3300 for call in calls)
    assert result.count("explanation") == len(calls)
    assert "later part" in calls[1][1]["content"]


def test_audio_upload_reopens_file_on_retry(tmp_path, monkeypatch):
    path = tmp_path / "segment.mp3"
    path.write_bytes(b"audio bytes")
    reads = []
    def create(**kwargs):
        filename, stream, mime = kwargs["file"]
        assert filename == "segment.mp3" and mime == "audio/mpeg"
        assert kwargs["language"] == "ar" and kwargs["model"] == "whisper-large-v3"
        reads.append(stream.read())
        if len(reads) == 1:
            raise status_error(RateLimitError, 429)
        return SimpleNamespace(text=" نص ")
    api = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    monkeypatch.setattr(groq_client, "client", lambda _: api)
    monkeypatch.setattr(groq_client, "_pace", lambda: None)
    monkeypatch.setattr(groq_client.time, "sleep", lambda _: None)
    assert groq_client.transcribe_file(path, "ar", "whisper-large-v3", "key") == "نص"
    assert reads == [b"audio bytes", b"audio bytes"]
