# Viral Clip Pipeline

This repository is a standalone Python foundation for an automated pipeline
that will discover funny video clips, prepare them for Instagram Reels, and
eventually help queue or post them. It is separate from the `ai-video-poster`
project.

## Planned Pipeline

1. Collect candidate clips from configured sources, starting with Reddit.
2. Store source metadata and downloaded clips for review.
3. Move approved clips through formatting, captions, branding, and final checks.
4. Prepare Reel-ready assets for queueing or posting.

## Collector Architecture

The collector is source-agnostic. `config/sources.json` holds each source's
collection rules, while `config/collector.json` defines the local output
folders and metadata file. `collector.config` loads and validates these files.

`collector.models` defines typed configuration and clip metadata records.
`collector.storage` stores those records in a schema-versioned JSON file and
provides duplicate checks using both the pipeline ID and source post ID. This
keeps the first implementation simple while leaving a clear path to a database
later.

`run_pipeline.py` demonstrates the architecture using temporary local storage:
it loads configuration, saves and reloads an example metadata record, then
confirms duplicate detection.

## Current Status

The local collector architecture, JSON configuration, metadata storage, and
tests are in place. Reddit fetching, credentials, networking, downloading,
video processing, captions, hooks, AI analysis, and Instagram posting have not
been added yet.
