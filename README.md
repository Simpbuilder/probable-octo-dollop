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
the OpenAI SDK for optional hook-candidate generation, `requests` for the
explicit Zernio upload client, and Google's API/OAuth libraries for explicit
YouTube Shorts uploads. It also installs Streamlit for the optional local control UI.

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

Manual intake uses `manual_urls_per_run` in `config/collector.json`, with a
default of 50 URLs per run.

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
- `downloads_per_run`: maximum pending records attempted in one run; defaults to 50.
- `enabled`: automatic download control; it is `false` by default.

## Vertical Reel Formatter

The formatter selects clips with `download_status: downloaded`,
`processing_status: pending`, and an existing `local_file_path`. It keeps the
original download in `clips/pending/` and creates a separate ready MP4 in
`clips/ready/plain/` when no hook is rendered or `clips/ready/hooked/` when it
is. Metadata records the final ready-file path and its 1080x1920 output
dimensions while preserving source dimensions and the original local path.
Existing files already in `clips/ready/` are retained in place.

### Permanent Hooked Archive And Recreation

Every successful hooked render is copied into `clips/archive/hooked/` when the
archive is enabled in `config/archive.json`. The copy is verified by file size
and SHA-256 hash before metadata records its archive path, timestamp, and hash.
An archive-copy failure never invalidates a ready render; it is stored as a
retryable archive error instead.

`archive.json` keeps this behavior explicit: `copy_on_success` controls normal
formatter copies, `overwrite_existing` remains `false` by default, and a changed
file receives a timestamped archive version rather than replacing an earlier
copy. `verify_copy` and `archive_hash_enabled` keep verification enabled by
default.

Archive maintenance is explicit and never downloads, formats, or uploads media:

```bash
py run_pipeline.py --archive-missing
py run_pipeline.py --verify-archive
```

The archive is permanent local media. Safe cleanup, regeneratable-media cleanup,
and project reset all exclude `clips/archive/`, as well as credentials, upload
history, and posted videos.

To list or rebuild a single clip, use the saved metadata and source URL only:

```bash
py run_pipeline.py --list-recreatable
py run_pipeline.py --recreate CLIP_ID
py run_pipeline.py --recreate CLIP_ID --force
```

The recreation command asks for `RECREATE` before it changes anything; `--yes`
is available for the Streamlit background action or a deliberate scripted run.
It reuses a valid pending source file, re-downloads only when required, renders
one hooked ready file, and refreshes its archive copy. It never invokes an
Instagram or YouTube uploader.

In the Streamlit app, **Archive** provides the same archive/verify/recreate
actions through the existing local background-job status system. **Ready
Videos** also has a per-file delete action: after confirmation it removes only
that selected ready output, clears its ready-output metadata, and preserves its
source download, archive copy, hooks, metadata history, and upload history.

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

`maximum_clips_per_run` defaults to 50 in both `config/hooks.json` and
`config/formatter.json`. The same 50-item default applies to downloader and
manual-intake queues.

Review candidates locally before formatting with:

```bash
py review_hooks.py
```

For each clip, the reviewer displays its ID, original title, and three options.
It also prints the exact metadata file being edited. Enter `1`, `2`, or `3` to
copy that saved candidate into `selected_hook`; the reviewer never generates,
rewrites, or renders hooks.
Use `c` to enter a custom hook; `s` to leave it unchanged; `r` to reject all
candidates; or `all 1`, `all 2`, or
`all 3` to choose one option for the remaining clips. The saved `selected_hook`
is used by a later formatter run, but generation and review never start that run.

Inspect a single stored clip's hook flow without changing metadata, generating
hooks, or rendering video:

```bash
py run_pipeline.py --debug-hook-flow CLIP_ID
```

The command prints the metadata file, candidates, `selected_hook`, final render
choice, and its priority reason.

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

## Instagram Reel Uploads

The optional Zernio uploader is intentionally separate from collection and
formatting. It reads only direct `.mp4` files in `clips/ready/hooked/`; it never
reads `clips/ready/plain/`, `clips/pending/`, source downloads, or files that
have been moved to `clips/posted/`.

Create `.env` from the template and set `ZERNIO_API_KEY` to an API key with
access to the connected Instagram account. The key is loaded only for explicit
Zernio commands and is never printed. Use this command to inspect the account
IDs available to that key:

```bash
py run_pipeline.py --list-zernio-accounts
```

