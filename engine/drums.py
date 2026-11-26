#!/usr/bin/env python3
"""Drum pattern transcription: band-split onsets -> classes -> 16th grid -> MIDI.

Usage:
    python drums.py SONG [--meter 4] [--seconds 90] [--midi out.mid] [--pretty]

Four bands are filtered and onset-detected independently, so a kick and a hat
struck together come back as two hits instead of one merged blob. Rule-based,
deterministic, no model weights and no network.
"""
import argparse
import json
import os
import sys
import warnings

import numpy as np
import librosa
from scipy.signal import butter, sosfiltfilt

warnings.filterwarnings("ignore", module="librosa")

GM = {"kick": 36, "snare": 38, "clap": 39, "hat": 42, "openhat": 46,
      "tom": 45, "crash": 49, "ride": 51}
BANDS = {
    "kick": (30, 110),
    "snare": (140, 500),
    "body": (400, 1200),
    "presence": (1500, 5000),
    "air": (6000, 11000),
}
HOP = 256


def band_signal(y, sr, lo, hi):
    nyq = sr / 2.0
    if hi >= nyq:
        sos = butter(4, lo / nyq, btype="high", output="sos")
    else:
        sos = butter(4, [lo / nyq, min(hi, nyq - 1) / nyq], btype="band", output="sos")
    return sosfiltfilt(sos, y)


def band_envelope(sig, sr):
    return librosa.onset.onset_strength(y=sig, sr=sr, hop_length=HOP)


def detect(env, sr, delta=0.05, min_gap=0.09):
    frames = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=HOP,
                                        backtrack=True, units="frames", delta=delta)
    times = np.asarray(frames, dtype=float) * HOP / sr
    if times.size == 0:
        return np.array([]), np.array([])
    strengths = env[np.clip(np.asarray(frames, dtype=int), 0, len(env) - 1)]
    strengths = strengths / (strengths.max() + 1e-9)
    keep_t, keep_s = [times[0]], [strengths[0]]
    for t, s in zip(times[1:], strengths[1:]):
        if t - keep_t[-1] >= min_gap:
            keep_t.append(t)
            keep_s.append(s)
        elif s > keep_s[-1]:
            keep_t[-1], keep_s[-1] = t, s
    return np.array(keep_t), np.array(keep_s)


def band_rms(sig, sr):
    return librosa.feature.rms(y=sig, frame_length=512, hop_length=HOP)[0]


def attack_ratio(env, frame, win=8):
    """Peak energy around the onset over the local baseline just before it."""
    base = float(np.median(env[max(0, frame - win):max(1, frame - 1)])) + 1e-9
    peak = float(env[max(0, frame - 1):frame + 2].max())
    return peak / base, peak


def classify_hit(shares, tail):
    """One onset can be several drums at once: a kick under a hat is two hits,
    so each band votes independently instead of picking one winner. Thresholds
    are band-energy shares within the onset window."""
    fired = []
    if shares["kick"] > 0.35:
        fired.append("kick")
    if shares["body"] > 0.03 and shares["presence"] > 0.5 * shares["air"]:
        fired.append("snare")
    if shares["air"] > 0.15 or (shares["air"] > 0.06 and shares["air"] > 1.2 * shares["presence"]):
        fired.append("hat" if tail < 0.18 else ("crash" if tail > 0.45 else "openhat"))
    return fired


def grid_times(beats):
    if len(beats) < 2:
        return np.array([])
    times = []
    for i in range(len(beats) - 1):
        span = beats[i + 1] - beats[i]
        for s in range(4):
            times.append(beats[i] + span * s / 4.0)
    times.append(beats[-1])
    return np.array(times)


