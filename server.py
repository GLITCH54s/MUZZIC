#!/usr/bin/env python3
"""MUZZIC — local music workbench.

Runs the analysis engine (key/scale, tempo, chords, structure, drum
transcription, taste critique) behind a tiny HTTP API and serves the UI.

    python server.py [--port 8765] [--host 127.0.0.1] [--no-browser]

No npm, no build step, no network calls: stdlib server + the bundled engine.
"""
import argparse
import functools
import math
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
CACHE = Path(tempfile.gettempdir()) / "muzzic"
CACHE.mkdir(parents=True, exist_ok=True)

ENGINE = ROOT / "engine"
if ENGINE.exists():
    sys.path.insert(0, str(ENGINE))
else:
    # Legacy layout: modules installed as Qoder skills. On any other machine
    # the vendored engine/ folder above is always used instead.
    SKILLS = Path(os.environ.get("MUZZIC_SKILLS", Path.home() / ".qoder" / "skills"))
    for sub in ("music-theory-analyze/scripts", "drum-transcribe/scripts", "music-taste-critic/scripts"):
        sys.path.insert(0, str(SKILLS / sub))

import theory as theory_mod          # noqa: E402
import drums as drums_mod            # noqa: E402
import score as score_mod            # noqa: E402

MAX_UPLOAD = 80 * 1024 * 1024
EXCERPT_SECONDS = 90.0
JOBS = {}
JOBS_LOCK = threading.Lock()
FFMPEG = shutil.which("ffmpeg")
ENGINE_LOCK = threading.Lock()


def ffmpeg_excerpt(src: Path, start: float, seconds: float) -> Path | None:
    if not FFMPEG:
        return None
    out = CACHE / f"excerpt-{src.stem[:24]}-{uuid.uuid4().hex[:8]}.wav"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(src),
           "-ac", "1", "-ar", "22050", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return out if out.exists() and out.stat().st_size > 4096 else None
    except Exception:
        return None


def finite(value):
    """JSON has no NaN/Infinity; drop them instead of emitting invalid payloads."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def measure_loudness(path: Path) -> float | None:
    if not FFMPEG:
        return None
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
           "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
           "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        blob = res.stderr[res.stderr.rfind("{"):res.stderr.rfind("}") + 1]
        value = float(json.loads(blob)["input_i"])
        return value if math.isfinite(value) and value > -70 else None
    except Exception:
        return None


def analyse_file(src: Path, start: float, seconds: float, name: str):
    prepared = ffmpeg_excerpt(src, start, seconds)
    target = prepared or src
    offset = 0.0 if prepared else start

    t0 = time.time()
    theory = theory_mod.analyse(str(target), offset, seconds, None)
    drums = drums_mod.transcribe(str(target), 4, seconds, offset)
    loudness = measure_loudness(target)
    critique = score_mod.score(theory, drums if "error" not in drums else None, loudness)

    midi_id = uuid.uuid4().hex
    if "error" not in drums:
        midi_path = CACHE / f"{midi_id}.mid"
        drums_mod.write_midi(str(midi_path), drums["bpm"], drums.pop("_midi_hits"), 4)
        with JOBS_LOCK:
            JOBS[midi_id] = midi_path

    if prepared and prepared.exists():
        prepared.unlink(missing_ok=True)

    return {
        "ok": True,
        "track": {"name": name, "seconds": theory["analysed_seconds"],
                  "source": src.name},
        "theory": theory,
        "drums": drums,
        "critique": critique,
        "midi": f"/api/midi/{midi_id}" if "error" not in drums else None,
        "elapsed": round(time.time() - t0, 1),
    }


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def end_headers(self):
        # Static files used to be cached heuristically, so a "reload" after a
        # fix could still run the old app.js. Never cache anything.
        if not getattr(self, "_cache_sent", False):
            self.send_header("Cache-Control", "no-store")
        self._cache_sent = False
        super().end_headers()

    def cors(self):
        # The page may also be opened straight off disk (file://) or from
        # another local preview. It listens on loopback only, so any web page
        # on this machine can reach it anyway — just answer CORS honestly.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def json(self, payload, status=200):
        body = json.dumps(finite(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._cache_sent = True
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path).path
        if route.startswith("/api/midi/"):
            job = route.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                path = JOBS.get(job)
            if not path or not path.exists():
                return self.json({"ok": False, "error": "midi not found"}, 404)
            data = path.read_bytes()
            self.send_response(200)
            self.cors()
            self.send_header("Content-Type", "audio/midi")
            self.send_header("Content-Disposition", 'attachment; filename="muzzic-drums.mid"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route == "/api/health":
            return self.json({"ok": True, "ffmpeg": bool(FFMPEG)})
        return super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/analyze":
            return self.json({"ok": False, "error": "unknown endpoint"}, 404)

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self.json({"ok": False, "error": "empty upload"}, 400)
            if length > MAX_UPLOAD:
                return self.json({"ok": False, "error": "file larger than 80 MB"}, 413)

            raw = self.rfile.read(length)
            name = unquote(self.headers.get("X-File-Name") or "track")
            name = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(name).name)[:80] or "track"
            start = float(self.headers.get("X-Start") or 0)
            seconds = float(self.headers.get("X-Seconds") or EXCERPT_SECONDS)
            seconds = max(8.0, min(180.0, seconds))

            suffix = Path(name).suffix.lower() or ".bin"
            src = CACHE / f"upload-{uuid.uuid4().hex[:10]}{suffix}"
            src.write_bytes(raw)

            with ENGINE_LOCK:
                result = analyse_file(src, start, seconds, name)
            result["track"]["bytes"] = len(raw)
            src.unlink(missing_ok=True)
            return self.json(result)

        except SystemExit as exc:
            return self.json({"ok": False, "error": str(exc) or "analysis failed"}, 422)
        except Exception as exc:
            traceback.print_exc()
            return self.json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def warm_engine():
    """JIT-compile librosa/numba paths once so the first upload is not slow."""
    try:
        import numpy as np
        import soundfile as sf
        probe = CACHE / "probe.wav"
        y = (np.random.default_rng(0).standard_normal(44100) * 0.1).astype("float32")
        sf.write(str(probe), y, 22050)
        theory_mod.analyse(str(probe), 0, 2.0)
        drums_mod.transcribe(str(probe), 4, 2.0)
        probe.unlink(missing_ok=True)
        print("engine warm")
    except Exception:
        traceback.print_exc()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to bind; use 0.0.0.0 inside Docker or on a LAN")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-warm", dest="warm", action="store_false")
    args = ap.parse_args()

    if not PUBLIC.exists():
        sys.exit(f"missing UI folder: {PUBLIC}")

    if args.warm:
        print("warming librosa (first run compiles numba kernels, ~10 s)...")
        warm_engine()

    handler = functools.partial(Handler, directory=str(PUBLIC))
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        sys.exit(f"cannot bind {args.host}:{args.port} — {exc}\n"
                 f"another MUZZIC instance may already be running; try --port {args.port + 1}")

    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"\n  MUZZIC  →  {url}")
    print(f"  ffmpeg: {'yes' if FFMPEG else 'not found (WAV/FLAC/OGG work; install ffmpeg for mp3/m4a)'}")
    print("  ctrl-c to stop\n")
    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
