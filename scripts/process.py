#!/usr/bin/env python3
import json
import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

VIDEOS_JSON = Path(__file__).parent.parent / "videos.json"
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
VIDEOS_DIR = Path(__file__).parent.parent / "videos"
MAX_FILE_SIZE_MB = 100  # Warning threshold for large files


def run(cmd, **kwargs):
    """Run a shell command and return the result"""
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kwargs)


def is_playlist(url):
    """Check if URL is a YouTube playlist"""
    return "playlist?list=" in url or "/playlist/" in url


def sanitize_filename(title):
    """Convert title to a valid filename"""
    # Replace invalid characters with underscores
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', title)
    # Replace spaces with underscores
    sanitized = sanitized.replace(' ', '_')
    # Remove consecutive underscores
    sanitized = re.sub(r'_+', '_', sanitized)
    # Remove any remaining non-alphanumeric characters
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '', sanitized)
    # Limit length
    sanitized = sanitized[:100]
    return sanitized


def yt_dlp_cmd(url, output_template, playlist):
    """Generate yt-dlp command"""
    js_flags = "--js-runtimes deno --remote-components ejs:npm"
    fmt = "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best"
    no_playlist = "" if playlist else "--no-playlist"

    cookies = ""
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        with open(COOKIES_FILE, 'r') as f:
            first_line = f.readline().strip()
            if '# Netscape HTTP Cookie File' in first_line:
                cookies = f'--cookies "{COOKIES_FILE}"'

    extractor_args = '--extractor-args "youtube:player_client=web,android,ios"'
    user_agent = '--user-agent "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'

    return (
        f'yt-dlp -f "{fmt}" --merge-output-format mp4 '
        f'--retries 10 --fragment-retries 10 --sleep-requests 5 '
        f'--sleep-interval 5 --max-sleep-interval 15 '
        f'--no-check-certificates --geo-bypass '
        f'--ignore-errors '
        f'{no_playlist} {cookies} {js_flags} {extractor_args} {user_agent} '
        f'-o "{output_template}" "{url}"'
    )


def read_info_json(tmpdir):
    """Read the .info.json file"""
    info_jsons = sorted(Path(tmpdir).rglob("*.info.json"))
    if not info_jsons:
        return {}
    try:
        return json.loads(info_jsons[0].read_text())
    except Exception:
        return {}


def update_videos_readme(videos):
    """Generate a README file listing all downloaded videos"""
    readme_path = Path(__file__).parent.parent / "VIDEOS.md"

    successful = [v for v in videos if v.get("status") == "done"]

    if not successful:
        if readme_path.exists():
            readme_path.unlink()
        return

    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write("# Downloaded Videos\n\n")
        f.write(f"Total videos downloaded: {len(successful)}\n\n")
        f.write("## Videos\n\n")

        for video in successful:
            f.write(f"### {video.get('title', 'Unknown')}\n")
            f.write(f"- **URL**: {video.get('url', 'N/A')}\n")
            f.write(f"- **Filename**: `{video.get('filename', 'N/A')}`\n")
            f.write(f"- **Size**: {video.get('file_size_mb', 0):.2f} MB\n")
            f.write(f"- **Downloaded**: {video.get('downloaded_at', 'N/A')}\n")
            f.write("\n")

    print(f"📄 Updated VIDEOS.md with {len(successful)} videos")