def downbeat_phase(beats, low_env, sr, meter):
    """Which beat of the bar carries the kick? Score each phase by low-end
    energy so the grid starts where the listener hears the downbeat."""
    if len(beats) < meter * 2:
        return 0
    energies = []
    for i in range(len(beats) - 1):
        a, b = int(beats[i] * sr / HOP), int(beats[i + 1] * sr / HOP)
        energies.append(float(low_env[a:max(a + 1, b)].sum()))
    energies = np.array(energies)
    scores = {}
    for phase in range(meter):
        idx = np.arange(phase, len(energies), meter)
        if idx.size:
            scores[phase] = float(np.median(energies[idx]))
    if not scores:
        return 0
    best_phase = max(scores, key=scores.get)
    # A half-bar offset is common when the kick lands on 1 and 3; prefer the
    # earlier phase unless the later one is clearly stronger.
    if best_phase != 0 and scores.get(0, 0.0) >= 0.85 * scores[best_phase]:
        return 0
    return best_phase


def write_midi(path, bpm, hits, meter=4):
    import mido
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.MetaMessage("time_signature", numerator=meter, denominator=4, time=0))
    events = []
    for h in hits:
        events.append((h["t"], 1, h["note"], h["velocity"]))
        events.append((h["t"] + 0.05, 0, h["note"], 0))
    events.sort(key=lambda e: (e[0], e[1]))
    now = 0.0
    for t, on, note, vel in events:
        delta = max(0, int(round((t - now) * 480 * bpm / 60.0)))
        now = t
        track.append(mido.Message("note_on" if on else "note_off", channel=9,
                                  note=note, velocity=vel, time=delta))
    mid.save(path)


