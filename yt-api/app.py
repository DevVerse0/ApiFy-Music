import os
import re
import tempfile
import urllib.parse
import urllib.request

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="ApiFy YouTube API (keyless)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

SECRET = os.environ.get("EXTRACTOR_SECRET", "")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def check_auth(authorization: str):
    if SECRET and authorization != f"Bearer {SECRET}":
        raise HTTPException(status_code=401, detail="unauthorized")


def base_opts():
    clients = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "android,web").split(",") if c.strip()]
    opts: dict = {
        "quiet": True,
        "noplaylist": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": clients}},
    }
    cookies = os.environ.get("YT_COOKIES", "")
    if cookies:
        p = os.path.join(tempfile.gettempdir(), "ytcookies.txt")
        try:
            with open(p, "w") as f:
                f.write(cookies)
            opts["cookiefile"] = p
        except OSError:
            pass
    return opts


def ydl():
    from yt_dlp import YoutubeDL
    return YoutubeDL(base_opts())


def thumb_for(vid: str) -> dict:
    base = f"https://i.ytimg.com/vi/{vid}"
    return {
        "default": f"{base}/default.jpg",
        "medium": f"{base}/mqdefault.jpg",
        "high": f"{base}/hqdefault.jpg",
        "maxres": f"{base}/maxresdefault.jpg",
        "best": f"{base}/hqdefault.jpg",
    }


def _len_to_secs(s) -> int:
    parts = (s or "").strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _ytm_items(results, max_results=10):
    items = []
    for s in (results or [])[:max_results]:
        vid = s.get("videoId")
        if not vid:
            continue
        arts = s.get("artists") or []
        thumbs = s.get("thumbnails") or []
        turls = [t.get("url") for t in thumbs if t.get("url")]
        items.append({
            "id": vid,
            "title": s.get("title") or "Unknown",
            "artist": arts[0].get("name") if arts else "Unknown",
            "artistId": arts[0].get("id") if arts else None,
            "thumbnail": turls[-1] if turls else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "thumbnails": {
                "default": turls[0] if turls else None,
                "medium": turls[len(turls) // 2] if turls else None,
                "high": turls[-1] if turls else None,
                "best": turls[-1] if turls else None,
            },
            "duration": _len_to_secs(s.get("duration") or ""),
        })
    return items


def ytm_song_meta(vid):
    from ytmusicapi import YTMusic
    vd = YTMusic().get_song(vid).get("videoDetails", {})
    if not vd.get("title"):
        return None
    thumbs = (vd.get("thumbnail") or {}).get("thumbnails", [])
    turls = [t.get("url") for t in thumbs if t.get("url")]
    try:
        views = int(str(vd.get("viewCount", "0")).replace(",", ""))
    except ValueError:
        views = 0
    return {
        "id": vid,
        "title": vd.get("title") or "Unknown",
        "artist": vd.get("author") or "Unknown",
        "artistId": vd.get("channelId"),
        "thumbnail": turls[-1] if turls else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "thumbnails": thumb_for(vid),
        "duration": int(vd.get("lengthSeconds") or 0),
        "views": views,
    }


def audio_info(vid: str) -> dict:
    """Fresh audio stream info via yt-dlp (URL expires in ~5-10 min)."""
    from yt_dlp import YoutubeDL
    opts = base_opts()
    opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    with YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    url = info.get("url")
    if not url and info.get("formats"):
        fmts = [f for f in info["formats"] if f.get("acodec") != "none"]
        fmts.sort(key=lambda f: (f.get("abr") or 0), reverse=True)
        if fmts:
            url = fmts[0].get("url")
            info = {**info, **fmts[0]}
    if not url:
        raise ValueError("no audio url")
    return info


@app.get("/")
def root():
    return {
        "ok": True,
        "app": "ApiFy YouTube API",
        "endpoints": [
            "/health", "/search?q=&max=", "/thumbnail?id=",
            "/video/{id}", "/audio?id=", "/download?id=",
            "/play/{id}", "/trending?max=",
        ],
    }


@app.get("/health")
def health():
    return {"ok": True}


# ---------- SEARCH ----------
@app.get("/search")
def search(q: str = Query(...), max: int = Query(10, le=25), authorization: str = Header(default="")):
    check_auth(authorization)
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="q required")
    try:
        from ytmusicapi import YTMusic
        return JSONResponse({"items": _ytm_items(YTMusic().search(q, filter="songs", limit=max), max)})
    except ImportError:
        raise HTTPException(status_code=500, detail="ytmusicapi not installed")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"search failed: {e}")


# ---------- THUMBNAIL ----------
@app.get("/thumbnail")
def thumbnail(id: str = Query(...), authorization: str = Header(default="")):
    check_auth(authorization)
    if not ID_RE.match(id):
        raise HTTPException(status_code=400, detail="bad video id")
    t = thumb_for(id)
    return JSONResponse({"id": id, "thumbnail": t["best"], "thumbnails": t})


