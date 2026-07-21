#!/usr/bin/env python3
"""PlanRef audio job runner. Runs on GitHub Actions.
Reads audio-jobs/request.json, talks to ElevenLabs, writes results to audio-jobs/out/.
The API key comes from the XI_KEY environment variable (GitHub secret) and is never written to output.
"""
import json, os, sys, subprocess, urllib.request, urllib.error

BASE = "https://api.elevenlabs.io"
KEY = os.environ.get("XI_KEY", "")
OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)

result = {"status": "started", "steps": []}

def api(path, data=None, headers=None, raw=False):
    h = {"xi-api-key": KEY}
    if headers: h.update(headers)
    req = urllib.request.Request(BASE + path, headers=h)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return body if raw else json.loads(body)

def fail(step, err):
    result["status"] = "error"
    result["failed_step"] = step
    result["error"] = str(err)[:2000]
    if isinstance(err, urllib.error.HTTPError):
        try:
            result["error_body"] = err.read().decode()[:2000]
        except Exception:
            pass
    write_result()
    sys.exit(0)  # exit 0 so the commit step still runs and reports the error

def write_result():
    with open(os.path.join(OUT, "result.json"), "w") as f:
        json.dump(result, f, indent=1)

if not KEY:
    fail("env", "XI_KEY secret is missing or empty")

req_path = os.path.join(os.path.dirname(__file__), "request.json")
request = json.load(open(req_path))

# 1. Subscription / credits
try:
    sub = api("/v1/user/subscription")
    result["subscription"] = {
        "tier": sub.get("tier"),
        "character_count": sub.get("character_count"),
        "character_limit": sub.get("character_limit"),
        "next_character_count_reset_unix": sub.get("next_character_count_reset_unix"),
    }
    result["steps"].append("subscription ok")
except Exception as e:
    result["subscription_error"] = str(e)[:500]
    result["steps"].append("subscription failed (continuing)")

# 2. Voices
try:
    voices = api("/v1/voices")["voices"]
    result["voices"] = [{"id": v["voice_id"], "name": v["name"], "category": v.get("category")} for v in voices]
    result["steps"].append("voices ok")
except Exception as e:
    fail("voices", e)

want = request.get("voice_name", "Steve H").strip().lower()
voice = next((v for v in voices if v["name"].strip().lower() == want), None)
if voice is None:
    voice = next((v for v in voices if want in v["name"].strip().lower()), None)
if voice is None:
    fail("voice_lookup", f"No voice matching '{request.get('voice_name')}' found. See voices list in result.json")
result["voice_used"] = {"id": voice["voice_id"], "name": voice["name"]}

# 2b. Models (optional)
if request.get("list_models"):
    try:
        models = api("/v1/models")
        result["models"] = [{"id": m.get("model_id"), "name": m.get("name"),
                             "cost_multiplier": m.get("cost_multiplier") or m.get("token_cost_factor")}
                            for m in models]
        result["steps"].append("models ok")
    except Exception as e:
        result["models_error"] = str(e)[:500]

def master(path):
    tmp = path + ".warm.mp3"
    af = ("highpass=f=60,"
          "equalizer=f=200:t=q:w=1.0:g=3,"
          "equalizer=f=3400:t=q:w=1.4:g=-3,"
          "equalizer=f=5800:t=q:w=2:g=-1.5,"
          "treble=g=-1:f=9000,"
          "acompressor=threshold=-22dB:ratio=2:attack=12:release=220:makeup=1.5,"
          "loudnorm=I=-16:TP=-1.5:LRA=9")
    subprocess.run(["ffmpeg", "-y", "-i", path, "-af", af, "-ar", "44100",
                    "-codec:a", "libmp3lame", "-b:a", "160k", tmp],
                   check=True, capture_output=True)
    os.replace(tmp, path)

# 3. Generate clips (one per item)
chars_before = result.get("subscription", {}).get("character_count")
for item in request.get("items", []):
    name = item["output"]
    model = item.get("model_id", "eleven_multilingual_v2")
    text = item["text"]
    try:
        audio = api(
            f"/v1/text-to-speech/{voice['voice_id']}?output_format={item.get('output_format','mp3_44100_128')}",
            data={"text": text, "model_id": model,
                  "voice_settings": item.get("voice_settings", {"stability": 0.5, "similarity_boost": 0.75})},
            raw=True,
        )
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(audio)
        if item.get("post") == "warm":
            master(os.path.join(OUT, name))
            result["steps"].append(f"generated+mastered {name} (model {model}, {len(text)} chars)")
        else:
            result["steps"].append(f"generated {name} ({len(audio)} bytes, model {model}, {len(text)} chars)")
    except Exception as e:
        msg = str(e)[:400]
        if isinstance(e, urllib.error.HTTPError):
            try: msg += " | " + e.read().decode()[:400]
            except Exception: pass
        result["steps"].append(f"FAILED {name}: {msg}")

# 4. Credits after
try:
    sub2 = api("/v1/user/subscription")
    result["credits_after"] = sub2.get("character_count")
    if chars_before is not None:
        result["credits_spent_this_run"] = sub2.get("character_count", 0) - chars_before
except Exception:
    pass

result["status"] = "ok"
write_result()
print("done:", json.dumps(result["steps"]))