The command prints platform, username, display name, account ID, profile ID,
and connection status, but never credentials. Set `account_id` in
`config/instagram.json` when more than one active Instagram account is listed.
When exactly one active Instagram account is available, an explicit upload
command can use that unambiguous account without changing the saved config.

`config/instagram.json` controls the stage. It is disabled by default and has a
one-upload queue limit and `publish_mode: "draft"` for a deliberately cautious
first use. Its `default_caption` is passed to Zernio exactly as written; the
uploader does not append hooks, hashtags, emojis, or generated text.

Bulk draft and publish-now commands can optionally space successful posts.
`delay_between_posts_enabled` defaults to `true`,
`delay_between_posts_seconds` defaults to `30`, and `maximum_delay_seconds`
defaults to `300`. The delay is applied only between successful eligible posts;
duplicates, skipped files, failures, the first post, and the final post never
add a wait. Override it for one explicit batch with `--post-delay` (zero
disables spacing, and values above the configured maximum are capped):

```bash
py run_pipeline.py --upload-instagram --all --post-delay 30
py run_pipeline.py --upload-instagram --publish-now --all --post-delay 60
```

The local Instagram page provides the same setting with presets, a custom
seconds field, an estimated batch-spacing duration, and a visible countdown
during multi-video uploads.

## Live Dashboard Progress

Bulk dashboard actions for download, hook generation, formatting, Instagram
draft uploads, and immediate publishing run in one local background worker.
The dashboard and active-job banner update after each completed item without
blocking navigation or unsaved form values. The sidebar's **Live refresh**
control defaults to **2 seconds** and also offers 1 second, 5 seconds, or Off;
automatic refresh is active only while a batch is running. **Refresh status**
remains available for a manual update.

The active banner shows the stage, counts, current clip or file, failure count,
latest message, elapsed time, and an estimate once enough items have completed.
Use **Stop** to prevent the next item from starting. The current download,
format, API request, or upload is allowed to finish safely, and completed work
and retryable pending items are preserved.

Runtime details live only in the ignored `metadata/runtime_status.json` file.
It is written atomically and a missing or malformed file is treated as idle;
stale active state is recovered safely when the local UI starts another job.

To enable uploads, set `enabled` to `true`, review the account ID and caption,
then create one Zernio draft from one finished hooked Reel:

```bash
py run_pipeline.py --upload-one-instagram
```

This command performs the documented Zernio sequence: request a presigned media
URL, upload the local MP4, then create an Instagram post with
`contentType: "reels"`. The successful post ID, account ID, filename, public
media URL, timestamp, publish mode, and status are stored locally in
`metadata/zernio_post_history.json`. That file and Zernio's recent post list are
used to prevent duplicate upload attempts.

Create drafts up to the configured queue limit with:

```bash
py run_pipeline.py --upload-instagram
```

Use `--all` only when deliberately processing every eligible hooked Reel:

```bash
py run_pipeline.py --upload-instagram --all
```

Publishing immediately remains an explicit opt-in and should be used only after
the draft workflow is confirmed:

```bash
py run_pipeline.py --upload-one-instagram --publish-now
```

By default, a successful upload leaves the hooked MP4 in place. Set
`move_after_upload` to `true` to move it into `clips/posted/`, or set
`delete_after_upload` to `true` only when deletion is intentionally desired.
The two settings cannot be enabled together. Upload failures leave the local
video and do not stop later eligible files; retry the same explicit command
after correcting the reported issue.

## YouTube Shorts Uploads

The optional YouTube uploader reads only direct `.mp4` files from
`clips/ready/hooked/`. It preserves originals by default, records YouTube video
ID, URL, title, privacy status, timestamp, and retryable failures in matching
clip metadata, and saves local success history in
`metadata/youtube_upload_history.json`.

`config/youtube.json` defaults to public uploads, `selfDeclaredMadeForKids: false`,
duplicate protection, a 50-item safety limit, a 30-second delay between successful
uploads, and `move_after_upload: false`. The title uses `selected_hook`, then
`hook_text`, then the source title, then the filename stem; it is capped at
YouTube's 100-character title limit. The configured description and tags are used
as written. No AI description or automatic hashtags are added.

For first-time setup, download a desktop OAuth client from Google Cloud and save
it as `client_secret.json` in this project's root. Then run:

```bash
py run_pipeline.py --youtube-login
```

The command opens Google's consent screen in the default browser. After approval,
it saves `token.json` in the project root and prints the authenticated YouTube
channel name and ID. It does not inspect or upload a video. Both OAuth files are
ignored by Git and protected from cleanup.

