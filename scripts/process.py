#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import zipfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
MAX_PART_BYTES = 1_900 * 1024 * 1024  # 1.9 GB


def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    return "playlist?list=" in url or "/playlist/" in url


def yt_dlp_cmd(url, output_template, playlist):
    # android client bypasses YouTube's JS n-challenge without needing a JS runtime
    # format fallback chain: prefer 720p mp4, then any 720p, then best available
    fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
    no_playlist = "" if playlist else "--no-playlist"
    cookies = f'--cookies "{COOKIES_FILE}"' if COOKIES_FILE else ""
    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--extractor-args "youtube:player_client=android,ios,web" '
        f"--write-info-json {no_playlist} {cookies} "
        f'-o "{output_template}" "{url}"'
    )


def read_info_json(tmpdir):
    info_jsons = sorted(Path(tmpdir).rglob("*.info.json"))
    if not info_jsons:
        return {}
    try:
        return json.loads(info_jsons[0].read_text())
    except Exception:
        return {}


def zip_files(files, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return zip_path


def split_and_zip(mp4, tmpdir):
    prefix = str(mp4) + ".part"
    run(f'split -b {MAX_PART_BYTES} "{mp4}" "{prefix}"')
    parts = sorted(Path(tmpdir).glob(mp4.name + ".part*"))
    zips = []
    for i, part in enumerate(parts, 1):
        zip_path = Path(tmpdir) / f"{mp4.stem}.part{i}.zip"
        zip_files([part], zip_path)
        zips.append(zip_path)
    return zips


def release_exists(tag):
    return run(f'gh release view "{tag}" --repo "{REPO}"').returncode == 0


def create_or_upload_release(tag, title, notes, files):
    files_str = " ".join(f'"{f}"' for f in files)
    notes_escaped = notes.replace('"', '\\"')
    if release_exists(tag):
        return run(f'gh release upload "{tag}" {files_str} --repo "{REPO}" --clobber')
    return run(
        f'gh release create "{tag}" {files_str} '
        f'--repo "{REPO}" --title "{title}" --notes "{notes_escaped}"'
    )


def get_release_url(tag):
    result = run(f'gh release view "{tag}" --repo "{REPO}" --json url -q .url')
    return result.stdout.strip() if result.returncode == 0 else ""


def process_entry(entry, tmpdir):
    url = entry["url"]
    playlist = is_playlist(url)
    output_template = (
        "%(playlist_id)s/%(playlist_index)03d-%(id)s.%(ext)s"
        if playlist
        else "%(id)s.%(ext)s"
    )

    print(f"Downloading: {url}")
    result = run(yt_dlp_cmd(url, str(Path(tmpdir) / output_template), playlist))
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[-500:]}")
        entry["status"] = "failed"
        entry["error"] = result.stderr[-500:].strip()
        return entry

    mp4_files = sorted(Path(tmpdir).rglob("*.mp4"))
    if not mp4_files:
        entry["status"] = "failed"
        entry["error"] = "No mp4 files produced by yt-dlp"
        return entry

    info = read_info_json(tmpdir)
    title = info.get("title") or info.get("playlist_title") or url

    if playlist:
        pl_id = info.get("playlist_id") or info.get("playlist") or Path(tmpdir).name
        tag = f"yt-playlist-{pl_id}"[:100]

        upload_files = []
        for mp4 in mp4_files:
            if mp4.stat().st_size > MAX_PART_BYTES:
                upload_files.extend(split_and_zip(mp4, tmpdir))
            else:
                zip_path = Path(tmpdir) / f"{mp4.stem}.zip"
                upload_files.append(zip_files([mp4], zip_path))

        notes = (
            f"Source: {url}\n"
            "Split parts: extract each zip, then: cat <name>.part*.mp4 > <name>.mp4"
        )
    else:
        # Read video id from info.json, not the filename (avoids .f398 suffix)
        video_id = info.get("id") or mp4_files[0].stem
        tag = f"yt-{video_id}"[:100]
        mp4 = mp4_files[0]

        if mp4.stat().st_size > MAX_PART_BYTES:
            upload_files = split_and_zip(mp4, tmpdir)
            notes = (
                f"Source: {url}\n"
                f"Split parts: extract each zip, then: cat {mp4.stem}.part*.mp4 > {mp4.name}"
            )
        else:
            zip_path = Path(tmpdir) / f"{video_id}.zip"
            upload_files = [zip_files([mp4], zip_path)]
            notes = f"Source: {url}"

    result = create_or_upload_release(tag, title, notes, upload_files)
    if result.returncode != 0:
        entry["status"] = "failed"
        entry["error"] = result.stderr[-500:].strip()
        return entry

    entry["status"] = "done"
    entry["title"] = title
    entry["release_tag"] = tag
    entry["release_url"] = get_release_url(tag)
    entry["downloaded_at"] = date.today().isoformat()
    print(f"  Done: {entry['release_url']}")
    return entry


def main():
    videos = json.loads(VIDEOS_JSON.read_text())
    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("No pending videos.")
        return

    for entry in pending:
        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)
        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")

    print("All done.")


if __name__ == "__main__":
    main()
