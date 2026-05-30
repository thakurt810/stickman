"""
😂 Stickman Dad Joke Pipeline  — Full Scene Image Edition  v2
==============================================================
Portrait 720×1280  |  4 full-scene images  |  edge-tts audio  |  Gemini jokes
Images used:
  black_talking.png  — black is speaking, orange listening
  orange_talking.png — orange is speaking, black listening
  left.png           — both laughing pose A
  right.png          — both laughing pose B  (alternated rapidly)

Bubble placement:
  black  talking → bubble top-left  area (over black character)
  orange talking → bubble top-right area (over orange character)
  laughing       → bubble centred, yellow tint + floating HA! text

Run:
  pip install pillow numpy imageio[ffmpeg] edge-tts google-genai
  python3 stickman_dadjoke_v2.py
"""

import os, json, asyncio, random, math, struct, wave, time, subprocess
import numpy as np
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

# ── CONFIGURATION ──────────────────────────────────────────────────────────
FPS            = 24
W, H           = 720, 1280          # output resolution (portrait reel)
LAUGH_SWAP_FPS = 5                  # times/sec left↔right swaps during laugh
LAUGH_DURATION = 5.0                # seconds for laugh scene

# Paths — adjust if running locally
IMAGES_DIR  = os.environ.get("IMAGES_DIR",  "/mnt/user-data/uploads")
OUTPUT_DIR  = os.environ.get("OUTPUT_DIR",  "output")
AUDIO_DIR   = os.environ.get("AUDIO_DIR",   "audio")
METADATA_DIR= os.environ.get("METADATA_DIR","metadata")

GEMINI_API_KEY        = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_DRIVE_FOLDER   = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
METADATA_DRIVE_FOLDER = os.environ.get("METADATA_DRIVE_FOLDER_ID", "")
UPLOAD_TO_DRIVE       = os.environ.get("UPLOAD_TO_DRIVE", "false").lower() == "true"
BG_MUSIC_PATH         = os.environ.get("BG_MUSIC_PATH", "")

VOICE_A = "en-US-GuyNeural"      # Black  stickman
VOICE_B = "en-US-AriaNeural"     # Orange stickman

MAX_RETRIES = 3

# Bubble anchor positions (cx, top_y) in the OUTPUT 720×1280 frame
# Black  is on LEFT  → bubble upper-left
# Orange is on RIGHT → bubble upper-right
BUBBLE_BLACK_CX  = 210   # centre-x of black's bubble
BUBBLE_ORANGE_CX = 510   # centre-x of orange's bubble
BUBBLE_TOP_Y     = 60    # y where bubble starts from top
BUBBLE_AB_CX     = 360   # centred for laugh bubble

# ── OPTIONAL IMPORTS ───────────────────────────────────────────────────────
try:
    from google import genai as google_genai
    from google.genai import types as genai_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    print("⚠️  google-genai not installed — will use demo joke.")

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False
    print("⚠️  edge-tts not installed — will use silent placeholders.")

try:
    import imageio.v3 as iio
    HAS_IMAGEIO = True
except ImportError:
    HAS_IMAGEIO = False
    print("⚠️  imageio not installed — pip install imageio[ffmpeg]")


# ══════════════════════════════════════════════════════════════════════════════
# 1.  SCENE IMAGE LOADER
# ══════════════════════════════════════════════════════════════════════════════
def _load_scene(filename: str) -> Image.Image:
    """Load a full-scene PNG and resize to output W×H."""
    path = os.path.join(IMAGES_DIR, filename)
    img  = Image.open(path).convert("RGB")
    img  = img.resize((W, H), Image.LANCZOS)
    return img

print("📂  Loading scene images…")
SCENES = {
    "black_talking":  _load_scene("black_talking.png"),
    "orange_talking": _load_scene("orange_talking.png"),
    "left":           _load_scene("left.png"),
    "right":          _load_scene("right.png"),
}
print("    ✅ All 4 scenes loaded.")


