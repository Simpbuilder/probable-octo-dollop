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
make sure `ffmpeg` is available on your `PATH`; verify it with:

```bash
ffmpeg -version
```

The downloader checks for FFmpeg only when yt-dlp selects separate streams. A
single-file media format can still download without it. See the
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

There is no vertical formatting, FFmpeg-based layout rendering, captions,
hooks, AI analysis, queueing, or Instagram posting. FFmpeg is used only by
yt-dlp when it must merge downloaded audio and video streams.
