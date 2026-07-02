# TeloPlay Backend
# A tiny FastAPI service that wraps yt-dlp to resolve playable audio
# stream URLs from a YouTube/YouTube Music video ID.
#
# Endpoints:
#   GET /health          -> used by the keep-alive cron ping
#   GET /stream/{videoId} -> returns a direct audio URL + metadata
#
# Run locally:
#   pip install -r requirements.txt
#   uvicorn main:app --host 0.0.0.0 --port 8000
#
# yt-dlp does the heavy lifting here (cipher solving, client fallback,
# po_token handling) - this file just exposes it over HTTP so Flutter
# can call it with a simple GET request.

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI(title="TeloPlay Backend")

# Allow the Flutter app (any origin) to call this API.
# Fine for now; can be locked down to specific origins later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reused across requests - yt-dlp caches some info internally which
# speeds up repeated calls.
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": False,
    # Prefer m4a/opus audio-only streams over full video+audio muxes.
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}


@app.get("/health")
def health():
    """Used by the keep-alive cron job (cron-job.org) to prevent
    Render's free tier from spinning the service down."""
    return {"status": "ok"}


@app.get("/stream/{video_id}")
def get_stream(video_id: str):
    """
    Resolves a playable audio stream URL for the given YouTube video ID.

    Returns:
        {
            "videoId": str,
            "title": str,
            "url": str,          # direct, playable audio URL
            "ext": str,          # e.g. "m4a", "webm"
            "abr": float | None, # audio bitrate (kbps)
            "duration": int | None
        }
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not resolve stream: {str(e)}",
        )

    stream_url = info.get("url")
    if not stream_url:
        raise HTTPException(
            status_code=502,
            detail="yt-dlp did not return a direct URL for this video",
        )

    return {
        "videoId": video_id,
        "title": info.get("title"),
        "url": stream_url,
        "ext": info.get("ext"),
        "abr": info.get("abr"),
        "duration": info.get("duration"),
    }