#!/usr/bin/env python3
"""Backfill embeddings for all existing messages that don't have one.

Usage:
    python scripts/backfill_embeddings.py [--batch-size N] [--dry-run]

Options:
    --batch-size N    Messages per batch (default: 50)
    --dry-run         Count messages but don't embed

Rate limiting: OpenAI text-embedding-3-small allows 3000 requests/min.
We default to 50 requests/batch with a 1-second sleep between batches,
which keeps us well under the limit even with retries.

This script is idempotent — safe to run multiple times. It skips messages
that already have embeddings, messages shorter than 50 chars, and test-result
type messages.
"""

import argparse
import asyncio
import os
import sys
import time

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import switchboard.db as db
import switchboard.embeddings.service as emb


async def run(batch_size: int, dry_run: bool) -> None:
    await db.init_db()

    total = await db.count_messages_needing_embedding()
    if total == 0:
        print("No messages need embedding. All done!")
        return

    if dry_run:
        print(f"DRY RUN: {total} messages need embedding (would embed with batch_size={batch_size})")
        return

    print(f"Starting backfill: {total} messages to embed (batch_size={batch_size})")

    service = emb.get_embedding_service()
    embedded = 0
    failed = 0
    start_time = time.monotonic()

    while True:
        batch = await db.get_messages_needing_embedding(batch_size=batch_size)
        if not batch:
            break

        for msg in batch:
            content = msg["content"]
            msg_type = msg["type"]
            msg_id = msg["id"]

            # Double-check skip conditions (should already be filtered by query)
            if not emb.should_embed(content, msg_type):
                continue

            vector = await service.embed_safe(content)
            if vector:
                blob = emb.encode_vector(vector)
                await db.set_message_embedding(msg_id, blob)
                embedded += 1
            else:
                failed += 1

            total_done = embedded + failed
            elapsed = time.monotonic() - start_time
            rate = total_done / elapsed if elapsed > 0 else 0
            print(
                f"Embedded {embedded}/{total} messages... "
                f"({failed} failed, {rate:.1f} msg/s)",
                end="\r",
                flush=True,
            )

        # Brief pause between batches to respect rate limits
        # 3000 req/min = 50/s; batch_size=50 at 50/s = 1s per batch is fine
        await asyncio.sleep(1.0)

    print()  # newline after \r
    elapsed = time.monotonic() - start_time
    print(f"Done! Embedded {embedded} messages in {elapsed:.1f}s ({failed} failed).")
    await db.close_db()


def main():
    parser = argparse.ArgumentParser(description="Backfill embeddings for Switchboard messages")
    parser.add_argument("--batch-size", type=int, default=50, help="Messages per batch (default: 50)")
    parser.add_argument("--dry-run", action="store_true", help="Count only, don't embed")
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY environment variable not set.")
        print("Set it and retry: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    asyncio.run(run(batch_size=args.batch_size, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
