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
    # A small delay before each request can reduce how often YouTube's
    # bot-detection triggers (rapid, back-to-back requests from the same
    # IP are a common trigger). This adds a little latency per stream
    # resolve but is low-risk and doesn't need any external service.
    "sleep_interval_requests": 1,
    # Prefer m4a/opus audio-only streams over full video+audio muxes.
    "extractor_args": {
        "youtube": {
            # Try android/ios first (less likely to trigger bot-check),
            # fall back to web if a video reports "unavailable" on
            # mobile clients (happens for some region/age-related cases).
            "player_client": ["android", "ios", "web"],
        }
    },
}


@app.get("/health")
def health():
    """Used by the keep-alive cron job (cron-job.org) to prevent
    Render's free tier from spinning the service down."""
    return {"status": "ok"}


def classify_error(raw_message: str) -> tuple[str, str]:
    """
    Maps a raw yt-dlp error message to a stable error code + a short
    user-facing reason. The Flutter app should switch on `code`, not
    on the raw message text (which can change between yt-dlp versions
    and isn't meant for display).

    Returns: (code, reason)
    """
    msg = raw_message.lower()

    if "sign in to confirm" in msg or "not a bot" in msg:
        return "BOT_CHECK", "YouTube temporarily blocked this request"
    if "video unavailable" in msg:
        return "UNAVAILABLE", "This video is unavailable"
    if "private video" in msg:
        return "PRIVATE", "This video is private"
    if "age" in msg and ("restrict" in msg or "confirm" in msg):
        return "AGE_RESTRICTED", "This video is age-restricted"
    if "region" in msg or "not available in your country" in msg:
        return "REGION_BLOCKED", "This video isn't available in this region"
    if "copyright" in msg:
        return "COPYRIGHT", "This video was blocked due to a copyright claim"

    return "UNKNOWN", "Could not resolve this stream"


@app.get("/stream/{video_id}")
def get_stream(video_id: str):
    """
    Resolves a playable audio stream URL for the given YouTube video ID.

    Success response:
        {
            "videoId": str,
            "title": str,
            "url": str,          # direct, playable audio URL
            "ext": str,          # e.g. "m4a", "webm"
            "abr": float | None, # audio bitrate (kbps)
            "duration": int | None
        }

    Error response (raised as HTTPException, status 404 or 502):
        {
            "detail": {
                "code": str,       # stable machine-readable code, e.g. "BOT_CHECK"
                "reason": str,     # short human-readable reason
                "videoId": str,
                "retryable": bool  # true if trying again might succeed
            }
        }
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        code, reason = classify_error(str(e))
        # BOT_CHECK (and genuinely unknown errors) are known to be
        # inconsistent (roadmap bug #8) - the same video sometimes
        # succeeds on a later attempt, so we mark them retryable.
        # Permanent states (unavailable/private/region/copyright) are not.
        retryable = code in ("BOT_CHECK", "UNKNOWN")
        status_code = 404 if code in ("UNAVAILABLE", "PRIVATE") else 502

        raise HTTPException(
            status_code=status_code,
            detail={
                "code": code,
                "reason": reason,
                "videoId": video_id,
                "retryable": retryable,
            },
        )

    stream_url = info.get("url")
    if not stream_url:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "NO_STREAM_URL",
                "reason": "Resolved the video but got no playable URL",
                "videoId": video_id,
                "retryable": True,
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