# ══════════════════════════════════════════════════════════════════════════════
# 2.  FONT HELPER
# ══════════════════════════════════════════════════════════════════════════════
def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    suffix = "-Bold" if bold else ""
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans{suffix}.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _wrap(text: str, max_chars: int = 28) -> list:
    words = text.split()
    lines, line = [], []
    for w in words:
        if len(" ".join(line + [w])) <= max_chars:
            line.append(w)
        else:
            if line:
                lines.append(" ".join(line))
            line = [w]
    if line:
        lines.append(" ".join(line))
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# 3.  SPEECH BUBBLE
# ══════════════════════════════════════════════════════════════════════════════
def draw_bubble(canvas: Image.Image,
                text: str,
                speaker: str,        # "A", "B", or "AB"
                alpha: float) -> None:
    """
    Overlay a speech bubble on canvas (in-place via alpha_composite).
    speaker="A"  → black  character  → bubble on LEFT  side
    speaker="B"  → orange character  → bubble on RIGHT side
    speaker="AB" → laugh bubble      → centred, yellow tint
    """
    if alpha <= 0:
        return

    lines    = _wrap(text, max_chars=24)
    font     = _get_font(26)
    name_fnt = _get_font(22, bold=True)
    pad, lh  = 14, 32
    bh       = len(lines) * lh + pad * 2 + 36
    bw       = 300                          # fixed bubble width

    # Pick anchor centre-x
    if speaker == "A":
        bcx = BUBBLE_BLACK_CX
        tab_label = "A"
        bg_col_rgb = (255, 255, 255)
    elif speaker == "B":
        bcx = BUBBLE_ORANGE_CX
        tab_label = "B"
        bg_col_rgb = (255, 255, 255)
    else:
        bcx = BUBBLE_AB_CX
        tab_label = "A & B 😂"
        bg_col_rgb = (255, 245, 180)

    bx = bcx - bw // 2
    by = BUBBLE_TOP_Y

    a_int    = int(245 * min(1.0, alpha))
    layer    = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d        = ImageDraw.Draw(layer)

    # Bubble body
    d.rounded_rectangle(
        [bx, by, bx + bw, by + bh],
        radius=18,
        fill=(*bg_col_rgb, a_int),
        outline=(60, 60, 60, a_int),
        width=2,
    )

    # Speaker tab above bubble
    tab_w = max(70, len(tab_label) * 12 + 20)
    d.rounded_rectangle(
        [bx + 12, by - 28, bx + 12 + tab_w, by + 8],
        radius=9,
        fill=(40, 40, 40, a_int),
    )
    d.text(
        (bx + 12 + tab_w // 2, by - 10),
        tab_label,
        fill=(255, 255, 255, a_int),
        font=name_fnt,
        anchor="mm",
    )

    # Text lines
    for i, ln in enumerate(lines):
        d.text(
            (bx + pad, by + pad + 10 + i * lh),
            ln,
            fill=(20, 20, 20, a_int),
            font=font,
        )

    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(layer)
    canvas.paste(canvas_rgba.convert("RGB"))


# ══════════════════════════════════════════════════════════════════════════════
# 4.  FLOATING  HA!  TEXT
# ══════════════════════════════════════════════════════════════════════════════
# Five HA! emitters spread across the top half of the frame
HA_EMITTERS = [
    (110, 350, 0.9),    # (x, start_y, speed_multiplier)
    (280, 280, 1.2),
    (390, 220, 0.8),
    (530, 300, 1.1),
    (650, 260, 1.0),
]

def draw_ha_text(canvas: Image.Image, t: float) -> None:
    font  = _get_font(44, bold=True)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d     = ImageDraw.Draw(layer)

    for i, (ex, ey, spd) in enumerate(HA_EMITTERS):
        # Each emitter cycles independently
        phase = (t * spd + i * 0.38) % 1.0   # 0→1 = one full float cycle
        y     = int(ey - 200 * phase)          # rises 200px over the cycle
        alpha = max(0, int(255 * (1.0 - phase)))
        if 0 < y < H and alpha > 15:
            # Slight colour variety: red / dark-red / crimson
            colours = [(220,30,30),(180,0,0),(200,50,50),(230,60,0),(210,20,20)]
            col = (*colours[i % len(colours)], alpha)
            d.text((ex, y), "HA!", fill=col, font=font, anchor="mm")

    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(layer)
    canvas.paste(canvas_rgba.convert("RGB"))


# ══════════════════════════════════════════════════════════════════════════════
# 5.  FRAME RENDERER
# ══════════════════════════════════════════════════════════════════════════════
def render_frame(scene_key: str,
                 bubble_text: str,
                 bubble_speaker: str,
                 bubble_alpha: float,
                 is_laugh: bool,
                 t: float) -> np.ndarray:
    """Return one RGB frame as a numpy array (H, W, 3)."""
    frame = SCENES[scene_key].copy()

    if is_laugh:
        draw_ha_text(frame, t)

    draw_bubble(frame, bubble_text, bubble_speaker, bubble_alpha)

    return np.array(frame)


# ══════════════════════════════════════════════════════════════════════════════
# 6.  GEMINI JOKE GENERATION
# ══════════════════════════════════════════════════════════════════════════════
MODEL_FALLBACKS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]