The uploader and status command use those root-level OAuth files. The sibling
`ai-video-poster` upload history remains read-only and is used only as an additional
duplicate signal. A missing or invalid token produces a clean message; only the
explicit `--youtube-login` command opens a browser or writes a token.

Inspect authentication and queue status without uploading or changing the token:

```bash
py run_pipeline.py --youtube-status
```

Run one deliberate live test only after reviewing that status:

```bash
py run_pipeline.py --upload-youtube-one
```

Run the configured batch limit, or every eligible Short deliberately:

```bash
py run_pipeline.py --upload-youtube
py run_pipeline.py --upload-youtube --all
```

The uploader waits only between successful eligible uploads. It does not wait
before the first or after the final upload, and duplicates, skips, and failures
do not add a delay. A failed upload remains retryable and does not stop later
eligible files. Set `move_after_upload` only to move successful files to
`clips/posted/`; it never deletes a source video.

## Local UI

Start the local Streamlit UI with:

```bash
py -m streamlit run app.py
```

On Windows, double-click `start_ui.bat` to run the same command from the project
directory. The UI is local only and reuses the existing collectors, downloader,
hook generator, formatter, uploader, storage, and cleanup modules.

Its Dashboard shows queue counts, stored failures, ready hooked videos, upload
history counts, and pending YouTube Shorts; key values are never displayed. The
YouTube page shows reusable-token/channel status, configured visibility, local
history, delay, upload actions, and validated settings. Add URLs accepts one URL per line and preserves
the current queue's comments and valid existing entries. Pipeline controls run
the established stages, while publish-now controls remain disabled until their
explicit confirmation checkbox is selected.

Hook Review shows only saved candidates and writes selections through the same
metadata actions as `review_hooks.py`; it never generates or renders a hook.
The Videos tab plays local `clips/ready/hooked/` files and displays each saved
hook, processing status, and upload status. Configuration exposes common queue
limits, hook auto-selection, Instagram posting settings, and YouTube settings,
then validates the complete configuration before saving.

## Cleanup And Reset

Every cleanup command prints its exact plan before it deletes anything. Safe
cleanup is limited to cache directories, partial downloads, temporary metadata
files, temporary hook/FFmpeg artifacts, and zero-byte failed outputs:

```bash
py run_pipeline.py --cleanup
py run_pipeline.py --cleanup --dry-run
```

Use broad temporary cleanup only when pending downloads and ready renders can be
regenerated. It removes media in `clips/pending/`, `clips/ready/plain/`, and
`clips/ready/hooked/`, then resets matching metadata so deleted downloads become
pending and deleted ready renders become format-ready again:

```bash
py run_pipeline.py --cleanup --all-temporary
py run_pipeline.py --cleanup --all-temporary --yes
```

Without `--yes`, the broad cleanup asks for `YES`. The UI always previews this
scope and requires a confirmation checkbox.

For a fresh batch, use the destructive reset:

```bash
py run_pipeline.py --reset-project
```

It clears pending, approved, rejected, and ready regeneratable files, temporary
logs, `metadata/clips.json`, and the local `input_urls.txt` queue. It requires
typing `RESET` exactly; `--yes` can never bypass this. The UI requires the same
exact text before reset execution.

All cleanup levels preserve `.env`, every `config/` file, source code, Git data,
OAuth credential/token files, `metadata/processed_urls.txt`,
`metadata/zernio_post_history.json`, `metadata/youtube_upload_history.json`, and
`clips/posted/`. Default cleanup also preserves downloaded clips and formatted
ready videos. No cleanup action creates uploads or publishes to Instagram.

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

Each queue normally respects its configured safety limit. When additional work
remains, the stage prints its eligible, processing, and remaining counts. Use
`--all` to process every currently eligible item for any enabled stage:

```bash
python run_pipeline.py --download --all
python run_pipeline.py --generate-hooks --all
python run_pipeline.py --format --all
python run_pipeline.py --download --generate-hooks --format --all
```

The combined command runs collection, download, hook generation, then
formatting in that order. `--all` does not bypass media filters, error handling,
or retry safeguards; it only removes the per-run queue cap.

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
ZERNIO_API_KEY=
```

The `.env` file is ignored by Git and must never contain committed credentials.
Edit `config/sources.json` for Reddit filters such as subreddits, score,
maximum age, sorting mode, and NSFW handling.

## Not Yet Implemented

There are no captions, logos, watermarks, video analysis, or automatic social
posting. Instagram publishing is available only through the explicit Zernio
commands above. FFmpeg is used only for downloader stream merges and the local
vertical formatting stage.
