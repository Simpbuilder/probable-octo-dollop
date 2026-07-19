# Viral Clip Pipeline

This repository is a standalone Python pipeline for finding funny video clips,
preparing them for Instagram Reels, and eventually helping queue or post them.
It remains separate from the `ai-video-poster` project.

## Current Collector

The first working source is Reddit. It uses PRAW and Reddit's official API to
inspect configured subreddits, filter suitable Reddit-hosted video posts, and
save their metadata in `metadata/clips.json`. It does not download video files.

The collector is deliberately split into focused modules:

- `collector.reddit_client` loads credentials and communicates with PRAW.
- `collector.reddit_filter` applies post eligibility rules.
- `collector.reddit_metadata` creates `ClipMetadata` records.
- `collector.storage` persists JSON metadata and prevents duplicates.
- `collector.collector` coordinates a run and returns a summary.

## Installation

Install the required packages in your Python virtual environment:

```bash
pip install -r requirements.txt
```

Create a local credentials file from the safe template:

```bash
copy .env.example .env
```

On macOS or Linux, use `cp .env.example .env` instead. Edit `.env` with the
three credentials for a Reddit application:

```text
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=
```

Register a Reddit application to obtain the client ID and client secret. The
user agent should uniquely identify this project, for example
`script:viral-clip-pipeline:0.1 (by u/your_reddit_username)`. The `.env` file
is ignored by Git; never commit real credentials.

## Configuration

Edit `config/sources.json` to configure the Reddit collector:

- `subreddits`: subreddit names to inspect.
- `minimum_score`: minimum Reddit score to accept.
- `maximum_clip_length_seconds`: reject videos with a known longer duration.
- `maximum_post_age_days`: reject older posts.
- `sorting_mode`: `hot`, `new`, or `top`.
- `top_time_filter`: for `top`, choose `day`, `week`, `month`, `year`, or `all`.
- `posts_to_inspect`: maximum submissions inspected per subreddit.
- `allow_nsfw`: set to `true` only when NSFW posts should be considered.

`config/collector.json` controls local workflow folders and the JSON metadata
file location.

## Run

```bash
python run_pipeline.py
```

Each run prints a summary of checked subreddits, inspected posts, accepted
metadata, duplicates, filtered posts, and recoverable errors. A missing or
invalid subreddit does not stop other configured subreddits from being checked.

## Not Yet Implemented

Videos are not downloaded or processed yet. There is no FFmpeg integration,
formatting, captions, hooks, AI analysis, queueing, or Instagram posting.
