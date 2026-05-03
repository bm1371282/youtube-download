#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
REPO = os.environ["REPO"]
MAX_PART_BYTES = 1_900 * 1024 * 1024  # 1.9 GB


def run(cmd, **kwargs):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    return "playlist?list=" in url or "/playlist/" in url


def yt_dlp_cmd(url, output_template, playlist):
    fmt = (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720]+bestaudio"
        "/best[height<=720]"
    )
    no_playlist = "" if playlist else "--no-playlist"
    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f"--write-info-json {no_playlist} "
        f'-o "{output_template}" "{url}"'
    )


def split_file(path):
    prefix = str(path) + ".part"
    run(f'split -b {MAX_PART_BYTES} "{path}" "{prefix}"')
    parts = sorted(Path(path.parent).glob(path.name + ".part*"))
    return parts


def release_exists(tag):
    result = run(f'gh release view "{tag}" --repo "{REPO}"')
    return result.returncode == 0


def create_or_upload_release(tag, title, notes, files):
    files_str = " ".join(f'"{f}"' for f in files)
    if release_exists(tag):
        result = run(f'gh release upload "{tag}" {files_str} --repo "{REPO}" --clobber')
    else:
        notes_escaped = notes.replace('"', '\\"')
        result = run(
            f'gh release create "{tag}" {files_str} '
            f'--repo "{REPO}" --title "{title}" --notes "{notes_escaped}"'
        )
    return result


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

    cmd = yt_dlp_cmd(url, str(Path(tmpdir) / output_template), playlist)
    print(f"Downloading: {url}")
    result = run(cmd)

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

    # Read title from first info-json if available
    info_jsons = list(Path(tmpdir).rglob("*.info.json"))
    title = entry.get("title") or url
    if info_jsons:
        try:
            info = json.loads(info_jsons[0].read_text())
            title = info.get("title") or info.get("playlist_title") or title
        except Exception:
            pass

    if playlist:
        tag = f"yt-playlist-{Path(tmpdir).name}"
        # Try to get playlist id from info json
        if info_jsons:
            try:
                info = json.loads(info_jsons[0].read_text())
                pl_id = info.get("playlist_id") or info.get("playlist") or tag
                tag = f"yt-playlist-{pl_id}"[:100]
            except Exception:
                pass

        upload_files = []
        for mp4 in mp4_files:
            if mp4.stat().st_size > MAX_PART_BYTES:
                parts = split_file(mp4)
                upload_files.extend(parts)
            else:
                upload_files.append(mp4)

        notes = f"Source: {url}\nTo reassemble split parts: cat <name>.mp4.part* > <name>.mp4"
        result = create_or_upload_release(tag, title, notes, upload_files)
        if result.returncode != 0:
            entry["status"] = "failed"
            entry["error"] = result.stderr[-500:].strip()
            return entry

        entry["status"] = "done"
        entry["title"] = title
        entry["release_tag"] = tag
        entry["release_url"] = get_release_url(tag)

    else:
        mp4 = mp4_files[0]
        # Derive video id from filename (yt-dlp names it <id>.mp4)
        video_id = mp4.stem
        tag = f"yt-{video_id}"[:100]

        if mp4.stat().st_size > MAX_PART_BYTES:
            parts = split_file(mp4)
            notes = (
                f"Source: {url}\n"
                f"To reassemble: cat {mp4.name}.part* > {mp4.name}"
            )
            result = create_or_upload_release(tag, title, notes, parts)
        else:
            notes = f"Source: {url}"
            result = create_or_upload_release(tag, title, notes, [mp4])

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

        # Write after each video so partial progress survives a failure
        VIDEOS_JSON.write_text(json.dumps(videos, indent=2, ensure_ascii=False) + "\n")

    print("All done.")


if __name__ == "__main__":
    main()