def process_entry(entry, tmpdir):
    """Process a single video/playlist entry"""
    url = entry["url"]
    playlist = is_playlist(url)

    # Create videos directory
    VIDEOS_DIR.mkdir(exist_ok=True)

    # Use a simple output template
    output_template = str(VIDEOS_DIR / "%(title)s.%(ext)s")

    print(f"\n📥 Downloading: {url}")
    result = run(yt_dlp_cmd(url, output_template, playlist))

    if result.returncode != 0:
        error_msg = result.stderr[-500:].strip()
        print(f"  ❌ ERROR: {error_msg}")
        entry["status"] = "failed"
        entry["error"] = error_msg
        return entry

    # Find downloaded video files
    video_files = []
    for ext in ['*.mp4', '*.mkv', '*.webm']:
        video_files.extend(VIDEOS_DIR.glob(ext))

    if not video_files:
        print("  ❌ ERROR: No video files produced")
        entry["status"] = "failed"
        entry["error"] = "No video files produced"
        return entry

    # Get the most recent video file
    video_file = max(video_files, key=lambda f: f.stat().st_mtime)

    # Get metadata
    info = read_info_json(tmpdir)
    title = info.get("title") or video_file.stem
    safe_filename = sanitize_filename(title)

    # Rename to safe filename
    new_path = VIDEOS_DIR / f"{safe_filename}.mp4"
    if video_file.suffix != '.mp4':
        # Convert to MP4 if needed (ffmpeg required)
        pass
    else:
        video_file.rename(new_path)

    file_size_mb = new_path.stat().st_size / (1024 * 1024)

    print(f"  📝 Title: {title}")
    print(f"  📁 File: {safe_filename}.mp4")
    print(f"  📦 Size: {file_size_mb:.2f} MB")

    # Save metadata
    metadata = {
        "url": url,
        "title": title,
        "filename": f"{safe_filename}.mp4",
        "file_size_mb": round(file_size_mb, 2),
        "downloaded_at": date.today().isoformat(),
        "video_id": info.get("id"),
        "uploader": info.get("uploader"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count")
    }

    metadata_path = VIDEOS_DIR / f"{safe_filename}.json"
    with open(metadata_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    if file_size_mb > MAX_FILE_SIZE_MB:
        print(f"  ⚠️ Warning: File > {MAX_FILE_SIZE_MB}MB. Consider Git LFS.")

    entry["status"] = "done"
    entry["title"] = title
    entry["filename"] = f"{safe_filename}.mp4"
    entry["file_size_mb"] = round(file_size_mb, 2)
    entry["downloaded_at"] = date.today().isoformat()

    print(f"  ✅ Done: Saved to videos/{safe_filename}.mp4")
    return entry


def main():
    """Main entry point"""
    print("=" * 60)
    print("🎬 YouTube Download Script")
    print("=" * 60)

    if not VIDEOS_JSON.exists():
        print(f"❌ Error: {VIDEOS_JSON} not found")
        exit(1)

    try:
        with open(VIDEOS_JSON, 'r', encoding='utf-8') as f:
            videos = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing videos.json: {e}")
        exit(1)

    pending = [v for v in videos if v.get("status") == "pending"]

    if not pending:
        print("✅ No pending videos.")
        return

    print(f"\n📋 Processing {len(pending)} video(s)...")

    for i, entry in enumerate(pending, 1):
        print(f"\n{'─' * 60}")
        print(f"📌 [{i}/{len(pending)}]")
        print(f"{'─' * 60}")

        with tempfile.TemporaryDirectory() as tmpdir:
            process_entry(entry, tmpdir)

        # Save progress after each video
        with open(VIDEOS_JSON, 'w', encoding='utf-8') as f:
            json.dump(videos, f, indent=2, ensure_ascii=False)
        print(f"💾 Progress saved")

    # Update README
    update_videos_readme(videos)

    # Summary
    successful = [v for v in videos if v.get("status") == "done"]
    failed = [v for v in videos if v.get("status") == "failed"]

    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    print(f"✅ Downloaded: {len(successful)}")
    print(f"❌ Failed: {len(failed)}")

    if successful:
        print(f"\n📁 Videos are in the 'videos/' directory")

    if failed:
        for f in failed:
            print(f"  - {f.get('url')}: {f.get('error', 'Unknown')[:100]}")
        exit(1)

    print("\n🎉 All done!")


if __name__ == "__main__":
    main()