DEMO_JOKE = {
    "dialogues": [
        "A: Why can't you see in the dark?",
        "B: I don't know, why?",
        "A: Because there is no C in dark!",
        "AB: Ha ha ha ha ha ha ha ha!",
    ]
}

def _gemini(prompt: str, max_tokens: int = 400) -> str:
    if not HAS_GEMINI:
        raise RuntimeError("google-genai not installed.")
    last_err = None
    for model in MODEL_FALLBACKS:
        try:
            client = google_genai.Client(api_key=GEMINI_API_KEY)
            cfg    = genai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.9,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
            resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
            return resp.text.strip()
        except Exception as e:
            last_err = e
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                continue
            if "404" in err or "not found" in err.lower():
                continue
            raise
    raise RuntimeError(f"All Gemini models failed: {last_err}")


def generate_joke() -> dict:
    if not HAS_GEMINI or not GEMINI_API_KEY:
        print("  ⚠️  Gemini not available — using demo joke.")
        return DEMO_JOKE

    prompt = """You are a dad joke writer for a short animated video featuring two stickman friends, A and B, chatting in a park.

Write ONE classic dad joke as a 3-line dialogue, followed by a shared laugh line.

STRICT OUTPUT FORMAT — output exactly 4 lines, nothing else:
A: <A asks a "why" or "what do you call" style question — the setup of the joke>
B: <B responds with "I don't know, why?" or "I don't know, what?" — always curious, never the punchline>
A: <A delivers the punchline — a short, punny, wordplay-based answer. The joke must hinge on a double meaning, homophones, or letter/word tricks. Keep it under 12 words.>
AB: Ha ha ha ha ha ha ha ha!

RULES:
1. The joke MUST be a true dad joke — groan-worthy wordplay or pun, family-friendly, G-rated.
2. Line 1 (A): Must be a question starting with "Why..." or "What do you call..." or "How do you...". Max 12 words.
3. Line 2 (B): Must be EXACTLY one of:
   - "I don't know, why?"
   - "I don't know, what?"
4. Line 3 (A): Punchline only. No explanation. Max 12 words. Funny due to wordplay.
5. Line 4 (AB): Must be exactly — Ha ha ha ha ha ha ha ha!
6. Do NOT add numbering, labels, asterisks, explanations, or commentary outside the 4 lines.
7. Do NOT reuse: "no C in the dark", "outstanding in his field", "nacho cheese".
8. Output only the 4 lines. Nothing before. Nothing after."""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  🤖 Gemini attempt {attempt}/{MAX_RETRIES}…")
            text  = _gemini(prompt)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            dlg   = [l for l in lines if ":" in l][:4]

            if len(dlg) != 4:
                raise ValueError(f"Got {len(dlg)} lines, expected 4")
            if not dlg[0].upper().startswith("A:"):
                raise ValueError("Line 1 must be A:")
            if not dlg[1].upper().startswith("B:"):
                raise ValueError("Line 2 must be B:")
            if not dlg[2].upper().startswith("A:"):
                raise ValueError("Line 3 must be A:")
            if not dlg[3].upper().startswith("AB:"):
                raise ValueError("Line 4 must be AB:")

            print("  ✅ Joke generated!")
            for l in dlg:
                print(f"     {l}")
            return {"dialogues": dlg}

        except Exception as e:
            print(f"  ❌ Attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2)

    print("  ⚠️  Using demo joke.")
    return DEMO_JOKE


# ══════════════════════════════════════════════════════════════════════════════
# 7.  AUDIO GENERATION
# ══════════════════════════════════════════════════════════════════════════════
async def _speak(text: str, voice: str, path: str) -> None:
    # edge-tts rate: -20% = 80% speed (slower, more natural)
    tts = edge_tts.Communicate(text, voice, rate="-20%")
    await tts.save(path)


def _generate_audio(text: str, voice: str, path: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            asyncio.run(_speak(text, voice, path))
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
            raise RuntimeError("empty file")
        except Exception as e:
            print(f"      ⚠️  Audio attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(1.5)
    return False


def _write_silent_wav(path: str, duration: float = 3.0, rate: int = 22050) -> None:
    n = int(rate * duration)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * n, *([0] * n)))


def generate_all_audio(joke: dict) -> list:
    os.makedirs(AUDIO_DIR, exist_ok=True)
    audio_files = []

    for i, line in enumerate(joke["dialogues"]):
        speaker, text = line.split(":", 1)
        speaker = speaker.strip().upper()
        text    = text.strip()

        # Laugh line: slow spaced-out text for natural pauses
        if speaker == "AB":
            tts_text = "Ha!  Ha!  Ha!  Ha!  Ha!  Ha!"
            voice    = VOICE_A
        elif speaker == "A":
            tts_text = text
            voice    = VOICE_A
        else:
            tts_text = text
            voice    = VOICE_B

        path = os.path.join(AUDIO_DIR, f"line_{i}.mp3")
        print(f"    🎙️  [{speaker}] {text[:55]}…")

        if HAS_EDGE_TTS:
            ok = _generate_audio(tts_text, voice, path)
            if not ok:
                print(f"    ⚠️  Audio failed for line {i+1} — using silence.")
                path = os.path.join(AUDIO_DIR, f"line_{i}.wav")
                _write_silent_wav(path, duration=4.0 if speaker == "AB" else 3.0)
        else:
            path = os.path.join(AUDIO_DIR, f"line_{i}.wav")
            _write_silent_wav(path, duration=4.0 if speaker == "AB" else 3.0)

        audio_files.append({"path": path, "speaker": speaker, "text": text})

    return audio_files


# ══════════════════════════════════════════════════════════════════════════════
# 8.  AUDIO AMPLITUDE  (for mouth-open detection — used as talking pulse)
# ══════════════════════════════════════════════════════════════════════════════
def get_duration(path: str) -> float:
    """Get audio duration in seconds using ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 3.0


# ══════════════════════════════════════════════════════════════════════════════
# 9.  VIDEO ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════════
def build_frames(joke: dict, audio_files: list) -> list:
    """Render all frames for all scenes and return as list of numpy arrays."""
    all_frames = []

    for i, line_data in enumerate(audio_files):
        speaker  = line_data["speaker"]   # "A", "B", "AB"
        text     = line_data["text"]
        path     = line_data["path"]
        is_laugh = speaker == "AB"

        duration  = get_duration(path) if not is_laugh else LAUGH_DURATION
        n_frames  = int(duration * FPS)

        print(f"  🎬 Rendering scene {i+1}: [{speaker}] {text[:45]}… "
              f"({duration:.1f}s, {n_frames} frames)")

        for fi in range(n_frames):
            t = fi / FPS

            # ── choose scene image ──────────────────────────────────────
            if is_laugh:
                swap_period = max(1, FPS // LAUGH_SWAP_FPS)
                scene_key   = "left" if (fi // swap_period) % 2 == 0 else "right"
            elif speaker == "A":
                scene_key = "black_talking"
            else:
                scene_key = "orange_talking"

            # ── bubble fade-in (0→1 over first 0.35s) ──────────────────
            bubble_alpha = min(1.0, max(0.0, (t - 0.15) / 0.35))

            frame = render_frame(
                scene_key     = scene_key,
                bubble_text   = text,
                bubble_speaker= speaker,
                bubble_alpha  = bubble_alpha,
                is_laugh      = is_laugh,
                t             = t,
            )
            all_frames.append(frame)

    return all_frames


def write_silent_video(frames: list, out_path: str) -> None:
    """Write frames to a silent mp4 using imageio."""
    print(f"  💾 Writing silent video ({len(frames)} frames)…")
    iio.imwrite(
        out_path,
        frames,
        fps=FPS,
        codec="libx264",
        quality=8,
        pixelformat="yuv420p",
    )


def merge_audio(video_path: str, audio_files: list, final_path: str) -> None:
    """
    Concatenate all audio files in order then mux with the video using ffmpeg.
    """
    print("  🔊 Merging audio…")
    os.makedirs(AUDIO_DIR, exist_ok=True)

    # Write ffmpeg concat list
    list_path = os.path.join(AUDIO_DIR, "concat.txt")
    with open(list_path, "w") as f:
        for af in audio_files:
            abs_path = os.path.abspath(af["path"])
            f.write(f"file '{abs_path}'\n")

    concat_audio = os.path.join(AUDIO_DIR, "all_audio.aac")

    # Concatenate all audio segments
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c:a", "aac", "-b:a", "128k",
        concat_audio,
    ], capture_output=True, check=True)

    # Mux video + concatenated audio
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", concat_audio,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        final_path,
    ], capture_output=True, check=True)

    print(f"  ✅ Final video → {final_path}")


# ══════════════════════════════════════════════════════════════════════════════
# 10.  METADATA
# ══════════════════════════════════════════════════════════════════════════════
HASHTAGS = (
    "#Shorts #DadJokes #Stickman #FunnyAnimation #AnimatedShorts "
    "#DadJoke #ParkChat #StickFigure #FunnyVideos #KidsContent "
    "#FamilyFriendly #CartoonReels #JokeOfTheDay #CleanJokes "
    "#AnimationLovers #LaughOutLoud #FunnyReels #StickmanAnimation "
    "#DailyLaugh #FeelGoodVideos"
)

def generate_metadata(joke: dict) -> dict:
    setup     = joke["dialogues"][0].split(":", 1)[1].strip()
    punchline = joke["dialogues"][2].split(":", 1)[1].strip()
    desc = (
        f"😂 Two stickman friends share a dad joke in the park!\n\n"
        f"🎤 {setup}\n\n"
        f"🥁 {punchline}\n\n"
        f"{HASHTAGS}"
    )
    return {
        "youtube_title":       "😂 Stickman Dad Joke of the Day — Wait for the Punchline! #Shorts",
        "youtube_description": desc,
        "instagram_caption":   desc,
    }

def save_metadata(joke: dict, meta: dict, base_name: str) -> str:
    os.makedirs(METADATA_DIR, exist_ok=True)
    path = os.path.join(METADATA_DIR, f"{base_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "joke_lines":   joke["dialogues"],
            **meta,
        }, f, ensure_ascii=False, indent=2)
    print(f"  📄 Metadata → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# 11.  GOOGLE DRIVE UPLOAD  (optional)
# ══════════════════════════════════════════════════════════════════════════════
def _drive_upload(file_path: str, folder_id: str, mime: str = "video/mp4"):
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request
        import pickle, base64, tempfile
    except ImportError:
        print("  ⚠️  pip install google-api-python-client google-auth-oauthlib")
        return None

    creds = None
    token_b64 = os.environ.get("GDRIVE_TOKEN_BASE64", "")
    if token_b64:
        try:
            creds = pickle.loads(base64.b64decode(token_b64))
        except Exception as e:
            print(f"  ⚠️  Token decode failed: {e}")

    if creds is None and os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as fh:
            creds = pickle.load(fh)

    if creds is None:
        print("  ❌ No Drive credentials. Skipping upload.")
        return None

    if not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"  ❌ Token refresh failed: {e}")
            return None

    try:
        svc   = build("drive", "v3", credentials=creds)
        meta  = {"name": os.path.basename(file_path),
                 "parents": [folder_id] if folder_id else []}
        media = MediaFileUpload(file_path, mimetype=mime, resumable=True)
        up    = svc.files().create(body=meta, media_body=media,
                                   fields="id,webViewLink").execute()
        link  = up.get("webViewLink")
        print(f"  ☁️  Uploaded → {link}")
        return link
    except Exception as e:
        print(f"  ❌ Drive upload failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 12.  MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print("\n😂  Stickman Dad Joke Pipeline  v2  (Full-Scene Image Edition)")
    print("=" * 65)

    if not HAS_IMAGEIO:
        print("❌  imageio required:  pip install imageio[ffmpeg]")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base_name = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"   Base name: {base_name}")

    # ── Step 1: joke ──────────────────────────────────────────────────────
    print("\n📖 Step 1 — Generating joke…")
    joke = generate_joke()

    # ── Step 2: metadata ──────────────────────────────────────────────────
    print("\n🏷️  Step 2 — Metadata…")
    meta      = generate_metadata(joke)
    json_path = save_metadata(joke, meta, base_name)
    print(f"   Title: {meta['youtube_title']}")

    # ── Step 3: audio ─────────────────────────────────────────────────────
    print("\n🎙️  Step 3 — Generating audio…")
    audio_files = generate_all_audio(joke)

    # ── Step 4: render frames ─────────────────────────────────────────────
    print("\n🎬 Step 4 — Rendering frames…")
    frames = build_frames(joke, audio_files)
    print(f"   Total frames: {len(frames)}  ({len(frames)/FPS:.1f}s)")

    # ── Step 5: write silent video ────────────────────────────────────────
    silent_path = os.path.join(OUTPUT_DIR, f"{base_name}_silent.mp4")
    write_silent_video(frames, silent_path)

    # ── Step 6: mux audio ─────────────────────────────────────────────────
    final_path = os.path.join(OUTPUT_DIR, f"{base_name}.mp4")
    try:
        merge_audio(silent_path, audio_files, final_path)
        os.remove(silent_path)   # clean up temp
    except Exception as e:
        print(f"  ⚠️  Audio mux failed ({e}) — keeping silent video as output.")
        final_path = silent_path

    # ── Step 7: Drive upload (optional) ───────────────────────────────────
    if UPLOAD_TO_DRIVE:
        print("\n☁️  Step 7 — Drive upload…")
        if GOOGLE_DRIVE_FOLDER:
            _drive_upload(final_path, GOOGLE_DRIVE_FOLDER, mime="video/mp4")
        if METADATA_DRIVE_FOLDER:
            _drive_upload(json_path, METADATA_DRIVE_FOLDER, mime="application/json")

    print(f"\n✅  Done!")
    print(f"   Video    → {final_path}")
    print(f"   Metadata → {json_path}\n")


if __name__ == "__main__":
    main()