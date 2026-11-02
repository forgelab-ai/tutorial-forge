"""tutorial-forge: turn a long screen recording plus a voiceover into a tight, YouTube-ready tutorial.

You describe the edit as a short JSON plan: sections, each with a voiceover and the source ranges to show.
The tool fits the footage to the voice (speeding up waits and marking them), punches in on dialogs,
adds step labels and a hook title, grades the picture, levels the audio and writes YouTube chapters.

    python forge.py render plan.json           # build the video and chapters.txt
    python forge.py split-voice voice.wav 10   # cut one long voice take into 10 section files at the pauses
    python forge.py preview video.mp4          # contact sheet of the result (preview.jpg next to it)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

GRADE = "eq=contrast=1.06:brightness=0.02:saturation=1.12,unsharp=5:5:0.6"
ACCENT = (0, 120, 212)
BADGE_FROM = 3.0         # show the "Nx FASTER" badge at this speed or above
SECTION_PAD = 0.5        # breathing room after each voice block, in seconds
FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def tool(name):
    """ffmpeg/ffprobe from FFMPEG_DIR, PATH, or winget's link folder (new installs are not on PATH until restart)."""
    for d in (os.environ.get("FFMPEG_DIR"), None,
              Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"):
        p = shutil.which(name, path=str(d)) if d else shutil.which(name)
        if p:
            return p
    sys.exit(f"{name} not found: install ffmpeg or set FFMPEG_DIR")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(map(str, cmd[:6]))} ...\n{r.stderr[-1500:]}")
    return r


def duration(path):
    out = run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.stdout.strip())


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


# ------------------------------------------------------------------ overlays (transparent full-frame PNGs)
def _canvas(w, h):
    img = Image.new("RGBA", (w, h))
    return img, ImageDraw.Draw(img)


def step_label(path, w, h, title, step=None):
    """Dark rounded label, top left: small STEP line over the step title."""
    img, d = _canvas(w, h)
    s = h / 1080
    f1, f2 = font(round(34 * s)), font(round(52 * s))
    x, y = round(60 * s), round(52 * s)
    bw = max(d.textlength(step or "", font=f1), d.textlength(title, font=f2)) + 64 * s
    bh = (130 if step else 96) * s
    d.rounded_rectangle([x, y, x + bw, y + bh], radius=22 * s, fill=(15, 20, 30, 215))
    d.rectangle([x, y + 18 * s, x + 8 * s, y + bh - 18 * s], fill=ACCENT + (255,))
    if step:
        d.text((x + 32 * s, y + 14 * s), step, font=f1, fill=(140, 200, 255))
    d.text((x + 32 * s, y + (58 if step else 18) * s), title, font=f2, fill="white")
    img.save(path)


def speed_badge(path, w, h, speed):
    img, d = _canvas(w, h)
    s = h / 1080
    f = font(round(44 * s))
    text = f"{speed:.0f}x FASTER"
    tw = d.textlength(text, font=f)
    x, y = w - tw - 120 * s, 60 * s
    d.rounded_rectangle([x, y, x + tw + 60 * s, y + 84 * s], radius=42 * s, fill=(255, 196, 0, 235))
    d.text((x + 30 * s, y + 12 * s), text, font=f, fill=(20, 20, 20))
    img.save(path)


def headline(path, w, h, big, small=None):
    """Big centred hook title in the lower third."""
    img, d = _canvas(w, h)
    s = h / 1080
    for txt, f, y, col in [(big, font(round(96 * s)), 0.70, "white"), (small, font(round(48 * s)), 0.82, (255, 210, 60))]:
        if txt:
            tw = d.textlength(txt, font=f)
            d.text(((w - tw) / 2, h * y), txt, font=f, fill=col, stroke_width=round(6 * s), stroke_fill=(0, 0, 0))
    img.save(path)


# ------------------------------------------------------------------ voice
def tts(text, out):
    """Placeholder voice with Windows' built-in speech, so a plan can be previewed before recording."""
    if os.name != "nt":
        sys.exit("voice_text needs Windows speech; record the voice and use 'voice' instead")
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SetOutputToWaveFile('{out}'); $s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text, text=True, capture_output=True)
    if r.returncode != 0 or not os.path.exists(out):
        sys.exit(f"text-to-speech failed: {r.stderr[-500:]}")


