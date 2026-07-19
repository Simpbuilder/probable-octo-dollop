# Viral Clip Pipeline

This repository is a standalone Python pipeline for collecting funny video
clips and preparing them for a future Instagram Reels workflow. It remains
separate from the `ai-video-poster` project.

## Installation

Create and activate a Python virtual environment, then install the project
dependencies:

```bash
pip install -r requirements.txt
```

This installs PRAW, `python-dotenv`, `yt-dlp`, Pillow for local hook-text overlays,
and the OpenAI SDK for optional hook-candidate generation.

### FFmpeg

yt-dlp can download a video and audio stream separately. FFmpeg is required to
merge those streams into a normal playable file. Install the FFmpeg binary and
make sure both `ffmpeg` and `ffprobe` are available on your `PATH`; verify them
with:

```bash
ffmpeg -version
ffprobe -version
```

The downloader checks for FFmpeg only when yt-dlp selects separate streams. A
single-file media format can still download without it. The vertical formatter
requires both tools to inspect and render local video files. See the
[yt-dlp FFmpeg guidance](https://github.com/yt-dlp/yt-dlp#strongly-recommended)
for installation details.

## Collection Modes

`config/collector.json` selects a metadata collection mode through
`pipeline_mode`:

- `manual_urls`: import URLs from `input_urls.txt` without Reddit API access.
- `reddit_api`: run the PRAW Reddit metadata collector.
- `both`: run manual URL intake first, then the Reddit API collector.

The checked-in configuration defaults to `manual_urls`, so development can
continue while Reddit Data API approval is pending. The PRAW collector remains
available and unchanged.

## Manual URL Intake

Add one public HTTP or HTTPS URL per line in `input_urls.txt`. Blank lines and
lines beginning with `#` are preserved and ignored:

```text
# A note for the next intake run
https://www.reddit.com/r/funny/comments/example_post/

https://example.com/another-public-url
```

The intake collector normalizes a URL for stable duplicate detection while
preserving the original pasted value in metadata. It recognizes Reddit domains
and records a subreddit when it appears in the URL. Valid non-Reddit URLs are
also accepted for future source support.

After an accepted URL, or one already represented in metadata, the line is
removed from `input_urls.txt` and appended to `metadata/processed_urls.txt`.
Invalid URLs and URLs that fail to save remain in the input queue for correction
or retry. The processed log is local runtime data and is ignored by Git.

## Pending Clip Downloader

The downloader processes metadata records whose `download_status` is `pending`.
It uses the stored `source_url`, saves a safe filename in `clips/pending/`, and
updates the metadata with the final local path, duration, width, height, and a
`downloaded` status. It never changes `processing_status` from `pending`.

The safe output file name is based on the clip's unique ID and retains the
extension selected by yt-dlp where possible. MP4 is preferred, but other
playable containers may be retained when conversion would require unavailable
tools. Existing output files are not overwritten unless `overwrite` is
explicitly enabled in the downloader settings.

Failures, including deleted or private posts, age restrictions, unavailable
formats, network errors, and missing FFmpeg for a selected merge, leave the
clip `pending` and save a concise `download_error`. They can be retried on the
next explicit downloader run.

### Downloader Configuration

The `downloader` block in `config/collector.json` controls:

- `directory`: must be `clips/pending` for this stage.
- `preferred_format`: `mp4` by default.
- `maximum_duration_seconds`: skip known longer media.
- `maximum_file_size_bytes`: yt-dlp file-size safety limit.
- `retries` and `timeout_seconds`: network behavior.
- `overwrite`: explicit opt-in to replace a matching target file.
- `downloads_per_run`: maximum pending records attempted in one run.
- `enabled`: automatic download control; it is `false` by default.

## Vertical Reel Formatter

The formatter selects clips with `download_status: downloaded`,
`processing_status: pending`, and an existing `local_file_path`. It keeps the
original download in `clips/pending/` and creates a separate ready MP4 in
`clips/ready/plain/` when no hook is rendered or `clips/ready/hooked/` when it
is. Metadata records the final ready-file path and its 1080x1920 output
dimensions while preserving source dimensions and the original local path.
Existing files already in `clips/ready/` are retained in place.

### Fit Layout

The default `fit` crop mode never crops or stretches source content. Landscape,
4:3, square, portrait, and already-vertical clips are scaled proportionally to
fit inside a white 1080x1920 canvas. The source is centered in the usable area,
with a configurable area above it for hook text.

Formatted media uses H.264 video, AAC audio when source audio exists,
`yuv420p`, a configured constant output frame rate, and MP4 fast-start metadata.
Audio-free videos remain valid outputs. The formatter retries safely: a bad or
missing source stays `pending` and receives a `format_error` in metadata.

### Hook Text

The `hook` block in `config/formatter.json` controls an optional clean text
overlay: black, centered, bold sans-serif text on the existing white canvas.
It supports a maximum width, one to three lines by default, word-aware wrapping,
automatic font shrinking, safe truncation, line spacing, text-box placement,
and optional outline or shadow settings. No subtitle track, logo, watermark, or
other decorative media is added. Generated hook candidates remain optional and
require review unless automatic selection is explicitly enabled.

Hook text is chosen in this order:

1. The explicit `--hook` value for a one-clip validation run.
2. A reviewed or automatically selected `selected_hook` candidate.
3. `hook_text` already stored on the clip metadata record.
4. The original source title when `fallback_to_source_title` is enabled.

If no hook is available, or `hook.enabled` is `false`, formatting continues
normally without text and records `hook_status: skipped`. A completed overlay
records `hook_text`, `hook_source`, and `hook_status: rendered`; failures remain
retryable with `hook_status: failed` and `hook_error`.

To set a persistent manual hook, add `hook_text` to the clip's local metadata
record. For a quick one-clip override, use the command in the Run section.

### Hook Generation And Review

`config/hooks.json` controls optional OpenAI hook generation separately from
rendering. It is disabled by default and contains the model, maximum hook
characters, generation queue limit, `automatic_selection` setting, and a
`blocked_phrases` list. The generator uses only the original post title and
available saved metadata; it does not inspect video content, download media, or
render a video.

Generate exactly three short, distinct candidate hooks for eligible clips with:

```bash
py run_pipeline.py --generate-hooks
```

Use `--force-hooks` with that command to replace existing candidates. Clips with
saved candidates are otherwise skipped. Generation asks for three distinct,
casual English captions that prefer two to seven words and never exceed nine.
The configured blocked phrases prevent generic clickbait; a blocked response is
retried once with the same source metadata. API failures are recorded on the
clip as retryable metadata errors and do not stop later clips.

Review candidates locally before formatting with:

```bash
py review_hooks.py
```

For each clip, the reviewer displays its ID, original title, and three options.
Enter `1`, `2`, or `3` to select a candidate; `c` to enter a custom hook; `s` to
leave it unchanged; `r` to reject all candidates; or `all 1`, `all 2`, or
`all 3` to choose one option for the remaining clips. The saved `selected_hook`
is used by a later formatter run, but generation and review never start that run.

### Fonts

By default, `hook.font_path` is `null`, so the renderer uses a safe bold system
font where one is available. To choose a font yourself, place a permitted `.ttf`
or `.otf` file in `assets/fonts/` and set a relative path such as
`assets/fonts/MyFont-Bold.ttf` in `config/formatter.json`. Font files are not
provided or committed by this project. When the configured file is missing, the
renderer tries platform system fonts and then Pillow's built-in fallback font,
logging the fallback it used.

### Formatter Configuration

`config/formatter.json` controls the local formatter and is disabled by
default. It includes:

- `output_directory`, the `clips/ready/` root that routes new plain and hooked
  renders into their respective subdirectories, plus `output_width`,
  `output_height`, and `background_color`.
- `horizontal_margin`, `top_text_area_height`, `bottom_margin`, and maximum
  video width and height.
- `crop_mode`, which must remain `fit` for no-crop output.
- `output_frame_rate`, `video_codec`, `audio_codec`, `crf`, and
  `encoding_preset`.
- `overwrite` and `maximum_clips_per_run` queue safeguards.
- `enabled`, for intentionally enabling formatting in a normal pipeline run.
- `hook`, including font, wrapping, alignment, box, fallback-title, and optional
  outline/shadow settings.

`config/hooks.json` separately controls OpenAI generation and manual-review
limits. Its `automatic_selection` setting is `false` by default, so generated
candidates require review unless that setting is deliberately enabled.

## Run

Run the configured metadata collectors without downloading media:

```bash
python run_pipeline.py
```

Run the configured collectors and then explicitly download pending clips:

```bash
python run_pipeline.py --download
```

This `--download` flag is the recommended live manual test. Paste a URL you
intentionally want to retrieve into `input_urls.txt`, then run the command
above. No real URL is committed to this repository, and the normal pipeline
does not download a queued URL unless this flag is used or `downloader.enabled`
is deliberately set to `true`.

Format only clips that are already downloaded and awaiting processing. This
does not run collectors or download new media:

```bash
python run_pipeline.py --format
```

Run the configured collectors, then download pending clips, then format ready
vertical outputs in one explicit pass:

```bash
python run_pipeline.py --download --format
```

For a safe manual validation of one real local downloaded clip, run:

```bash
python run_pipeline.py --format-one
```

This formats at most one downloaded, pending clip and leaves its original file
in `clips/pending/`. It requires local `ffmpeg` and `ffprobe`; no real clip is
included in the repository.

To render one explicit manual hook without overwriting an existing reference
output, run:

```bash
python run_pipeline.py --format-one --hook "He looked away for one second..."
```

This selects one downloaded pending clip, or a ready clip when no pending clip
exists, and writes a hook-specific MP4 in `clips/ready/hooked/`. The file name
is based on the clip ID plus a stable hook-text digest, while the metadata points
to the newest ready render. The original pending download and any previous ready
reference file are retained.

The downloader prints a summary such as:

```text
Download queue
Pending: 8
Downloaded: 5
Skipped: 1
Failed: 2
```

## Reddit API Collector

For `reddit_api` or `both` mode, create a local credentials file:

```bash
copy .env.example .env
```

On macOS or Linux, use `cp .env.example .env`. Set these values in `.env`:

```text
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=
OPENAI_API_KEY=
```

The `.env` file is ignored by Git and must never contain committed credentials.
Edit `config/sources.json` for Reddit filters such as subreddits, score,
maximum age, sorting mode, and NSFW handling.

## Not Yet Implemented

There are no captions, logos, watermarks, video analysis, queueing, or Instagram
posting. FFmpeg is used only for downloader stream merges and the local vertical
formatting stage.
