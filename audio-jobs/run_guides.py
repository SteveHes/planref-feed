#!/usr/bin/env python3
"""PlanRef full-guide audio production. Runs on GitHub Actions.

For each guide slug in request.json:
  - loads audio-jobs/scripts/<slug>.json (sections -> paras)
  - chunks text at paragraph boundaries (never across sections), ~4200 chars max
  - generates each chunk via ElevenLabs Turbo v2.5 with prosody continuity
    (previous_text / next_text)
  - concatenates chunks with a short silence at section boundaries
  - masters once: warm EQ + pitch 0.97 + loudness normalisation (the approved
    "C tone pitch" chain)
  - uploads <slug>.mp3 to R2 and records duration metadata

Secrets via env: XI_KEY, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error

BASE = "https://api.elevenlabs.io"
KEY = os.environ.get("XI_KEY", "")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
WORK = os.path.join(HERE, "work")
os.makedirs(OUT, exist_ok=True)
os.makedirs(WORK, exist_ok=True)

VOICE_SETTINGS = {"stability": 0.48, "similarity_boost": 0.65, "style": 0.08,
                  "use_speaker_boost": False, "speed": 1.0}
MODEL = "eleven_turbo_v2_5"
CHUNK_LIMIT = 4200
SECTION_GAP_S = 0.8
PITCH = 0.97

result = {"status": "started", "guides": {}, "log": []}

def log(msg):
    result["log"].append(msg)
    print(msg, flush=True)

def save_result():
    with open(os.path.join(OUT, "result.json"), "w") as f:
        json.dump(result, f, indent=1)

def api(path, data=None, raw=False, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers={"xi-api-key": KEY})
            if data is not None:
                req.add_header("Content-Type", "application/json")
                req.data = json.dumps(data).encode()
            with urllib.request.urlopen(req, timeout=300) as r:
                body = r.read()
            return body if raw else json.loads(body)
        except Exception as e:
            if attempt == tries - 1:
                raise
            log(f"retry {attempt+1} after error: {str(e)[:200]}")
            time.sleep(8 * (attempt + 1))

def get_voice_id(name):
    voices = api("/v1/voices")["voices"]
    for v in voices:
        if v["name"].strip().lower() == name.strip().lower():
            return v["voice_id"]
    raise RuntimeError(f"voice '{name}' not found")

def chunk_guide(sections, intro, outro):
    """Returns list of (text, is_section_start) chunks."""
    chunks = []
    for si, sec in enumerate(sections):
        paras = list(sec["paras"])
        if si == 0:
            paras[0] = intro + " " + paras[0]
        if si == len(sections) - 1:
            paras[-1] = paras[-1] + " " + outro
        cur = ""
        first_of_section = True
        for p in paras:
            if cur and len(cur) + len(p) + 1 > CHUNK_LIMIT:
                chunks.append((cur, first_of_section))
                first_of_section = False
                cur = p
            else:
                cur = (cur + "\n\n" + p) if cur else p
        if cur:
            chunks.append((cur, first_of_section))
    return chunks

def master(src, dst):
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    if "rubberband" in probe.stdout:
        pre = f"rubberband=pitch={PITCH},"
    else:
        pre = f"asetrate=44100*{PITCH},aresample=44100,atempo={1/PITCH:.6f},"
    af = (pre + "highpass=f=70,"
          "equalizer=f=180:t=q:w=1.0:g=3,"
          "equalizer=f=3300:t=q:w=1.3:g=-3,"
          "treble=g=-1.5:f=7500,"
          "loudnorm=I=-16:TP=-1.5:LRA=11")
    subprocess.run(["ffmpeg", "-y", "-i", src, "-af", af, "-ar", "44100",
                    "-codec:a", "libmp3lame", "-b:a", "128k", dst],
                   check=True, capture_output=True)

def duration_of(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())

def upload_r2(local, key):
    import boto3
    s3 = boto3.client("s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto")
    s3.upload_file(local, "planref-audio", key,
                   ExtraArgs={"ContentType": "audio/mpeg",
                              "CacheControl": "public, max-age=31536000"})

def build_guide(slug, title, voice_id, public_base):
    gdir = os.path.join(WORK, slug)
    os.makedirs(gdir, exist_ok=True)
    sections = json.load(open(os.path.join(HERE, "scripts", f"{slug}.json")))
    intro = f"You're listening to PlanRef. This is the full guide to {title}."
    outro = ("And that's the end of this guide. Thanks for listening. "
             "The written version, with every reference, is in PlanRef.")
    chunks = chunk_guide(sections, intro, outro)
    total_chars = sum(len(c) for c, _ in chunks)
    log(f"{slug}: {len(chunks)} chunks, {total_chars} chars")

    files = []
    for i, (text, sec_start) in enumerate(chunks):
        payload = {"text": text, "model_id": MODEL, "voice_settings": VOICE_SETTINGS}
        if i > 0:
            payload["previous_text"] = chunks[i-1][0][-300:]
        if i < len(chunks) - 1:
            payload["next_text"] = chunks[i+1][0][:300]
        audio = api(f"/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
                    data=payload, raw=True)
        cpath = os.path.join(gdir, f"c{i:03d}.mp3")
        with open(cpath, "wb") as f:
            f.write(audio)
        files.append((cpath, sec_start))
        log(f"{slug}: chunk {i+1}/{len(chunks)} done ({len(audio)} bytes)")

    # silence file for section gaps
    sil = os.path.join(gdir, "sil.mp3")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", str(SECTION_GAP_S), "-codec:a", "libmp3lame", "-b:a", "128k", sil],
                   check=True, capture_output=True)

    lst = os.path.join(gdir, "list.txt")
    with open(lst, "w") as f:
        for j, (cpath, sec_start) in enumerate(files):
            if j > 0 and sec_start:
                f.write(f"file '{sil}'\n")
            f.write(f"file '{cpath}'\n")

    joined = os.path.join(gdir, "joined.mp3")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-codec:a", "libmp3lame", "-b:a", "128k", joined],
                   check=True, capture_output=True)

    final = os.path.join(gdir, f"{slug}.mp3")
    master(joined, final)
    dur = duration_of(final)
    size = os.path.getsize(final)
    key = f"guides/{slug}.mp3"
    upload_r2(final, key)
    log(f"{slug}: uploaded {key} ({size} bytes, {dur:.0f}s)")
    return {"title": title, "url": f"{public_base}/{key}", "duration": round(dur),
            "bytes": size, "chars": total_chars}

def main():
    request = json.load(open(os.path.join(HERE, "request.json")))
    public_base = request.get("public_base", "https://audio.planref.co.uk")
    for var in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT"):
        if not os.environ.get(var):
            raise RuntimeError(f"{var} is not set — add the GitHub secret before running")
    probe = os.path.join(WORK, "probe.txt")
    open(probe, "w").write("planref audio probe")
    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
    s3.upload_file(probe, "planref-audio", "probe.txt")
    log("R2 probe upload ok")
    sub = api("/v1/user/subscription")
    result["credits_before"] = sub.get("character_count")
    voice_id = get_voice_id(request.get("voice_name", "Steve H"))
    for g in request["guides"]:
        try:
            result["guides"][g["slug"]] = build_guide(g["slug"], g["title"], voice_id, public_base)
        except Exception as e:
            msg = str(e)[:500]
            if isinstance(e, urllib.error.HTTPError):
                try: msg += " | " + e.read().decode()[:500]
                except Exception: pass
            result["guides"][g["slug"]] = {"error": msg}
            log(f"{slug if False else g['slug']}: FAILED {msg}")
        save_result()
    sub2 = api("/v1/user/subscription")
    result["credits_after"] = sub2.get("character_count")
    result["credits_spent"] = (result["credits_after"] or 0) - (result["credits_before"] or 0)
    result["status"] = "ok" if all("error" not in v for v in result["guides"].values()) else "partial"
    save_result()

if __name__ == "__main__":
    if not KEY:
        result.update(status="error", error="XI_KEY missing"); save_result(); sys.exit(0)
    try:
        main()
    except Exception as e:
        result.update(status="error", error=str(e)[:800]); save_result(); sys.exit(0)
