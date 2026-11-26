#!/usr/bin/env python3
"""Score a track against the Nightshift taste model.

Usage:
    python score.py --theory theory.json [--drums drums.json] [--loudness -12.4] [--pretty]

Reads the JSON produced by theory.py and drums.py, compares every measurable
axis with the targets in profile.json, and returns per-axis scores, a weighted
overall score, ranked fixes and a written verdict.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_PROFILE = os.path.join(HERE, "profile.json")
PROFILE_PATH = LOCAL_PROFILE if os.path.exists(LOCAL_PROFILE) else os.path.join(HERE, os.pardir, "profile.json")

AXIS_LABELS = {
    "groove": "Groove",
    "harmony": "Harmony",
    "sound": "Sound",
    "dynamics": "Dynamics",
    "structure": "Structure",
    "mix": "Mix",
}


def load_profile(path=PROFILE_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def range_score(value, target):
    """1.0 inside the window, tapering off by one window-width either side."""
    lo, hi = target
    width = max(1e-9, hi - lo)
    if lo <= value <= hi:
        return 1.0
    dist = lo - value if value < lo else value - hi
    return clamp(1.0 - dist / width)


def floor_score(value, minimum):
    if minimum <= 0:
        return 1.0
    return clamp(value / minimum)


def metric(name, value, target, score, unit="", kind="range"):
    return {
        "metric": name,
        "value": round(float(value), 2),
        "target": target,
        "score": round(score, 3),
        "unit": unit,
        "kind": kind,
        "status": "in" if score >= 0.995 else ("near" if score >= 0.6 else "out"),
    }


def gather(theory, drums, loudness):
    k = theory["key"]
    h = theory["harmony"]
    tb = theory["timbre"]
    sections = theory.get("structure", {}).get("sections", [])
    lengths = [s["end"] - s["start"] for s in sections] or [theory.get("analysed_seconds", 0)]
    longest = max(lengths)
    shortest = min(lengths)
    dynamic_range = tb["rms_p90_db"] - tb["rms_p10_db"]

    feel = drums.get("feel", {}) if drums else {}
    instruments = drums.get("instruments", {}) if drums else {}
    hats = instruments.get("hat", {}).get("per_bar")
    if hats is None:
        hats = sum(instruments.get(i, {}).get("per_bar", 0)
                   for i in ("hat", "openhat", "ride"))

    bpm = drums.get("bpm") if drums and "bpm" in drums else theory["tempo"]["bpm"]
    if loudness is None:
        loudness = tb["rms_p90_db"] - 2.0

    return {
        "bpm": bpm,
        "offbeat_ratio": feel.get("offbeat_ratio", 0.0),
        "hats_per_bar": hats or 0.0,
        "timing_spread_ms": feel.get("timing_spread_ms", 0.0),
        "chord_changes_per_bar": h["changes_per_bar"],
        "unique_chords": h["unique_chords"],
        "key_confidence": k["confidence"],
        "brightness_hz": tb["brightness_hz"],
        "percussive_ratio": tb["percussive_ratio"],
        "dynamic_range_db": dynamic_range,
        "loudness_db": loudness,
        "section_seconds": longest,
        "shortest_section": shortest,
        "flatness": tb["flatness"],
        "drum_hits": drums.get("onsets") if drums else 0,
    }


def axis_groove(m, p):
    t = p["targets"]
    return [
        metric("Tempo", m["bpm"], t["bpm"], range_score(m["bpm"], t["bpm"]), "BPM"),
        metric("Off-beat hits", m["offbeat_ratio"], t["offbeat_ratio"],
               range_score(m["offbeat_ratio"], t["offbeat_ratio"]), "", "ratio"),
        metric("Hats per bar", m["hats_per_bar"], t["hats_per_bar"],
               range_score(m["hats_per_bar"], t["hats_per_bar"]), "", "count"),
        metric("Timing spread", m["timing_spread_ms"], t["timing_spread_ms"],
               range_score(m["timing_spread_ms"], t["timing_spread_ms"]), "ms"),
    ]


def axis_harmony(m, p):
    t = p["targets"]
    return [
        metric("Chords per bar", m["chord_changes_per_bar"], t["chord_changes_per_bar"],
               range_score(m["chord_changes_per_bar"], t["chord_changes_per_bar"]), "", "count"),
        metric("Unique chords", m["unique_chords"], t["unique_chords_min"],
               floor_score(m["unique_chords"], t["unique_chords_min"]), "", "floor"),
        metric("Key certainty", m["key_confidence"], [0.45, 1.0],
               range_score(m["key_confidence"], [0.45, 1.0]), "", "ratio"),
    ]


def axis_sound(m, p):
    t = p["targets"]
    return [
        metric("Brightness", m["brightness_hz"], t["brightness_hz"],
               range_score(m["brightness_hz"], t["brightness_hz"]), "Hz"),
        metric("Percussive share", m["percussive_ratio"], t["percussive_ratio"],
               range_score(m["percussive_ratio"], t["percussive_ratio"]), "", "ratio"),
    ]


def axis_dynamics(m, p):
    t = p["targets"]
    return [
        metric("Dynamic range", m["dynamic_range_db"], t["dynamic_range_db"],
               range_score(m["dynamic_range_db"], t["dynamic_range_db"]), "dB"),
    ]


def axis_structure(m, p):
    t = p["targets"]
    return [
        metric("Longest section", m["section_seconds"], t["section_seconds"],
               range_score(m["section_seconds"], t["section_seconds"]), "s"),
    ]


def axis_mix(m, p):
    t = p["targets"]
    return [
        metric("Loudness", m["loudness_db"], t["loudness_db"],
               range_score(m["loudness_db"], t["loudness_db"]), "dBFS"),
        metric("Spectral flatness", m["flatness"], [0.0, 0.16],
               range_score(m["flatness"], [0.0, 0.16]), "", "ratio"),
    ]


AXES = {
    "groove": axis_groove,
    "harmony": axis_harmony,
    "sound": axis_sound,
    "dynamics": axis_dynamics,
    "structure": axis_structure,
    "mix": axis_mix,
}

FIX_HINTS = {
    "Tempo": "Land inside the nocturnal pocket — either pull the tempo down toward {target} BPM or make the track's case for a faster feel with half-time drums.",
    "Off-beat hits": "Move some weight off the downbeat: syncopate the kick or push the hats into the off-beat so the groove leans.",
    "Hats per bar": "Thin or thicken the hat layer — {target} per bar is the range this taste likes. Too many turns it into a wash, too few loses the pulse.",
    "Timing spread": "Tighten the playing. A spread of {target} ms is human; wider than that reads as sloppy rather than loose.",
    "Chords per bar": "Adjust harmonic rhythm — either let chords breathe for {target} bars or add movement where it is static now.",
    "Unique chords": "The progression is too static. Introduce at least {target} distinct chords, even if only as passing colour.",
    "Key certainty": "The tonal centre is muddy. Simplify the arrangement during the key-defining moments so the tonic is unambiguous.",
    "Brightness": "Rebalance the spectrum toward {target} Hz — the track is either dulled or brittle against this taste.",
    "Percussive share": "Shift the harmonic/percussive balance so percussion holds around {target} of the energy.",
    "Dynamic range": "Restore contrast: leave quiet passages quiet and let the loud ones actually peak. Aim for {target} dB of range.",
    "Longest section": "Break the long stretch up — sections here want to run about {target} seconds before something changes.",
    "Loudness": "Ride the level into {target} dBFS. Too hot flattens the life out of it; too quiet and it never arrives.",
    "Spectral flatness": "The mix is noisy or over-saturated. Clean low-level fizz so the tonal body reads clearly.",
}


def fmt_target(target):
    if isinstance(target, list):
        if len(target) == 2:
            return f"{target[0]:g}–{target[1]:g}"
        return ", ".join(f"{t:g}" for t in target)
    return f"{target:g}"


def score(theory, drums=None, loudness=None, profile=None):
    p = profile or load_profile()
    m = gather(theory, drums or {}, loudness)

    axes, fixes = [], []
    for key, weight in sorted(p["weights"].items(), key=lambda kv: -kv[1]):
        metrics = AXES[key](m, p)
        value = 100.0 * sum(x["score"] for x in metrics) / len(metrics)
        axes.append({
            "key": key,
            "label": AXIS_LABELS[key],
            "weight": weight,
            "score": round(value, 1),
            "metrics": metrics,
        })
        for x in metrics:
            lost = (1.0 - x["score"]) * weight * 100.0
            if x["score"] >= 0.85:
                continue
            hint = FIX_HINTS.get(x["metric"], "Nudge this toward the target.")
            fixes.append({
                "axis": key,
                "metric": x["metric"],
                "impact": round(lost, 1),
                "issue": f"{x['metric']} is {x['value']:g}{(' ' + x['unit']) if x['unit'] else ''}, "
                         f"outside the {fmt_target(x['target'])}{(' ' + x['unit']) if x['unit'] else ''} window.",
                "action": hint.format(target=fmt_target(x["target"])),
            })

    overall = sum(a["score"] * a["weight"] for a in axes) / sum(a["weight"] for a in axes)
    fixes.sort(key=lambda f: -f["impact"])

    strengths = [f"{a['label']} ({a['score']:.0f})" for a in sorted(axes, key=lambda x: -x["score"]) if a["score"] >= 75][:3]
    weak = [a for a in axes if a["score"] < 55]

    k = theory["key"]
    verdict = build_verdict(overall, k, m, strengths, weak, fixes, mood_of(k["mode"]))

    return {
        "persona": p["persona"],
        "overall": round(overall, 1),
        "grade": grade(overall),
        "summary": summary_line(overall, k, m),
        "axes": axes,
        "metrics": m,
        "strengths": strengths,
        "weaknesses": [f"{a['label']} ({a['score']:.0f})" for a in weak],
        "fixes": fixes[:6],
        "verdict": verdict,
    }


def grade(v):
    if v >= 85:
        return "A"
    if v >= 75:
        return "B"
    if v >= 65:
        return "C"
    if v >= 55:
        return "D"
    return "E"


def summary_line(overall, key, m):
    return (f"{overall:.0f}/100 · {key['label']} · {m['bpm']:.0f} BPM · "
            f"brightness {m['brightness_hz']} Hz")


MOODS = {
    "ionian": ("a major-key lift", True),
    "lydian": ("a bright, floating mode", True),
    "mixolydian": ("a bluesy major mode", True),
    "aeolian": ("a minor-key nocturne", False),
    "dorian": ("a minor mode with a raised sixth", False),
    "phrygian": ("a dark phrygian cast", False),
    "locrian": ("a rootless haze", False),
}


def mood_of(mode):
    return MOODS.get(mode, ("an unusual mode", False))


def build_verdict(overall, key, m, strengths, weak, fixes, mood):
    openers = {
        85: "This one lands almost exactly where Nightshift lives.",
        75: "Solidly in the pocket — a few edges still need filing.",
        65: "The bones are right but the finish is loose.",
        55: "There is a track in here, but it is fighting the mix.",
        0: "This does not read as Nightshift's room at all.",
    }
    opener = next(v for k, v in sorted(openers.items(), reverse=True) if overall >= k)

    fits, is_major = mood
    stance = "that suits the after-midnight brief" if (not is_major and "haze" not in fits) \
        else "that works against the after-midnight brief"
    parts = [opener,
             f"It sits in {key['label']}, {fits} {stance}, "
             f"with confidence {key['confidence']:.2f}."]
    if strengths:
        parts.append("What works: " + ", ".join(strengths).lower() + ".")
    if weak:
        parts.append("What does not: " + ", ".join(a["label"].lower() for a in weak) + ".")
    if fixes:
        top = fixes[0]
        parts.append(f"If you change one thing, change the {top['metric'].lower()} — {top['action']}")
    else:
        parts.append("Nothing here needs rescuing; leave it alone and let it breathe.")
    return " ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--theory", required=True)
    ap.add_argument("--drums")
    ap.add_argument("--loudness", type=float)
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    with open(args.theory, "r", encoding="utf-8") as fh:
        theory = json.load(fh)
    drums = None
    if args.drums:
        with open(args.drums, "r", encoding="utf-8") as fh:
            drums = json.load(fh)

    result = score(theory, drums, args.loudness)
    if args.pretty:
        print(f"{result['persona']} · {result['grade']} · {result['summary']}")
        for a in sorted(result["axes"], key=lambda x: -x["score"]):
            bar = "█" * int(a["score"] / 5)
            print(f"  {a['label']:<10} {a['score']:>5.1f} {bar}")
        print("\n" + result["verdict"])
        for f in result["fixes"]:
            print(f"  - [{f['axis']}] {f['issue']}  → {f['action']}")
    else:
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
