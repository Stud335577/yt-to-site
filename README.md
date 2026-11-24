# Forty Hadith — Waqf project

A playlist-to-site pipeline configured for Sheikh Ibn Uthaymeen's explanation of Imam An-Nawawi's Forty Hadith. It imports the playlist `PLXS0usquloW_VC2gPb6nu1FFQTxWc1Ww_`, retrieves Arabic captions (or transcribes audio with Groq Whisper when captions are unavailable), creates structured lessons, and translates into English, Spanish, French, Indonesian and Urdu. The Next.js site serves the Arabic source and the five translations, with RTL layout for Arabic and Urdu.

## Important: verification and publication

**This is an editorial workflow, not a guarantee of religious accuracy. Do not publish the output without qualified human review.** Transcription and generative models can omit, misidentify, misattribute or invent words and citations. Review the original recording, the complete hadith text and attribution, the Sheikh's explanation, every verse/reference, and all translated content, titles, summaries and SEO metadata. The site shows a review notice but does **not** implement an approval gate or restrict access. Keep it private until reviewed and approved.

With `transcription.quran_validation: true`, the formatter is instructed to tag every complete Quran quotation with its surah and ayah. `quran-validator` checks tagged text against its bundled Arabic Quran; invalid references, text mismatches, malformed tags or a missing validator cause formatting to fail instead of falling back to unverified tagged text. Verified text is replaced with the database's canonical text, and protected from changes during article translation. A missing or changed translation placeholder causes translation to fail. The validator cannot identify every untagged or misattributed quotation or establish that a reference was appropriate in context. Existing files generated before validation was enabled are not revalidated automatically; use a fresh data directory or remove and regenerate the affected outputs and progress files.

No verified standard Quran translation corpus is bundled. The model is instructed **not to generate translated Quran quotations**: verified Arabic verses and their references remain in translated articles. The site's permanent link leads to the King Fahd Quran Printing Complex, where readers can find Quran editions and translations. This is a link to the complex, not a guaranteed direct download or a verification of the availability of each language. A qualified reviewer must check that the model followed these instructions and that translations faithfully reflect the lesson; if translated Quran text is needed in the articles, integrate an independently authenticated and licensed translation corpus first. Interlinking is automatically skipped when Quran validation is enabled because its LLM rewrites article text.

## Setup

Requires Python 3.10+ and Node.js 18+. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[quran]'
cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env and fill in GROQ_API_KEY.
```

The CLI loads `.env` from the current working directory. Environment variables already exported take precedence. Keep `.env` private and run these commands from the repository root. `config.yaml` is preconfigured for this playlist and locales; `data_dir: data` is relative to the current working directory. Use a fresh data directory, not one containing an earlier channel or playlist.

| Variable | Purpose | Required? |
| --- | --- | --- |
| `GROQ_API_KEY` | Formatting, translation, summaries, SEO and Groq Whisper | Yes, for the full Waqf pipeline |
| `DATA_DIR` | Absolute path read by the Next.js server | Optional; defaults to repository `data/` when started from `web/`. Does not change the pipeline's YAML `data_dir`. |

Neither a YouTube Data API key nor a ClipScript account is needed. `yt-dlp` obtains ordered playlist entries and Arabic subtitles directly from YouTube. It prefers human-authored Arabic subtitles when available and then auto-generated Arabic subtitles, in JSON3 or WebVTT format. Automatic captions can contain serious errors; review against the recording. YouTube may rate-limit or block automated access, change its interfaces, restrict videos, or not provide captions. Updating `yt-dlp` may help. Network access to YouTube is required. Groq free-tier limits and model availability vary by account and may change; verify your account and usage. Requests may incur charges depending on your plan.

When no supported Arabic caption track is present, the default configuration downloads the audio temporarily and uses Groq `whisper-large-v3`. The bundled `imageio-ffmpeg` binary converts and splits audio into ten-minute, 24 kbps mono MP3 chunks, which are uploaded individually; temporary files are removed afterward. This needs YouTube and Groq network access and enough temporary disk space. Caption-download errors do not trigger audio fallback; retry them or add a reviewed raw transcript at `data/transcripts/<id>.md`. Set `transcription.audio_fallback: false` to disable audio uploads. Captions and ASR can make serious errors and require scholarly review.

All LLM stages use Groq; the default model is `llama-3.3-70b-versatile` and can be changed in the YAML configuration. Formatting and translation inputs are split into bounded chunks, and truncated responses are rejected. The shared client spaces request starts by at least `sleep: 3.0` seconds across workers, retries 429, 5xx and network errors with exponential backoff, and honors `Retry-After` up to five minutes. If a reset exceeds five minutes, the run stops for later resumption. A three-second interval does not guarantee staying within token, audio-duration or daily free-tier quotas; model availability and limits are account-dependent. Change `sleep` if needed. The Waqf configuration automatically skips the unrelated legacy interlink stage to protect Quran text; enabling that generic stage with Quran validation disabled still requires its separate Cohere embedding service, while its LLM calls use Groq.

## Test the first two lessons, then continue

The limit selects the **first two playlist positions**, not the first two new videos. Use a fresh `data/` directory. The sync rejects pre-existing video entries outside the selected range, so it cannot accidentally process an existing full playlist during a two-item test. Never reuse a directory containing unrelated transcripts or progress files.

```bash
# From the repository root, after setup and filling in .env:
yts run config.yaml --limit 2
# Inspect data/videos.jsonl, data/transcripts/ and the translations.
yts status config.yaml

# After reviewing the test, remove the limit and resume the same data directory:
yts run config.yaml
yts status config.yaml
```

The full run processes the playlist in the order supplied by YouTube. Verify that the playlist currently contains the expected 42 lessons and that its order really matches lessons 1–42; playlist order, not inferred title numbers, is used. Missing positions reported by yt-dlp are rejected, but YouTube can hide entries, so compare against the actual playlist before publication. Unavailable or duplicate playlist items cause sync to fail for review. Existing IDs remain stable. The full run resumes existing outputs, so the initial two do not need to be regenerated. Failed stages stop the pipeline; correct the issue and rerun. JSONL and progress files are resumable but stale outputs do not automatically update when prompts, playlist metadata or translations change.

For sync **only**, use `yts init config.yaml --limit 2` (or `yts init config.yaml` for the full list). It does not require an API key and does not generate articles. To isolate the test entirely, change `data_dir` in a separate config file before running; the site reads `data/` by default. `yts run config.yaml --dry-run` fetches playlist metadata and previews pending stages without generating articles; it is not an offline end-to-end test.

## Website

```bash
cd web
npm install
npm run dev
```

Open the local URL printed by Next.js. `/` redirects to `/ar`; lesson pages are `/<locale>/lessons/<id>`. The site reads JSONL, generated SEO metadata and formatted Markdown from `data/` at request time, so a Node server is required. Article languages without a generated translation show an unavailable notice rather than silently displaying Arabic as a translation. Every lesson links to the Quran complex and the footer describes the Waqf.

## Development checks

```bash
pip install -e '.[quran,dev]'
pytest -q
cd web && npm install && npm run build
```

Tests mock external services; they do not certify the actual playlist, providers, scholarly accuracy or translation quality.

## License

MIT
