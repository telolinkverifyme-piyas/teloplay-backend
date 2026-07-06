# TeloPlay Backend — clean restart with detailed diagnostics
#
# This version focuses ONLY on the cookies-based approach (no PO token
# provider) so we can clearly see whether cookies alone work or not,
# before adding more complexity.
#
# Run locally:
#   pip install -r requirements.txt
#   uvicorn main:app --host 0.0.0.0 --port 8000

import os
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

COOKIES_PATH = "/etc/secrets/cookies.txt"

YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        }
    },
}

cookies_exist = os.path.exists(COOKIES_PATH)
cookies_size = os.path.getsize(COOKIES_PATH) if cookies_exist else 0

if cookies_exist:
    YDL_OPTS["cookiefile"] = COOKIES_PATH


@app.get("/health")
def health():
    """Detailed health check - tells us exactly what state the cookies
    file is in, not just whether it exists. A 0-byte or tiny file means
    the Secret File upload didn't actually save the content."""
    return {
        "status": "ok",
        "cookies_file_exists": cookies_exist,
        "cookies_file_size_bytes": cookies_size,
        "cookies_likely_valid": cookies_size > 500,  # a real cookies.txt is usually several KB
    }


@app.get("/stream/{video_id}")
def get_stream(video_id: str):
    """
    Resolves a playable audio stream URL. Returns detailed error info
    (not just a generic message) so we can tell exactly which failure
    mode we're hitting: bot-detection, unavailable video, network issue,
    or something else.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        error_text = str(e)

        # Classify the error so the Flutter app (and we, debugging) know
        # exactly what kind of failure this is.
        if "Sign in to confirm" in error_text or "not a bot" in error_text:
            error_type = "BOT_DETECTION"
        elif "Video unavailable" in error_text or "This video is not available" in error_text:
            error_type = "VIDEO_UNAVAILABLE"
        elif "Private video" in error_text:
            error_type = "PRIVATE_VIDEO"
        else:
            error_type = "UNKNOWN"

        raise HTTPException(
            status_code=502,
            detail={
                "error_type": error_type,
                "message": error_text,
                "cookies_were_used": cookies_exist,
                "cookies_file_size": cookies_size,
            },
        )

    stream_url = info.get("url")
    if not stream_url:
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "NO_URL_RETURNED",
                "message": "yt-dlp did not return a direct URL for this video",
            },
        )

    return {
        "videoId": video_id,
        "title": info.get("title"),
        "url": stream_url,
        "ext": info.get("ext"),
        "abr": info.get("abr"),
        "duration": info.get("duration"),
    }