def split_voice(voice, sections, out_dir=None, noise="-35dB", min_gap=1.0):
    """Cut one long take into `sections` files at its longest pauses."""
    log = run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(voice), "-af",
               f"silencedetect=n={noise}:d={min_gap}", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    total = duration(voice)
    gaps = [(e - s, (s + e) / 2) for s, e in zip(starts, ends) if 0.5 < s and e < total - 0.5]
    if len(gaps) < sections - 1:
        sys.exit(f"found only {len(gaps)} pauses for {sections} sections: lower min_gap or check the take")
    cuts = sorted(mid for _, mid in sorted(gaps, reverse=True)[:sections - 1])
    bounds = [0.0] + cuts + [total]
    out_dir = Path(out_dir or Path(voice).with_suffix(""))
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        dst = out_dir / f"voice-{i:02d}.wav"
        # silenceremove trims the pause at both ends so each block starts on the first word
        run([tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(voice), "-af",
             f"silenceremove=start_periods=1:start_threshold={noise},areverse,"
             f"silenceremove=start_periods=1:start_threshold={noise},areverse", str(dst)])
        print(f"{dst.name}: {a:7.2f}s - {b:7.2f}s  ({duration(dst):.1f}s)")
    return out_dir


# ------------------------------------------------------------------ video
def fit_speeds(clips, target):
    """Speeds so the section's clips fill `target` seconds. Fixed speeds stay; the rest share what is left."""
    fixed = sum((c["end"] - c["start"]) / c["speed"] for c in clips if c.get("speed"))
    free = sum(c["end"] - c["start"] for c in clips if not c.get("speed"))
    shared = free / max(target - fixed, 0.5) if free else 1.0
    return [c.get("speed") or max(shared, 1.0) for c in clips]


def render_clip(cfg, clip, speed, out, overlays, fade_in, hold):
    """One graded clip at the output size: speed change, eased punch-in, PNG overlays, optional end hold."""
    w, h, fps = cfg["width"], cfg["height"], cfg["fps"]
    vf = [f"setpts=PTS/{speed}", f"scale={w}:{h}:flags=lanczos", GRADE]
    if clip.get("zoom"):
        z, cx, cy = clip["zoom"]
        r = 1.0  # seconds to ease in
        e = f"min(t/{r},1)"
        zt = f"(1+({z}-1)*{e}*{e}*(3-2*{e}))"
        vf.append(f"scale=w='{w}*{zt}':h='{h}*{zt}':eval=frame:flags=bicubic,"
                  f"crop={w}:{h}:x='(iw-{w})*{cx}':y='(ih-{h})*{cy}'")
    if hold > 0:
        vf.append(f"tpad=stop_mode=clone:stop_duration={hold:.3f}")
    if fade_in:
        vf.append("fade=t=in:d=0.25")
    length = (clip["end"] - clip["start"]) / speed + max(hold, 0)
    cmd = [tool("ffmpeg"), "-v", "error", "-y", "-ss", str(clip["start"]), "-t", str(clip["end"] - clip["start"]),
           "-i", cfg["source"]]
    for png, _, _ in overlays:
        cmd += ["-loop", "1", "-i", png]
    graph, last = f"[0:v]{','.join(vf)},fps={fps}[v0]", "v0"
    for i, (_, t0, t1) in enumerate(overlays, 1):
        graph += (f";[{i}:v]format=rgba,fade=t=in:st={t0}:d=0.3:alpha=1[o{i}]"
                  f";[{last}][o{i}]overlay=0:0:enable='between(t,{t0},{t1})':shortest=1[v{i}]")
        last = f"v{i}"
    run(cmd + ["-filter_complex", graph, "-map", f"[{last}]", "-an", "-t", f"{length:.3f}", "-c:v", "libx264",
               "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", out])
    return length