def transcribe(path, meter=4, seconds=90.0, start=0.0):
    y, sr = librosa.load(path, sr=22050, mono=True, offset=start, duration=seconds)
    y, _ = librosa.effects.trim(y, top_db=40)
    # A little lead-in silence gives the onset detector a baseline, otherwise a
    # hit on the very first sample is invisible and the whole grid rotates.
    pad = 0.25
    y = np.concatenate([np.zeros(int(pad * sr)), y])

    full_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=full_env, sr=sr, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(np.asarray(beat_frames, dtype=int), sr=sr, hop_length=HOP) - pad
    if len(beats) < 4:
        return {"bpm": round(bpm, 2), "error": "too few beats detected"}

    sigs = {name: band_signal(y, sr, lo, hi) for name, (lo, hi) in BANDS.items()}
    rms = {name: band_rms(s, sr) for name, s in sigs.items()}
    kick_env = band_envelope(sigs["kick"], sr)

    frames = librosa.onset.onset_detect(onset_envelope=full_env, sr=sr, hop_length=HOP,
                                        backtrack=False, units="frames", delta=0.04)
    hits = []
    lag = int(0.18 * sr / HOP)
    for frame in frames:
        raw = {k: float(rms[k][max(0, frame - 1):frame + 2].max()) for k in rms}
        total = sum(raw.values()) + 1e-9
        shares = {k: v / total for k, v in raw.items()}
        peak = raw["air"] + 1e-9
        tail = float(rms["air"][frame + lag]) / peak if frame + lag < len(rms["air"]) else 0.0
        strength = float(full_env[frame] / (full_env.max() + 1e-9))
        t = float(frame * HOP / sr) - pad
        for instrument in classify_hit(shares, min(1.0, tail)):
            hits.append({"t": t, "instrument": instrument, "strength": strength})

    hits.sort(key=lambda h: h["t"])

    step_grid = grid_times(beats)
    step = float(np.median(np.diff(beats)) / 4.0)
    # beat_track marks beats slightly late; centre the grid on the attacks.
    deltas = [h["t"] - step_grid[int(np.argmin(np.abs(step_grid - h["t"])))] for h in hits]
    if deltas:
        step_grid = step_grid + float(np.median(deltas))
    phase = downbeat_phase(beats, kick_env, sr, meter)

    for h in hits:
        idx = int(np.argmin(np.abs(step_grid - h["t"])))
        h["step"] = idx
        h["offset_ms"] = round(float((h["t"] - step_grid[idx]) * 1000), 1)
    hits = [h for h in hits if abs(h["offset_ms"]) <= 70]

    step_offset = phase * 4
    for h in hits:
        h["step"] -= step_offset
    hits = [h for h in hits if h["step"] >= 0]

    bar_steps = meter * 4
    n_bars = max(1, int(np.ceil((max(h["step"] for h in hits) + 1) / bar_steps))) if hits else 1

    bars = []
    for b in range(n_bars):
        bar = {}
        for h in hits:
            if h["step"] // bar_steps != b:
                continue
            bar.setdefault(h["instrument"], []).append({
                "step": h["step"] % bar_steps,
                "velocity": int(20 + 107 * h["strength"]),
                "offset_ms": h["offset_ms"],
            })
        bars.append(bar)

    core = {}
    for inst in GM:
        best, best_count = None, -1
        for bar in bars:
            key = tuple(sorted(x["step"] for x in bar.get(inst, [])))
            if not key:
                continue
            count = sum(1 for b in bars
                        if tuple(sorted(x["step"] for x in b.get(inst, []))) == key)
            if count > best_count:
                best, best_count = key, count
        if best is not None:
            core[inst] = {"steps": list(best), "bars_matching": best_count}

    summary = {}
    for inst in GM:
        inst_hits = [h for h in hits if h["instrument"] == inst]
        if not inst_hits:
            continue
        summary[inst] = {
            "hits": len(inst_hits),
            "per_bar": round(len(inst_hits) / n_bars, 2),
            "mean_offset_ms": round(float(np.mean([h["offset_ms"] for h in inst_hits])), 1),
            "offset_spread_ms": round(float(np.std([h["offset_ms"] for h in inst_hits])), 1),
        }

    order = ["crash", "openhat", "hat", "tom", "snare", "kick"]
    tab = []
    for b, bar in enumerate(bars):
        cells = {inst: ["·"] * bar_steps for inst in order}
        for inst, items in bar.items():
            for item in items:
                cells.setdefault(inst, ["·"] * bar_steps)[item["step"]] = \
                    "X" if item["velocity"] > 100 else "x"
        tab.append(f"bar {b + 1}")
        for inst in order:
            if inst in cells and any(c != "·" for c in cells[inst]):
                tab.append(f"  {inst:<7}|{''.join(cells[inst])}|")

    offbeat = sum(1 for h in hits if h["step"] % 4 != 0)
    midi_hits = [{"t": h["step"] * step, "note": GM[h["instrument"]],
                  "velocity": max(1, min(127, int(20 + 107 * h["strength"])))}
                 for h in hits if h["instrument"] in GM]

    return {
        "file": path,
        "bpm": round(bpm, 2),
        "meter": meter,
        "step_seconds": round(step, 4),
        "onsets": len(hits),
        "bars": n_bars,
        "downbeat_phase": phase,
        "grid": bars,
        "core": core,
        "instruments": summary,
        "feel": {
            "offbeat_ratio": round(offbeat / max(1, len(hits)), 3),
            "mean_timing_offset_ms": round(float(np.mean([h["offset_ms"] for h in hits])), 1),
            "timing_spread_ms": round(float(np.std([h["offset_ms"] for h in hits])), 1),
        },
        "tab": "\n".join(tab),
        "_midi_hits": midi_hits,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--meter", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--midi")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    result = transcribe(args.file, args.meter, args.seconds, args.start)
    if "error" in result:
        print(json.dumps(result))
        return
    midi_hits = result.pop("_midi_hits")
    if args.midi:
        write_midi(args.midi, result["bpm"], midi_hits, args.meter)
        result["midi_path"] = os.path.abspath(args.midi)

    if args.pretty:
        print(f"{result['file']}  {result['bpm']:.1f} BPM · {result['bars']} bars · {result['onsets']} onsets")
        print(result["tab"])
        for inst, s in result["instruments"].items():
            print(f"  {inst:<8} {s['per_bar']:>4}/bar  offset {s['mean_offset_ms']:+.0f}±{s['offset_spread_ms']:.0f} ms")
        print(f"  offbeat {result['feel']['offbeat_ratio']:.0%}")
    else:
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
