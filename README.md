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

This installs PRAW, `python-dotenv`, and `yt-dlp`.

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
`clips/ready/`. Metadata records the ready-file path and its 1080x1920 output
dimensions while preserving source dimensions and the original local path.

### Fit Layout

The default `fit` crop mode never crops or stretches source content. Landscape,
4:3, square, portrait, and already-vertical clips are scaled proportionally to
fit inside a white 1080x1920 canvas. The source is centered in the usable area,
with a configurable blank zone above it reserved for future hook text. No hook
text, captions, logos, or watermarks are rendered at this stage.

Formatted media uses H.264 video, AAC audio when source audio exists,
`yuv420p`, a configured constant output frame rate, and MP4 fast-start metadata.
Audio-free videos remain valid outputs. The formatter retries safely: a bad or
missing source stays `pending` and receives a `format_error` in metadata.

### Formatter Configuration

`config/formatter.json` controls the local formatter and is disabled by
default. It includes:

- `output_directory`, `output_width`, `output_height`, and `background_color`.
- `horizontal_margin`, `top_text_area_height`, `bottom_margin`, and maximum
  video width and height.
- `crop_mode`, which must remain `fit` for no-crop output.
- `output_frame_rate`, `video_codec`, `audio_codec`, `crf`, and
  `encoding_preset`.
- `overwrite` and `maximum_clips_per_run` queue safeguards.
- `enabled`, for intentionally enabling formatting in a normal pipeline run.

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
```

The `.env` file is ignored by Git and must never contain committed credentials.
Edit `config/sources.json` for Reddit filters such as subreddits, score,
maximum age, sorting mode, and NSFW handling.

## Not Yet Implemented

There are no captions, hook-text rendering, logos, watermarks, AI analysis,
queueing, or Instagram posting. FFmpeg is used only for downloader stream
merges and the local vertical formatting stage.
