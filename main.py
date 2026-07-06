import os
import shutil
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="TeloPlay Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_COOKIES_PATH = "/etc/secrets/cookies.txt"
WRITABLE_COOKIES_PATH = "/tmp/cookies.txt"


# bgutil-ytdlp-pot-provider আলাদা Render service হিসেবে deploy করা আছে।
# এটা PO Token (Proof of Origin) সাপ্লাই করে yt-dlp কে - কড়া bot-detection
# tier এর ভিডিওগুলোতে (নতুন/label গান) শুধু cookies যথেষ্ট না, PO token
# ছাড়া YouTube "Sign in to confirm you're not a bot" দিয়ে ব্লক করে দেয়।
POT_PROVIDER_BASE_URL = "https://bgutil-ytdlp-pot-provider-xrd0.onrender.com"

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        },
        "youtubepot-bgutilhttp": {
            "base_url": [POT_PROVIDER_BASE_URL],
        },
    },
}

# Render এর Secret File read-only, কিন্তু yt-dlp resolve করার পর
# cookiejar.save() দিয়ে ওই ফাইলে write করার চেষ্টা করে -> crash করত।
# তাই startup এ Secret File থেকে /tmp তে (writable) কপি করে নিচ্ছি,
# আর yt-dlp কে সেই writable কপি ব্যবহার করাচ্ছি।
cookies_exist = os.path.exists(SECRET_COOKIES_PATH)
cookies_size = 0

if cookies_exist:
    shutil.copyfile(SECRET_COOKIES_PATH, WRITABLE_COOKIES_PATH)
    cookies_size = os.path.getsize(WRITABLE_COOKIES_PATH)
    YDL_OPTS["cookiefile"] = WRITABLE_COOKIES_PATH


@app.get("/health")
def health():
    return {
        "status": "ok",
        "cookies_file_exists": cookies_exist,
        "cookies_file_size_bytes": cookies_size,
        "cookies_likely_valid": cookies_size > 500,
        "cookies_path_used": WRITABLE_COOKIES_PATH if cookies_exist else None,
    }


@app.get("/debug-pot")
def debug_pot():
    """
    Temporary diagnostic endpoint. Runs yt-dlp with verbose logging and
    returns the captured debug lines so we can confirm whether the
    bgutil PO token plugin is actually being discovered/loaded by yt-dlp,
    and whether it's successfully reaching the remote pot-provider.
    Remove this route once the PO token setup is confirmed working -
    it's not meant to stay in production.
    """
    log_lines = []

    class CaptureLogger:
        def debug(self, msg):
            log_lines.append(f"DEBUG: {msg}")

        def warning(self, msg):
            log_lines.append(f"WARNING: {msg}")

        def error(self, msg):
            log_lines.append(f"ERROR: {msg}")

    debug_opts = dict(YDL_OPTS)
    debug_opts["verbose"] = True
    debug_opts["logger"] = CaptureLogger()
    debug_opts["quiet"] = False
    debug_opts["no_warnings"] = False

    # A known, cheap video id just to trigger extractor init / plugin
    # discovery logging - we don't care about the actual stream result here.
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    extraction_error = None
    try:
        with yt_dlp.YoutubeDL(debug_opts) as ydl:
            ydl.extract_info(test_url, download=False, process=False)
    except Exception as e:
        extraction_error = str(e)

    pot_related_lines = [
        line for line in log_lines
        if "pot" in line.lower() or "bgutil" in line.lower()
    ]

    return {
        "pot_related_debug_lines": pot_related_lines,
        "all_debug_lines_count": len(log_lines),
        "extraction_error": extraction_error,
        "pot_provider_base_url_configured": POT_PROVIDER_BASE_URL,
    }


@app.get("/stream/{video_id}")
def get_stream(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        error_text = str(e)
        if "Sign in to confirm" in error_text or "not a bot" in error_text:
            error_type = "BOT_DETECTION"
        elif "Video unavailable" in error_text:
            error_type = "VIDEO_UNAVAILABLE"
        elif "Private video" in error_text:
            error_type = "PRIVATE_VIDEO"
        else:
            error_type = "UNKNOWN"
        raise HTTPException(
            status_code=502,
            detail={"error_type": error_type, "message": error_text},
        )
    except Exception as e:
        # cookies write/save সহ যেকোনো অপ্রত্যাশিত এরর ধরার জন্য,
        # যাতে ভবিষ্যতে raw 500 এর বদলে সঠিক error দেখা যায়।
        raise HTTPException(
            status_code=500,
            detail={"error_type": "UNEXPECTED_ERROR", "message": str(e)},
        )

    stream_url = info.get("url")
    if not stream_url:
        raise HTTPException(status_code=502, detail={"error_type": "NO_URL_RETURNED"})

    return {
        "videoId": video_id,
        "title": info.get("title"),
        "url": stream_url,
        "ext": info.get("ext"),
        "abr": info.get("abr"),
        "duration": info.get("duration"),
    }