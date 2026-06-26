"""carclaude — FastAPI app: token auth, static PWA, voice + agent endpoints.

Routes (all /api/* require `Authorization: Bearer <APP_TOKEN>`):
  GET  /healthz          open liveness check
  GET  /api/config       model + working dir (for the UI)
  POST /api/stt          multipart audio  -> {"text": ...}
  POST /api/message      {"text": ...}    -> SSE stream of agent events
  POST /api/tts          {"text": ...}    -> audio/mpeg bytes
  /                      static PWA (web/)
"""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server.config import WEB_DIR, settings
from server import history, personalities, prefs, state, usage, voice
from server.agent import CarAgent

_agent: CarAgent | None = None
_agent_error: str | None = None
_turn_lock = asyncio.Lock()  # single user, single agent session -> one turn at a time


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _agent_error
    _agent = CarAgent(settings)
    try:
        await _agent.start()
        _agent_error = None
    except Exception as e:  # let health/STT/TTS still serve; report on /api/message
        _agent_error = f"{type(e).__name__}: {e}"
    yield
    if _agent:
        await _agent.aclose()


app = FastAPI(title="carclaude", lifespan=lifespan)


_CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; "
        "media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def _security_and_cache(request: Request, call_next):
    """Security headers on every response (anti-clickjacking + CSP defense-in-depth), plus
    no-cache on the PWA shell so front-end updates appear without a manual cache-clear (iOS
    home-screen apps otherwise serve stale JS/HTML)."""
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css", ".webmanifest", ".svg")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def require_token(request: Request) -> None:
    if not settings.app_token:
        raise HTTPException(503, "Server APP_TOKEN is not configured.")
    auth = request.headers.get("authorization", "")
    # Header-only: never accept the token as a ?token= query param — query strings leak into
    # proxy/cloudflared access logs, Referer headers, and browser history, and APP_TOKEN is
    # equivalent to host code execution. The web client only ever sends the Bearer header.
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not secrets.compare_digest(token, settings.app_token):
        raise HTTPException(401, "Bad or missing token.")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config", dependencies=[Depends(require_token)])
async def api_config() -> dict:
    p = prefs.load()
    return {"model": p["model"], "working_dir": settings.working_dir,
            "stt": settings.stt_provider, "tts": settings.tts_provider,
            "persona": state.persona_name, "effort": p["effort"],
            "pause_ms": p["pause_ms"], "max_ms": p["max_ms"],
            "barge_sensitivity": p["barge_sensitivity"], "speech_floor": p["speech_floor"],
            "status_ack": p["status_ack"], "ack_thinking": p["ack_thinking"],
            "ack_delay_ms": p["ack_delay_ms"], "ack_phrase": p["ack_phrase"]}


@app.get("/api/personalities", dependencies=[Depends(require_token)])
async def api_personalities() -> dict:
    return {"personas": [{"id": p["id"], "name": p["name"]} for p in personalities.catalog()],
            "active": state.persona_id}


@app.post("/api/personality", dependencies=[Depends(require_token)])
async def api_personality(payload: dict) -> dict:
    p = personalities.get((payload.get("id") or "").strip())
    if not p:
        raise HTTPException(404, "Unknown persona.")
    state.select(p["id"], p["name"], p["prompt"], p["voice_id"])
    return {"ok": True, "id": p["id"], "name": p["name"]}


@app.get("/api/usage", dependencies=[Depends(require_token)])
async def api_usage() -> dict:
    return await usage.report()


@app.post("/api/stt", dependencies=[Depends(require_token)])
async def api_stt(audio: UploadFile) -> JSONResponse:
    data = await audio.read()
    try:
        text = await voice.transcribe(data, audio.content_type or "audio/webm")
    except voice.VoiceError as e:
        raise HTTPException(502, str(e))
    if settings.stt_provider == "deepgram":
        usage.record_deepgram()
    return JSONResponse({"text": text})


@app.post("/api/tts", dependencies=[Depends(require_token)])
async def api_tts(payload: dict) -> Response:
    try:
        audio, mime = await voice.synthesize(payload.get("text", ""))
    except voice.VoiceError as e:
        raise HTTPException(502, str(e))
    return Response(content=audio, media_type=mime)


@app.post("/api/message", dependencies=[Depends(require_token)])
async def api_message(payload: dict) -> StreamingResponse:
    text = (payload.get("text") or "").strip()

    async def gen():
        if not text:
            yield _sse({"type": "done", "text": ""})
            return
        if _agent_error:
            yield _sse({"type": "error", "message": f"Agent unavailable: {_agent_error}"})
            return
        # Wait briefly for any prior turn to release (a just-interrupted barge-in turn
        # releases within a moment), rather than rejecting outright.
        try:
            await asyncio.wait_for(_turn_lock.acquire(), timeout=20)
        except asyncio.TimeoutError:
            yield _sse({"type": "error", "message": "Still busy with the previous request."})
            return
        try:
            assert _agent is not None
            async for event in _agent.ask(text):
                yield _sse(event)
        except Exception as e:  # surface to the UI rather than 500
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            _turn_lock.release()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/interrupt", dependencies=[Depends(require_token)])
async def api_interrupt() -> dict:
    """Barge-in: stop the agent's current turn so the next one (or silence) can take over."""
    if _agent is not None:
        await _agent.interrupt()
    return {"ok": True}


@app.post("/api/clear", dependencies=[Depends(require_token)])
async def api_clear() -> dict:
    """Reset the conversation like Claude Code's /clear: drop the live session's context and
    stop auto-recalling earlier turns. The on-disk transcript is kept (the agent can still be
    asked to look further back); only what it is handed at the start of the next turn resets."""
    # Barge in on any turn in flight, then try to take the turn lock so the in-flight turn's
    # final history.record() lands *before* our boundary marker (recall stops at the right
    # point). The mark itself is a single synchronous append — no torn write is possible under
    # single-threaded asyncio — so on a lock timeout we still mark, rather than dropping the live
    # session yet silently leaving recall intact.
    if _agent is not None:
        await _agent.interrupt()
    got = False
    try:
        await asyncio.wait_for(_turn_lock.acquire(), timeout=10)
        got = True
    except asyncio.TimeoutError:
        pass
    try:
        state.bump()             # next turn rebuilds the SDK session -> fresh conversation
        history.clear()          # mark the recall boundary whether or not we got the lock
    finally:
        if got:
            _turn_lock.release()
    return {"ok": True}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# Static PWA last, so /api routes and /healthz win.
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
