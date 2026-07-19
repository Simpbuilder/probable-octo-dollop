# Viral Clip Pipeline

This repository is a standalone Python pipeline for collecting funny video
clips and preparing them for a future Instagram Reels workflow. It remains
separate from the `ai-video-poster` project.

## Collection Modes

`config/collector.json` selects which collector runs through `pipeline_mode`:

- `manual_urls`: import URLs from `input_urls.txt` without Reddit API access.
- `reddit_api`: run the existing PRAW Reddit metadata collector.
- `both`: run manual URL intake first, then the Reddit API collector.

The checked-in configuration defaults to `manual_urls` so work can continue
while Reddit Data API approval is pending. Change it to `reddit_api` or `both`
when credentials and approval are available.

## Manual URL Intake

Add one public HTTP or HTTPS URL per line in `input_urls.txt`. The queue also
supports blank lines and comments beginning with `#`:

```text
# A note for the next intake run
https://www.reddit.com/r/funny/comments/example_post/

https://example.com/another-public-url
```

URLs are normalized for stable duplicate detection while the original pasted
URL is preserved in clip metadata. Reddit domains are detected and their
subreddit is recorded when it is present in the URL. Valid non-Reddit URLs are
also accepted for future source support.

After an accepted URL, or a URL already represented in metadata, the queue
entry is removed from `input_urls.txt` and appended to
`metadata/processed_urls.txt`. Invalid URLs and URLs that fail to save remain
in `input_urls.txt` for correction or retry. The processed log is local runtime
data and is ignored by Git.

Manual intake creates metadata only. It does not fetch pages, resolve media,
or download any video files.

## Reddit API Collector

The existing Reddit collector uses PRAW and Reddit's official API to inspect
configured subreddits, filter Reddit-hosted video posts, and save their
metadata in `metadata/clips.json`.

Install dependencies in your Python virtual environment:

```bash
pip install -r requirements.txt
```

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

## Configuration

Edit `config/sources.json` for Reddit-specific collection rules:

- `subreddits`: subreddit names to inspect.
- `minimum_score`: minimum Reddit score to accept.
- `maximum_clip_length_seconds`: reject videos with a known longer duration.
- `maximum_post_age_days`: reject older posts.
- `sorting_mode`: `hot`, `new`, or `top`.
- `top_time_filter`: for `top`, choose `day`, `week`, `month`, `year`, or `all`.
- `posts_to_inspect`: maximum submissions inspected per subreddit.
- `allow_nsfw`: set to `true` only when NSFW posts should be considered.

`config/collector.json` also controls local workflow folders, metadata storage,
and the active `pipeline_mode`.

## Run

```bash
python run_pipeline.py
```

Each active collector prints a concise summary. The manual queue runs without
credentials; the Reddit collector reports a clear setup message if credentials
are unavailable.

## Not Yet Implemented

No media is downloaded or processed yet. There is no FFmpeg integration,
formatting, captions, hooks, AI analysis, queueing, or Instagram posting.
