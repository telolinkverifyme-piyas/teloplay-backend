import os
import re
import shutil
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
import yt_dlp

app = FastAPI(title="TeloPlay Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# /resolve-info response e pura player JS (~2.5MB raw text) thake -
# GZip diye eta compress kore pathano hoy, transfer size onek kome jay
# (JS text shadharonoto 70-80% compress hoy). Kono extra kaj lage na -
# FastAPI/Starlette nijei minimum_size er beshi response gulo gzip kore.
app.add_middleware(GZipMiddleware, minimum_size=1000)

SECRET_COOKIES_PATH = "/etc/secrets/cookies.txt"
WRITABLE_COOKIES_PATH = "/tmp/cookies.txt"


# bgutil-ytdlp-pot-provider আলাদা Render service হিসেবে deploy করা আছে।
# এটা PO Token (Proof of Origin) সাপ্লাই করে yt-dlp কে - কড়া bot-detection
# tier এর ভিডিওগুলোতে (নতুন/label গান) শুধু cookies যথেষ্ট না, PO token
# ছাড়া YouTube "Sign in to confirm you're not a bot" দিয়ে ব্লক করে দেয়।
#
# NOTE (Phase C, device-side resolve architecture e move korar por):
# eta ekhon shudhu purano /stream/{videoId} endpoint er jonno dorkar.
# Notun /resolve-info/{videoId} flow (niche) e ei cookies/PO-token kichu
# lage na, karon stream resolve device e (residential IP) hocche.
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

    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    extraction_error = None
    try:
        with yt_dlp.YoutubeDL(debug_opts) as ydl:
            ydl.extract_info(test_url, download=False)
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
    """
    OLD FLOW (yt-dlp + cookies + PO-token, Render datacenter IP theke resolve).
    Bot-detection issue er karone deprecate hocche dhire dhire - notun
    /resolve-info/{video_id} (device-side resolve architecture) stable
    howar por eta shorano hobe.
    """
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


# ============================================================
# PHASE C: NEW DEVICE-SIDE RESOLVE FLOW
# ============================================================
#
# Ei endpoint stream URL resolve kore na - shudhu raw player JS ("kaach-mal")
# app ke পাঠায়। Actual n-param/signature solving hobe device e (residential
# IP), flutter_js + yt-dlp-ejs solver bundle (lib.min.js + core.min.js,
# app assets e bundled) diye - Phase D te.

_YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_JS_URL_PATTERN = re.compile(r'"jsUrl":"([^"]+)"')
_JS_URL_PATTERN_ALT = re.compile(r'"PLAYER_JS_URL":"([^"]+)"')


async def _fetch_watch_page(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}&hl=en"
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=_YT_HEADERS)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "WATCH_PAGE_FETCH_FAILED",
                    "message": f"YouTube watch page fetch failed with status {resp.status_code}",
                },
            )
        return resp.text


def _extract_player_js_url(watch_html: str) -> str:
    match = _JS_URL_PATTERN.search(watch_html) or _JS_URL_PATTERN_ALT.search(watch_html)
    if not match:
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": "PLAYER_JS_URL_NOT_FOUND",
                "message": "player JS URL not found in watch page (YouTube may have changed page structure)",
            },
        )
    js_path = match.group(1)
    if js_path.startswith("//"):
        return f"https:{js_path}"
    if js_path.startswith("/"):
        return f"https://www.youtube.com{js_path}"
    return js_path


async def _fetch_player_js(js_url: str) -> str:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(js_url, headers=_YT_HEADERS)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "error_type": "PLAYER_JS_FETCH_FAILED",
                    "message": f"player JS fetch failed with status {resp.status_code}",
                },
            )
        return resp.text


def _extract_player_id(js_url: str) -> str:
    m = re.search(r"/s/player/([a-zA-Z0-9_-]+)/", js_url)
    return m.group(1) if m else js_url


@app.get("/resolve-info/{video_id}")
async def resolve_info(video_id: str):
    """
    Returns raw player JS + metadata needed for device-side n-param/sig
    solving via flutter_js + yt-dlp-ejs solver bundle (lib.min.js +
    core.min.js, bundled in the Flutter app's assets, NOT sent by backend).
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        raise HTTPException(
            status_code=400,
            detail={"error_type": "INVALID_VIDEO_ID", "message": "Invalid videoId format"},
        )

    watch_html = await _fetch_watch_page(video_id)
    player_url = _extract_player_js_url(watch_html)
    player_js = await _fetch_player_js(player_url)
    player_id = _extract_player_id(player_url)

    return JSONResponse(
        {
            "video_id": video_id,
            "player_url": player_url,
            "player_id": player_id,
            "player_js": player_js,
        }
    )