def stamp(t):
    t = int(round(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def render(plan_path):
    plan_path = Path(plan_path)
    cfg = {"width": 1920, "height": 1080, "fps": 30, **json.loads(plan_path.read_text(encoding="utf-8"))}
    base = plan_path.parent
    cfg["source"] = str((base / cfg["source"]).resolve())
    out = (base / cfg["output"]).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = cfg["width"], cfg["height"]
    work = Path(tempfile.mkdtemp(prefix="forge-", dir=out.parent))
    parts, voices, chapters, t = [], [], [], 0.0

    for si, sec in enumerate(cfg["sections"], 1):
        voice = work / f"voice-{si:02d}.wav"
        if sec.get("voice"):
            shutil.copy(base / sec["voice"], voice)
        elif sec.get("voice_text"):
            tts(sec["voice_text"], str(voice))
        else:
            sys.exit(f"section {si} needs 'voice' (a recording) or 'voice_text'")
        sec_len = duration(voice) + SECTION_PAD
        voices.append((voice, sec_len))
        if sec.get("title"):
            chapters.append((t, sec["title"]))

        label = None
        if sec.get("title"):
            label = str(work / f"label-{si}.png")
            step_label(label, w, h, sec["title"], sec.get("step"))
        hook = None
        if sec.get("headline"):
            hook = str(work / f"hook-{si}.png")
            headline(hook, w, h, sec["headline"], sec.get("subline"))

        clips = sec["clips"]
        speeds = fit_speeds(clips, sec_len)
        natural = sum((c["end"] - c["start"]) / s for c, s in zip(clips, speeds))
        if natural > sec_len:   # fixed speeds overshoot the voice: shorten every clip by the same share
            k = sec_len / natural
            clips = [{**c, "end": c["start"] + (c["end"] - c["start"]) * k} for c in clips]
            natural = sec_len
        for ci, (clip, speed) in enumerate(zip(clips, speeds)):
            clen = (clip["end"] - clip["start"]) / speed
            last = ci == len(clips) - 1
            hold = sec_len - natural if last and natural < sec_len else 0.0
            ov = []
            if label:
                ov.append((label, 0.1 if ci == 0 else 0, clen + hold))
            if hook and ci == 0:
                ov.append((hook, 0, clen))
            if speed >= BADGE_FROM:
                badge = str(work / f"badge-{si}-{ci}.png")
                speed_badge(badge, w, h, speed)
                ov.append((badge, 0.2, clen))
            name = str(work / f"part-{si:02d}-{ci:02d}.mp4")
            render_clip(cfg, clip, speed, name, ov, fade_in=(ci == 0 and si > 1), hold=hold)
            parts.append(name)
        t += sec_len
        print(f"section {si}: {sec_len:.1f}s, speeds {', '.join(f'{s:.1f}x' for s in speeds)}")

    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p in parts), encoding="utf-8")
    video = work / "video.mp4"
    run([tool("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)])

    # Voice: every block padded to its section, 30 ms fades against pops, levelled to YouTube's -14 LUFS.
    cmd = [tool("ffmpeg"), "-v", "error", "-y"]
    for v, _ in voices:
        cmd += ["-i", str(v)]
    graph = ";".join(f"[{i}:a]aresample=48000,afade=t=in:d=0.03,apad=whole_dur={d:.3f},"
                     f"afade=t=out:st={d - 0.03:.3f}:d=0.03[a{i}]" for i, (_, d) in enumerate(voices))
    graph += ";" + "".join(f"[a{i}]" for i in range(len(voices)))
    graph += f"concat=n={len(voices)}:v=0:a=1,highpass=f=80,loudnorm=I=-14:TP=-1.5:LRA=7[a]"
    run(cmd + ["-i", str(video), "-filter_complex", graph, "-map", f"{len(voices)}:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])

    if chapters:
        if chapters[0][0] > 0:
            chapters.insert(0, (0, "Intro"))
        out.with_name(out.stem + "-chapters.txt").write_text(
            "\n".join(f"{stamp(a)} {b}" for a, b in chapters) + "\n", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"done: {out} ({duration(out):.1f}s)")
    return out


def preview(video, cols=6, rows=3):
    video = Path(video)
    n = cols * rows
    every = duration(video) / n
    out = video.with_name(video.stem + "-preview.jpg")
    run([tool("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-vf",
         f"fps=1/{every:.3f},scale=384:-1,tile={cols}x{rows}", "-frames:v", "1", str(out)])
    print(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="build the video from a JSON plan").add_argument("plan")
    sv = sub.add_parser("split-voice", help="cut one voice take into per-section files")
    sv.add_argument("voice")
    sv.add_argument("sections", type=int)
    sv.add_argument("--out")
    sv.add_argument("--noise", default="-35dB")
    sv.add_argument("--min-gap", type=float, default=1.0)
    sub.add_parser("preview", help="contact sheet of a video").add_argument("video")
    a = ap.parse_args()
    if a.cmd == "render":
        render(a.plan)
    elif a.cmd == "split-voice":
        split_voice(a.voice, a.sections, a.out, a.noise, a.min_gap)
    else:
        preview(a.video)


if __name__ == "__main__":
    main()
