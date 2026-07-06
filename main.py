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