# ---------- VIDEO METADATA ----------
@app.get("/video/{vid}")
def video(vid: str, authorization: str = Header(default="")):
    check_auth(authorization)
    if not ID_RE.match(vid):
        raise HTTPException(status_code=400, detail="bad video id")
    try:
        with ydl() as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except Exception:
        try:
            meta = ytm_song_meta(vid)
        except Exception:
            meta = None
        if not meta:
            raise HTTPException(status_code=502, detail="metadata blocked right now (rate-limit, retry shortly)")
        meta["source"] = "ytmusic"
        return JSONResponse(meta)
    if not info:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse({
        "id": vid,
        "title": info.get("title") or "Unknown",
        "description": (info.get("description") or "")[:500],
        "artist": info.get("channel") or info.get("uploader") or "Unknown",
        "artistId": info.get("channel_id"),
        "thumbnail": info.get("thumbnail"),
        "thumbnails": thumb_for(vid),
        "duration": info.get("duration") or 0,
        "views": info.get("view_count") or 0,
        "likes": info.get("like_count") or 0,
        "source": "ytdlp",
    })


# ---------- AUDIO STREAM URL ----------
@app.get("/audio")
def audio(id: str = Query(...), authorization: str = Header(default="")):
    check_auth(authorization)
    if not ID_RE.match(id):
        raise HTTPException(status_code=400, detail="bad video id")
    try:
        info = audio_info(id)
    except ImportError:
        raise HTTPException(status_code=500, detail="yt-dlp not installed")
    except Exception:
        raise HTTPException(status_code=502, detail="audio blocked for this video right now (retry or set YT_COOKIES)")
    return JSONResponse({
        "id": id,
        "audioUrl": info.get("url"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
    })


# ---------- DOWNLOAD (proxied file, save as .m4a) ----------
@app.get("/download")
def download(id: str = Query(...), authorization: str = Header(default="")):
    check_auth(authorization)
    if not ID_RE.match(id):
        raise HTTPException(status_code=400, detail="bad video id")
    try:
        info = audio_info(id)
    except ImportError:
        raise HTTPException(status_code=500, detail="yt-dlp not installed")
    except Exception:
        raise HTTPException(status_code=502, detail="download blocked for this video right now (retry or set YT_COOKIES)")
    src = info.get("url")
    title = re.sub(r"[^\w\s-]", "", info.get("title") or id).strip()[:80] or id

    def gen():
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                yield chunk

    quoted = urllib.parse.quote(f"{title}.m4a")
    return StreamingResponse(
        gen(),
        media_type="audio/mp4",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# ---------- PLAY (metadata + audio in one call) ----------
@app.get("/play/{vid}")
def play(vid: str, authorization: str = Header(default="")):
    check_auth(authorization)
    if not ID_RE.match(vid):
        raise HTTPException(status_code=400, detail="bad video id")
    try:
        info = audio_info(vid)
        return JSONResponse({
            "id": vid,
            "title": info.get("title") or "Unknown",
            "artist": info.get("channel") or info.get("uploader") or "Unknown",
            "artistId": info.get("channel_id"),
            "thumbnail": info.get("thumbnail"),
            "thumbnails": thumb_for(vid),
            "duration": info.get("duration") or 0,
            "views": info.get("view_count") or 0,
            "audioUrl": info.get("url"),
        })
    except Exception:
        try:
            meta = ytm_song_meta(vid)
        except Exception:
            meta = None
        if not meta:
            raise HTTPException(status_code=502, detail="blocked right now (retry or set YT_COOKIES)")
        meta["audioUrl"] = None
        meta["audioBlocked"] = True
        return JSONResponse(meta)


# ---------- TRENDING ----------
@app.get("/trending")
def trending(max: int = Query(10, le=25), authorization: str = Header(default="")):
    check_auth(authorization)
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        charts = yt.get_charts(country="BD")
        items = []
        for pl in (charts.get("videos") or [])[:3]:
            try:
                tracks = yt.get_playlist(pl.get("playlistId"), limit=max).get("tracks", [])
                items += _ytm_items(
                    [{"videoId": t.get("videoId"), "title": t.get("title"),
                      "artists": t.get("artists"), "thumbnails": t.get("thumbnails"),
                      "duration": (t.get("length") or "")} for t in tracks], max)
            except Exception:
                continue
            if len(items) >= max:
                break
        if not items:
            items = _ytm_items(yt.search("trending songs", filter="songs", limit=max), max)
        return JSONResponse({"items": items[:max]})
    except ImportError:
        raise HTTPException(status_code=500, detail="ytmusicapi not installed")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trending failed: {e}")
