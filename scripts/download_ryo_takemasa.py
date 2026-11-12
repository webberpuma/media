#!/usr/bin/env python3
"""Download all images posted by ryotakemasa.bsky.social (Bluesky) at full size.

- Paginates app.bsky.feed.getAuthorFeed on the public AppView API (no auth needed).
- Keeps only the artist's own posts (author DID match), including replies with images.
- Downloads the full-size blob from cdn.bsky.app in the original format
  (mimeType -> @jpeg / @png / @webp path suffix), which is the largest version available.
- Idempotent/resumable: manifest.json tracks every blob CID, its source post, filename,
  size and sha256. Re-running skips anything already downloaded.
"""
import json
import os
import sys
import time
import hashlib
import urllib.request
import urllib.parse

HANDLE = "ryotakemasa.bsky.social"
API = "https://public.api.bsky.app/xrpc"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../media
OUT_DIR = os.path.join(BASE, "Ryo_Takemasa")
MANIFEST_PATH = os.path.join(OUT_DIR, "manifest.json")
CSV_PATH = os.path.join(OUT_DIR, "downloads.csv")
UA = "ryo-takemasa-image-fetcher/1.0 (personal archival)"

MIME_EXT = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def http_get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url):
    return json.loads(http_get(url))


def fetch_with_retry(url, attempts=4):
    last = None
    for i in range(attempts):
        try:
            return http_get(url)
        except Exception as e:  # noqa: BLE001 - retry any network error
            last = e
            time.sleep(2 * (i + 1))
    raise last


def collect_blobs(post):
    """Yield (cid, mime) for the artist's own images on a post (plain or recordWithMedia)."""
    rec = post.get("record") or {}
    emb = rec.get("embed") or {}
    imgs = []
    if emb.get("$type") == "app.bsky.embed.images":
        imgs = emb.get("images") or []
    elif emb.get("$type") == "app.bsky.embed.recordWithMedia":
        media = emb.get("media") or {}
        if media.get("$type") == "app.bsky.embed.images":
            imgs = media.get("images") or []
    for im in imgs:
        blob = im.get("image") or {}
        cid = (blob.get("ref") or {}).get("$link") or blob.get("cid")
        if cid:
            yield cid, blob.get("mimeType", "image/jpeg")


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MANIFEST_PATH)


def write_csv(manifest):
    rows = sorted(
        manifest.items(),
        key=lambda kv: (kv[1].get("posted_at", ""), kv[1].get("filename", "")),
    )
    with open(CSV_PATH, "w", encoding="utf-8") as f:
        f.write("filename,posted_at,post_uri,alt_text,bytes,sha256,cid,status\n")
        for cid, m in rows:
            alt = (m.get("alt") or "").replace('"', "'").replace("\n", " ")
            f.write(
                f"{m.get('filename','')},{m.get('posted_at','')},\"{m.get('post_uri','')}\",\"{alt}\","
                f"{m.get('bytes','')},{m.get('sha256','')},{cid},{m.get('status','')}\n"
            )


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = load_manifest()

    # Skip posts whose images are all already recorded + on disk with matching size.
    def already_done(cid):
        m = manifest.get(cid)
        return bool(m) and m.get("status") == "ok" and os.path.exists(
            os.path.join(OUT_DIR, m["filename"])
        ) and os.path.getsize(os.path.join(OUT_DIR, m["filename"])) == m.get("bytes")

    did = http_get_json(
        f"{API}/com.atproto.identity.resolveHandle?handle={urllib.parse.quote(HANDLE)}"
    )["did"]
    print(f"[info] {HANDLE} -> {did}")

    # --- paginate the full feed (default filter includes replies; we filter by author) ---
    feed_items, cursor = [], None
    while True:
        q = f"actor={urllib.parse.quote(did)}&limit=100"
        if cursor:
            q += f"&cursor={urllib.parse.quote(cursor)}"
        page = http_get_json(f"{API}/app.bsky.feed.getAuthorFeed?{q}")
        feed_items.extend(page.get("feed", []))
        cursor = page.get("cursor")
        print(f"[feed] fetched {len(feed_items)} feed items", flush=True)
        if not cursor:
            break
        time.sleep(0.4)

    # --- build download list: only the artist's own posts ---
    jobs = {}  # cid -> job info
    for item in feed_items:
        post = item.get("post") or {}
        if post.get("author", {}).get("did") != did:
            continue  # repost / quote of someone else
        uri = post.get("uri", "")
        rkey = uri.rsplit("/", 1)[-1]
        rec = post.get("record") or {}
        posted_at = (rec.get("createdAt") or "")[:19].replace("T", " ")
        date = posted_at[:10] or "unknown"
        # alt texts by position, from the view or the record
        view_imgs = ((post.get("embed") or {}).get("images")) or []
        alts = [(v.get("alt") or "") for v in view_imgs]
        for idx, (cid, mime) in enumerate(collect_blobs(post), start=1):
            if cid in jobs:
                continue
            ext = MIME_EXT.get(mime, "jpeg")
            jobs[cid] = {
                "cid": cid,
                "mime": mime,
                "ext": ext,
                "post_uri": uri,
                "posted_at": posted_at,
                "filename": f"{date}_{rkey}_{idx}.{ext}",
                "alt": alts[idx - 1] if idx - 1 < len(alts) else "",
            }

    print(f"[info] unique images by artist: {len(jobs)}")

    # --- download ---
    ok = skip = fail = 0
    for n, (cid, job) in enumerate(jobs.items(), start=1):
        if already_done(cid):
            skip += 1
            continue
        url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@{job['ext']}"
        path = os.path.join(OUT_DIR, job["filename"])
        try:
            data = fetch_with_retry(url)
            with open(path, "wb") as f:
                f.write(data)
            manifest[cid] = {
                **job,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": "ok",
                "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            ok += 1
            print(f"[{n}/{len(jobs)}] OK   {job['filename']} ({len(data):,} B)", flush=True)
        except Exception as e:  # noqa: BLE001
            manifest.setdefault(cid, {**job, "status": "failed", "error": str(e)})
            fail += 1
            print(f"[{n}/{len(jobs)}] FAIL {job['filename']}: {e}", flush=True)
        if n % 20 == 0:
            save_manifest(manifest)  # checkpoint
        time.sleep(0.25)

    save_manifest(manifest)
    write_csv(manifest)
    print(f"\n[done] downloaded={ok} skipped(already had)={skip} failed={fail} total={len(jobs)}")
    print(f"[done] images in: {OUT_DIR}")
    print(f"[done] tracking: {MANIFEST_PATH} + {CSV_PATH}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
