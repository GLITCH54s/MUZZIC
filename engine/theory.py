#!/usr/bin/env python3
"""Key / scale / mode, chords, tempo, meter and structure for an audio file.

Usage:
    python theory.py SONG [--start 0] [--seconds 90] [--tonic C] [--pretty]

Prints one JSON object on stdout. Analysis is mono at 22.05 kHz, which is
plenty for pitch-class and rhythm work and much faster than full rate.
"""
import argparse
import json
import sys
import warnings

import numpy as np
import librosa

warnings.filterwarnings("ignore", module="librosa")

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

KRUMHANSL_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KRUMHANSL_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

MODES = {
    "ionian": [0, 2, 4, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "aeolian": [0, 2, 3, 5, 7, 8, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
}
MODE_PLAIN = {"ionian": "major", "aeolian": "minor"}
MAJOR_FAMILY = {"ionian", "lydian", "mixolydian"}
ROMAN = {
    "ionian": ["I", "ii", "iii", "IV", "V", "vi", "vii°"],
    "lydian": ["I", "II", "iii", "#iv°", "V", "vi", "vii"],
    "mixolydian": ["I", "ii", "iii°", "IV", "v", "vi", "bVII"],
    "aeolian": ["i", "ii°", "III", "iv", "v", "VI", "VII"],
    "dorian": ["i", "ii", "III", "IV", "v", "vi°", "VII"],
    "phrygian": ["i", "bII", "III", "iv", "v°", "VI", "vii"],
    "locrian": ["i°", "bII", "III", "iv", "bV", "VI", "bvii"],
}

CHORD_WINDOW = 0.45
SUS4_PENALTY = 0.04
CHORD_STAY_BONUS = 0.055

CHORDS = {"maj": [0, 4, 7], "min": [0, 3, 7], "dim": [0, 3, 6],
          "sus4": [0, 5, 7], "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11],
          "min7": [0, 3, 7, 10]}
SUFFIX = {"maj": "", "min": "m", "dim": "dim", "sus4": "sus4",
          "7": "7", "maj7": "maj7", "min7": "m7"}


def pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    den = np.linalg.norm(a) * np.linalg.norm(b)
    return float(a @ b / den) if den else 0.0


def mode_profile(degrees, mode):
    """Krumhansl profile mapped onto a mode's degrees. Major-family modes use
    the major profile, minor-family the minor one: sharing a single profile
    makes minor modes inherit a major third's weight and flips the mode."""
    base, src = ((KRUMHANSL_MAJOR, [0, 2, 4, 5, 7, 9, 11]) if mode in MAJOR_FAMILY
                 else (KRUMHANSL_MINOR, [0, 2, 3, 5, 7, 8, 10]))
    base = base - base.mean()
    prof = np.zeros(12)
    for i, d in enumerate(degrees):
        prof[d % 12] = base[src[i]]
    return prof


def key_candidates(chroma_mean, top=6):
    out = []
    for mode, degrees in MODES.items():
        prof = mode_profile(degrees, mode)
        for tonic in range(12):
            out.append({
                "tonic": NOTES[tonic],
                "mode": mode,
                "label": f"{NOTES[tonic]} {MODE_PLAIN.get(mode, mode)}",
                "score": round(pearson(chroma_mean, np.roll(prof, tonic)), 4),
            })
    out.sort(key=lambda c: -c["score"])
    return out[:top]


def estimate_tempo(y, sr):
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    frames = np.asarray(beats, dtype=int)
    confidence = 0.0
    try:
        per_frame = librosa.feature.tempo(onset_envelope=onset_env, sr=sr, aggregate=None)
        if per_frame.size:
            confidence = float(1.0 - min(np.std(per_frame) / max(np.mean(per_frame), 1e-6), 1.0))
    except Exception:
        pass
    return {
        "bpm": round(bpm, 2),
        "half_time": round(bpm / 2, 2),
        "double_time": round(bpm * 2, 2),
        "beat_count": int(len(frames)),
        "beat_confidence": round(confidence, 3),
    }, frames, onset_env


def estimate_meter(onset_env, beats):
    """Fold inter-beat accent strength by candidate meters; the meter that
    repeats most consistently wins."""
    if len(beats) < 8:
        return {"meter": 4, "confidence": 0.0, "scores": {}}
    strengths = np.array([
        float(onset_env[beats[i]:max(beats[i] + 1, beats[i + 1])].sum())
        for i in range(len(beats) - 1)
    ])
    strengths = (strengths - strengths.mean()) / (strengths.std() + 1e-9)
    best, scores = (4, -1e9), {}
    for meter in (2, 3, 4, 6):
        usable = len(strengths) - len(strengths) % meter
        if usable < meter * 3:
            continue
        folded = strengths[:usable].reshape(-1, meter)
        explained = float(np.var(folded.mean(axis=0))) / (float(np.var(folded)) + 1e-9)
        # Folding into more groups explains more variance by construction, so
        # 4/4 gets a prior: only a clear winner displaces the default.
        if meter != 4:
            explained -= 0.05
        scores[str(meter)] = round(float(explained), 3)
        if explained > best[1]:
            best = (meter, explained)
    return {"meter": best[0], "confidence": round(max(0.0, min(1.0, best[1])), 3),
            "scores": scores}


def build_templates():
    vecs, roots, quals, labels = [], [], [], []
    for quality, degrees in CHORDS.items():
        for root in range(12):
            v = np.zeros(12)
            v[[(root + d) % 12 for d in degrees]] = 1.0
            vecs.append(v / np.linalg.norm(v))
            roots.append(root)
            quals.append(quality)
            labels.append(NOTES[root] + SUFFIX[quality])
    return np.stack(vecs, axis=1), roots, quals, labels


def viterbi_chords(sims, stay_bonus=None):
    stay_bonus = CHORD_STAY_BONUS if stay_bonus is None else stay_bonus
    """Decode the best chord path, paying a small cost to change chord.

    Per-beat argmax flickers: a beat that straddles a change scores as a hybrid
    (F# over A#m reads as F#maj7) and every flicker becomes a chord the report
    has to explain. Favouring the previous chord unless the new one is clearly
    better keeps bars whole.
    """
    n_beats, n_states = sims.shape
    score = sims[0].copy()
    back = np.zeros((n_beats, n_states), dtype=np.int16)
    for b in range(1, n_beats):
        stay = score + stay_bonus
        best_other = int(np.argmax(score))
        moved = score.copy()
        moved[best_other] = -np.inf
        switch = float(np.max(moved)) if n_states > 1 else -np.inf
        take_stay = stay >= switch
        score = sims[b] + np.where(take_stay, stay, switch)
        back[b] = np.where(take_stay, np.arange(n_states), best_other)
    path = [int(np.argmax(score))]
    for b in range(n_beats - 1, 0, -1):
        path.append(int(back[b][path[-1]]))
    return path[::-1]


def estimate_chords(chroma, beats, sr):
    """One absolute chord per beat, decoded as a path rather than per beat.

    Each beat is read from the middle of its span, so the moment where one
    chord hands over to the next does not contaminate either. Sevenths and
    suspensions must beat the best triad by a margin before they relabel it.
    """
    T, roots, quals, labels = build_templates()
    is_triad = np.array([q in ("maj", "min", "dim", "sus4") for q in quals])
    is_sus4 = np.array([q == "sus4" for q in quals])
    sims_rows = []
    kept = []
    for i, b in enumerate(beats):
        start = int(b)
        stop = int(beats[i + 1]) if i + 1 < len(beats) else chroma.shape[1]
        stop = max(start + 1, stop)
        span = stop - start
        # A beat_track grid runs late by a fraction of a beat, so a window that
        # spans [this beat, next beat] catches the following chord's attack.
        # A window centred on the beat, just under one beat wide, stays inside
        # one chord while still averaging enough frames to be stable.
        half = max(1, int(span * CHORD_WINDOW))
        seg = chroma[:, max(0, start - half):min(chroma.shape[1], start + half)]
        if seg.size == 0:
            continue
        v = seg.mean(axis=1)
        v = v / (np.linalg.norm(v) + 1e-9)
        sims = T.T @ v - SUS4_PENALTY * is_sus4
        triad_best = int(np.argmax(np.where(is_triad, sims, -1)))
        for k in np.where(~is_triad)[0]:
            if sims[triad_best] > sims[k] - 0.04:
                sims[k] = -np.inf
        sims_rows.append(sims)
        kept.append(i)

    picks = viterbi_chords(np.stack(sims_rows)) if sims_rows else []

    out = []
    i = 0
    while i < len(picks):
        j = i
        while j + 1 < len(picks) and picks[j + 1] == picks[i]:
            j += 1
        beat = kept[i]
        out.append({
            "t": round(float(librosa.frames_to_time(beats[beat], sr=sr)), 2),
            "beat": beat + 1,
            "root": NOTES[roots[picks[i]]],
            "quality": quals[picks[i]],
            "chord": labels[picks[i]],
            "beats": int(j - i + 1),
        })
        i = j + 1
    return out, int(len(kept))


def annotate_romans(chords, tonic, mode):
    tonic_idx = NOTES.index(tonic)
    scale, numerals = MODES[mode], ROMAN[mode]
    for c in chords:
        degree = (NOTES.index(c["root"]) - tonic_idx) % 12
        if degree not in scale:
            c["roman"] = "–"
            continue
        roman = numerals[scale.index(degree)]
        q = c["quality"]
        if q == "7" or q == "maj7":
            roman += "7" if q == "7" else "maj7"
        elif q == "min7":
            roman += "7"
        elif q == "sus4":
            roman += "sus4"
        c["roman"] = roman
    return chords


def home_score(chords, total_beats, tonic, mode="ionian"):
    """How much the track behaves as if `tonic` is home: time spent on that
    root, plus credit for opening and closing there. Chords on the tonic are
    only counted in full when their third agrees with the candidate mode —
    without this the chroma profile alone cannot separate A# minor from
    A# major when the bass keeps sounding the root."""
    if not chords or not total_beats:
        return 0.0
    want_minor = mode not in MAJOR_FAMILY
    on = 0.0
    for c in chords:
        if c["root"] != tonic:
            continue
        if c["quality"] == "sus4":
            agree = 0.6
        else:
            c_minor = c["quality"] in ("min", "min7", "dim")
            agree = 1.0 if c_minor == want_minor else 0.3
        on += c["beats"] * agree
    on /= total_beats
    bonus = (0.35 if chords[0]["root"] == tonic else 0.0) + \
            (0.25 if chords[-1]["root"] == tonic else 0.0)
    return min(1.0, on + bonus)


def third_verdict(chroma_mean, tonic):
    """Which third the audio actually sounds. A mode is decided by its third
    far more than by profile correlation, and the third is often too quiet in
    a full mix for the correlation to notice it."""
    t = NOTES.index(tonic)
    minor_third = float(chroma_mean[(t + 3) % 12])
    major_third = float(chroma_mean[(t + 4) % 12])
    if minor_third > major_third * 1.15:
        return True, minor_third, major_third
    if major_third > minor_third * 1.15:
        return False, minor_third, major_third
    return None, minor_third, major_third


def choose_key(candidates, chords, total_beats, chroma_mean=None):
    scored = []
    for cand in candidates:
        total = cand["score"] + 0.20 * home_score(chords, total_beats, cand["tonic"], cand["mode"])
        # Modal labels are over-predicted: a mode has to earn its name.
        if cand["mode"] not in ("ionian", "aeolian"):
            total -= 0.06
        scored.append({**cand, "total": round(total, 4)})
    scored.sort(key=lambda c: -c["total"])

    # A loop that opens on the relative minor's tonic is heard as minor even
    # when the major profile scores higher. Report both; prefer the minor.
    top = scored[0]

    if chroma_mean is not None:
        wants_minor, m3, M3 = third_verdict(chroma_mean, top["tonic"])
        is_minor = top["mode"] not in MAJOR_FAMILY
        if wants_minor is not None and wants_minor != is_minor:
            family = ("aeolian", "dorian", "phrygian") if wants_minor else tuple(MAJOR_FAMILY)
            rival = next((c for c in scored
                          if c["tonic"] == top["tonic"] and c["mode"] in family), None)
            if rival and top["total"] - rival["total"] < 0.30:
                rival["mode_from_third"] = {"minor": round(m3, 3), "major": round(M3, 3)}
                top = rival

    if top["mode"] in ("ionian", "lydian", "mixolydian") and chords:
        tonic_idx = NOTES.index(top["tonic"])
        minor_tonic = NOTES[(tonic_idx + 9) % 12]
        minor = next((c for c in scored if c["tonic"] == minor_tonic and c["mode"] == "aeolian"), None)
        if minor and top["total"] - minor["total"] < 0.20 and chords[0]["root"] == minor_tonic:
            minor["relative_of"] = top["label"]
            top["relative_ambiguity"] = True
            return minor, scored
    return top, scored


def estimate_structure(y, sr):
    cens = librosa.feature.chroma_cens(y=y, sr=sr)
    fps = 512.0 / sr
    block = max(1, int(round(4.0 / fps)))
    blocks = [cens[:, s:s + block].mean(axis=1)
              for s in range(0, cens.shape[1] - block + 1, block)]
    if len(blocks) < 3:
        return {"sections": []}
    feats = np.stack(blocks)
    novelty = np.linalg.norm(np.diff(feats, axis=0), axis=1)
    threshold = novelty.mean() + novelty.std()
    cut_blocks = [0] + [i + 1 for i, v in enumerate(novelty) if v > threshold] + [len(blocks)]

    sections, letter_of = [], {}
    for start_block, end_block in zip(cut_blocks, cut_blocks[1:]):
        centroid = feats[start_block:end_block].mean(axis=0)
        best_letter, best_sim = None, -1.0
        for letter, ref in letter_of.items():
            sim = pearson(centroid, ref)
            if sim > best_sim:
                best_letter, best_sim = letter, sim
        if best_letter is None or best_sim < 0.75:
            best_letter = chr(ord("A") + len(letter_of))
            letter_of[best_letter] = centroid
        sections.append({
            "label": best_letter,
            "start": round(start_block * block * fps, 1),
            "end": round(end_block * block * fps, 1),
        })
    return {"sections": sections}


def timbre(y, sr):
    harmonic, percussive = librosa.effects.hpss(y)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    flat = librosa.feature.spectral_flatness(y=y)[0]
    rms = librosa.feature.rms(y=y)[0]
    p_energy = float(np.sum(percussive ** 2))
    h_energy = float(np.sum(harmonic ** 2))
    return {
        "brightness_hz": int(np.median(cent)),
        "flatness": round(float(np.median(flat)), 4),
        "percussive_ratio": round(p_energy / (p_energy + h_energy + 1e-9), 3),
        "rms_p10_db": round(float(20 * np.log10(np.percentile(rms, 10) + 1e-9)), 2),
        "rms_p90_db": round(float(20 * np.log10(np.percentile(rms, 90) + 1e-9)), 2),
    }


def analyse(path, start=0.0, seconds=90.0, tonic_hint=None):
    y, sr = librosa.load(path, sr=22050, mono=True, offset=start, duration=seconds)
    if y.size < sr // 2:
        raise SystemExit("clip shorter than 0.5 s")
    y, _ = librosa.effects.trim(y, top_db=40)
    y_harm = librosa.effects.harmonic(y)

    chroma = librosa.feature.chroma_stft(y=y_harm, sr=sr, n_fft=8192)
    chroma_mean = (chroma / (chroma.sum(axis=0, keepdims=True) + 1e-9)).mean(axis=1)

    timing, beat_frames, onset_env = estimate_tempo(y, sr)
    meter = estimate_meter(onset_env, beat_frames)
    chords, total_beats = estimate_chords(chroma, beat_frames, sr)

    candidates = key_candidates(chroma_mean, top=8)
    if tonic_hint:
        candidates = [c for c in candidates if c["tonic"] == tonic_hint.upper()] or candidates
    ranked = choose_key(candidates, chords, total_beats, chroma_mean)
    top, ranked = ranked
    annotate_romans(chords, top["tonic"], top["mode"])

    tie = next((c for c in ranked[1:] if c["tonic"] != top["tonic"]), None)
    ambiguity = top["total"] - (tie["total"] if tie else 1.0)

    return {
        "file": path,
        "analysed_seconds": round(len(y) / sr, 2),
        "sample_rate": sr,
        "tempo": timing,
        "meter": meter,
        "key": {
            "tonic": top["tonic"],
            "mode": top["mode"],
            "scale": MODE_PLAIN.get(top["mode"], top["mode"]),
            "label": top["label"],
            "confidence": top["score"],
            "score_with_home": top["total"],
            "relative": tie,
            "alternatives": ranked[1:5],
            "scale_degrees": [NOTES[(NOTES.index(top["tonic"]) + d) % 12] for d in MODES[top["mode"]]],
            "ambiguous": bool(ambiguity < 0.05),
        },
        "harmony": {
            "progression": chords,
            "unique_chords": len({c["chord"] for c in chords}),
            "changes_per_bar": round(len(chords) / max(1.0, total_beats / meter["meter"]), 2),
        },
        "structure": estimate_structure(y, sr),
        "timbre": timbre(y, sr),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--tonic", help="force the tonic pitch class, e.g. C or F#")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()
    r = analyse(args.file, args.start, args.seconds, args.tonic)
    if args.pretty:
        k, t = r["key"], r["tempo"]
        print(f"{r['file']}  ({r['analysed_seconds']}s)")
        print(f"  {t['bpm']:.1f} BPM · {r['meter']['meter']}/4 · {k['label']} "
              f"(conf {k['confidence']:.2f}{', ambiguous' if k['ambiguous'] else ''})")
        print(f"  scale: {' '.join(k['scale_degrees'])}")
        print("  chords: " + " | ".join(f"{c['chord']}({c['roman']})" for c in r["harmony"]["progression"][:16]))
        print("  structure: " + " ".join(f"{s['label']}@{s['start']:.0f}s" for s in r["structure"]["sections"]))
    else:
        json.dump(r, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
