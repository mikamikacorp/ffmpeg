import datetime
import io
import json
import math
import os
import subprocess
import urllib.request
import urllib.parse
import boto3
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Video constants ──────────────────────────────────────────────────────────
WIDTH, HEIGHT, FPS = 1280, 720, 30
DURATION   = 4.1   # seconds each photo scene is displayed
TRANSITION = 0.5   # xfade transition duration

INTRO_DURATION = 8.0   # cinematic title card
FIN_DURATION   = 5.0   # "FIN" card between slideshow and credits
OUTRO_DURATION = 22.0  # scrolling end credits

# ── Scene plan (50 scenes) ───────────────────────────────────────────────────
SCENE_PLAN = [
    "full", "full", "lr",   "full", "full",
    "tb",   "full", "full", "lr",   "full",
    "full", "full", "tb",   "full", "lr",
    "full", "full", "full", "tb",   "full",
    "lr",   "full", "full", "full", "grid",
    "full", "full", "lr",   "full", "tb",
    "full", "full", "full", "lr",   "full",
    "tb",   "full", "full", "full", "lr",
    "full", "full", "tb",   "full", "full",
    "grid", "full", "full", "lr",   "full",
]
_COST = {"full": 1, "lr": 2, "tb": 2, "grid": 4}
NUM_SCENES        = len(SCENE_PLAN)
NUM_SOURCE_IMAGES = sum(_COST[s] for s in SCENE_PLAN)  # 71

TRANSITIONS = [
    "fade",       "wipeleft",   "slideleft",   "dissolve",    "fadeblack",
    "wiperight",  "slideright", "radial",      "circleopen",  "zoomin",
    "wipeup",     "slideup",    "fadewhite",   "circleclose", "fade",
    "wipedown",   "slidedown",  "dissolve",    "coverleft",   "radial",
    "smoothleft", "smoothright","diagtl",      "diagtr",      "fade",
    "squeezeh",   "squeezev",   "zoomin",      "fadeblack",   "wipeleft",
    "wiperight",  "slideleft",  "slideright",  "dissolve",    "radial",
    "circleopen", "fadewhite",  "wipeup",      "wipedown",    "slideup",
    "slidedown",  "coverleft",  "coverright",  "fade",        "dissolve",
    "zoomin",     "fadeblack",  "wipeleft",    "wiperight",   "fade",
]

GAP, GAP_COLOR = 5, (15, 15, 15)
_UNSPLASH_QUERIES = [
    "family lifestyle", "family outdoor",
    "parents children home", "family portrait", "family vacation",
]


# ── Font utilities ────────────────────────────────────────────────────────────

def _find_font(bold: bool = False) -> str:
    import glob
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    # Fixed path set by Dockerfile (most reliable)
    fixed = f"/opt/fonts/{name}"
    if os.path.exists(fixed):
        return fixed
    # Runtime glob fallback (searches entire /usr/share/fonts tree)
    matches = glob.glob(f"/usr/share/fonts/**/{name}", recursive=True)
    if matches:
        return matches[0]
    raise RuntimeError(f"{name} not found — install dejavu-sans-fonts")


def _esc(text: str) -> str:
    """Escape text for FFmpeg drawtext filter."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


# ── Gradient image generation (Unsplash fallback) ───────────────────────────

def _hsv_to_rgb(h_arr: np.ndarray, s: float, v: float):
    h  = h_arr % 1.0
    i  = (h * 6).astype(np.int32) % 6
    f  = (h * 6) - (h * 6).astype(np.int32)
    p, q = v*(1-s), v*(1-f*s)
    t_v  = v*(1-(1-f)*s)
    vv   = np.full_like(h, v)
    r = np.select([i==0,i==1,i==2,i==3,i==4,i==5],[vv,q, p, p, t_v,vv])
    g = np.select([i==0,i==1,i==2,i==3,i==4,i==5],[t_v,vv,vv,q, p,  p ])
    b = np.select([i==0,i==1,i==2,i==3,i==4,i==5],[p, p, t_v,vv,vv, q ])
    return r, g, b


def _generate_gradient(idx: int) -> Image.Image:
    x = np.linspace(0,1,WIDTH,dtype=np.float32)
    y = np.linspace(0,1,HEIGHT,dtype=np.float32)
    X,Y = np.meshgrid(x,y)
    rad = math.radians((idx*53)%360)
    pat = idx % 6
    if   pat==0: t=np.clip((X*math.cos(rad)+Y*math.sin(rad)+1)/2,0,1)
    elif pat==1: t=np.clip(np.sqrt((X-.5)**2+(Y-.5)**2)/.8,0,1)
    elif pat==2: t=X
    elif pat==3: t=np.clip(np.sin(X*math.pi*2+idx*.4)*.3+Y,0,1)
    elif pat==4: t=np.clip(1-np.sqrt((X-.5)**2+(Y-.5)**2)*2,0,1)
    else:        t=np.clip(X*.6+Y*.4,0,1)
    phi  = (1+math.sqrt(5))/2
    h    = ((idx/phi)%1*(1-t)+((idx/phi+.38)%1)*t).astype(np.float32)
    r,g,b = _hsv_to_rgb(h,.78,.92)
    vig  = np.clip(1-np.sqrt((X-.5)**2+(Y-.5)**2)*1.1,.3,1.0)
    rgb  = np.stack([np.clip(c*vig*255,0,255).astype(np.uint8) for c in (r,g,b)],axis=2)
    return Image.fromarray(rgb,"RGB")


# ── Unsplash download ────────────────────────────────────────────────────────

def _download_from_unsplash(dest_dir: str, access_key: str, count: int) -> list[str]:
    photo_urls: list[str] = []
    for query in _UNSPLASH_QUERIES:
        if len(photo_urls) >= count:
            break
        for page in range(1, 5):
            if len(photo_urls) >= count:
                break
            api_url = (
                "https://api.unsplash.com/search/photos"
                f"?query={urllib.parse.quote(query)}&per_page=30&page={page}"
                f"&orientation=landscape&client_id={access_key}"
            )
            req = urllib.request.Request(
                api_url, headers={"Accept-Version":"v1","User-Agent":"ffmpeg-slideshow/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    results = json.loads(r.read()).get("results",[])
            except Exception as e:
                print(f"  Unsplash API error: {e}"); break
            if not results: break
            for p in results:
                raw = p["urls"]["raw"]
                photo_urls.append(
                    f"{raw}&w={WIDTH}&h={HEIGHT}&fit=crop&crop=faces,focalpoint&auto=format&q=85")
                if len(photo_urls) >= count: break

    if not photo_urls:
        raise RuntimeError("Unsplash returned 0 photos")

    paths: list[str] = []
    for i in range(count):
        url  = photo_urls[i % len(photo_urls)]
        path = os.path.join(dest_dir, f"photo_{i:03d}.jpg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"ffmpeg-slideshow/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                with open(path,"wb") as f: f.write(r.read())
        except Exception as e:
            print(f"  Photo {i} failed ({e}), using gradient")
            _generate_gradient(i).save(path,"JPEG",quality=85)
        paths.append(path)
        if (i+1)%10==0: print(f"  {i+1}/{count} downloaded")
    return paths


# ── Scene compositing ─────────────────────────────────────────────────────────

def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    sw, sh = img.size
    scale  = max(w/sw, h/sh)
    nw, nh = int(sw*scale), int(sh*scale)
    img    = img.resize((nw,nh), Image.LANCZOS)
    l,t    = (nw-w)//2, (nh-h)//2
    return img.crop((l,t,l+w,t+h))


def compose_scene(scene_type: str, srcs: list[Image.Image]) -> Image.Image:
    canvas = Image.new("RGB",(WIDTH,HEIGHT),GAP_COLOR)
    draw   = ImageDraw.Draw(canvas)
    if scene_type == "full":
        canvas.paste(_fit(srcs[0],WIDTH,HEIGHT),(0,0))
    elif scene_type == "lr":
        pw = (WIDTH-GAP)//2
        canvas.paste(_fit(srcs[0],pw,HEIGHT),(0,0))
        canvas.paste(_fit(srcs[1],pw,HEIGHT),(pw+GAP,0))
        draw.rectangle([pw,0,pw+GAP-1,HEIGHT-1],fill=GAP_COLOR)
    elif scene_type == "tb":
        ph = (HEIGHT-GAP)//2
        canvas.paste(_fit(srcs[0],WIDTH,ph),(0,0))
        canvas.paste(_fit(srcs[1],WIDTH,ph),(0,ph+GAP))
        draw.rectangle([0,ph,WIDTH-1,ph+GAP-1],fill=GAP_COLOR)
    elif scene_type == "grid":
        pw,ph = (WIDTH-GAP)//2, (HEIGHT-GAP)//2
        for src,pos in zip(srcs,[(0,0),(pw+GAP,0),(0,ph+GAP),(pw+GAP,ph+GAP)]):
            canvas.paste(_fit(src,pw,ph),pos)
        draw.rectangle([pw,0,pw+GAP-1,HEIGHT-1],fill=GAP_COLOR)
        draw.rectangle([0,ph,WIDTH-1,ph+GAP-1],fill=GAP_COLOR)
    return canvas


def create_scene_frames(tmp_dir: str) -> list[str]:
    src_dir   = os.path.join(tmp_dir,"sources"); os.makedirs(src_dir,exist_ok=True)
    scene_dir = os.path.join(tmp_dir,"scenes");  os.makedirs(scene_dir,exist_ok=True)

    access_key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if access_key:
        print(f"Downloading {NUM_SOURCE_IMAGES} photos from Unsplash…")
        src_paths = _download_from_unsplash(src_dir, access_key, NUM_SOURCE_IMAGES)
    else:
        print(f"Generating {NUM_SOURCE_IMAGES} gradient images…")
        src_paths = []
        for i in range(NUM_SOURCE_IMAGES):
            p = os.path.join(src_dir,f"src_{i:03d}.jpg")
            _generate_gradient(i).save(p,"JPEG",quality=85)
            src_paths.append(p)

    scene_paths: list[str] = []
    src_idx = 0
    for si, stype in enumerate(SCENE_PLAN):
        cost = _COST[stype]
        srcs = [Image.open(src_paths[src_idx+j]) for j in range(cost)]
        src_idx += cost
        frame = compose_scene(stype, srcs)
        p = os.path.join(scene_dir, f"scene_{si:03d}.jpg")
        frame.save(p,"JPEG",quality=90)
        scene_paths.append(p)

    print(f"Composed {NUM_SCENES} scenes "
          f"({SCENE_PLAN.count('full')} full / {SCENE_PLAN.count('lr')} lr / "
          f"{SCENE_PLAN.count('tb')} tb / {SCENE_PLAN.count('grid')} grid)")
    return scene_paths


# ── Music download ────────────────────────────────────────────────────────────

def _download_from_jamendo(tmp_dir: str, client_id: str, min_dur: float) -> str:
    import random

    def _search(extra: str) -> list:
        url = (
            "https://api.jamendo.com/v3.0/tracks/"
            f"?client_id={client_id}&format=json&limit=20"
            "&tags=acoustic&vocalinstrumental=instrumental"
            f"&audioformat=mp31&order=popularity_total{extra}"
        )
        req = urllib.request.Request(url,headers={"User-Agent":"ffmpeg-slideshow/1.0"})
        with urllib.request.urlopen(req,timeout=15) as r:
            return json.loads(r.read()).get("results",[])

    results = _search(f"&durationbetween={int(min_dur)}_600") or _search("")
    if not results:
        raise RuntimeError("Jamendo returned 0 tracks — check JAMENDO_CLIENT_ID")

    track = random.choice(results)
    print(f"Music: '{track['name']}' / {track['artist_name']} ({track['duration']}s) [Jamendo CC]")
    path = os.path.join(tmp_dir,"music.mp3")
    req  = urllib.request.Request(track["audio"],headers={"User-Agent":"ffmpeg-slideshow/1.0"})
    with urllib.request.urlopen(req,timeout=60) as r:
        with open(path,"wb") as f: f.write(r.read())
    return path


def get_music(tmp_dir: str, total_duration: float) -> str | None:
    path = os.path.join(tmp_dir,"music.mp3")

    s3_key = os.environ.get("MUSIC_S3_KEY")
    if s3_key:
        print(f"Downloading music from S3…")
        boto3.client("s3").download_file(os.environ["OUTPUT_BUCKET"],s3_key,path)
        return path

    music_url = os.environ.get("MUSIC_URL")
    if music_url:
        print("Downloading music from URL…")
        req = urllib.request.Request(music_url,headers={"User-Agent":"ffmpeg-slideshow/1.0"})
        with urllib.request.urlopen(req,timeout=60) as r:
            with open(path,"wb") as f: f.write(r.read())
        return path

    jamendo_id = os.environ.get("JAMENDO_CLIENT_ID")
    if jamendo_id:
        print("Searching Jamendo…")
        return _download_from_jamendo(tmp_dir, jamendo_id, total_duration)

    print("No music configured")
    return None


# ── Intro clip (cinematic title card) ─────────────────────────────────────────

def _render_intro_frame(
    t: float, title: str, subtitle: str, location: str, fonts: dict
) -> Image.Image:
    """Render one intro frame at time t using PIL (no FFmpeg drawtext needed)."""
    D = INTRO_DURATION

    def _alpha(t_start: float, in_dur: float = 1.5) -> float:
        out_start = D - 1.5
        if t < t_start:              return 0.0
        if t < t_start + in_dur:     return (t - t_start) / in_dur
        if t < out_start:            return 1.0
        if t < D:                    return (D - t) / 1.5
        return 0.0

    # Transparent overlay for alpha-compositing
    base = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
    over = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    cy   = HEIGHT // 2

    def put(text: str, font, y: int, a: float, color=(255, 255, 255)) -> None:
        if a <= 0:
            return
        bbox = draw.textbbox((0, 0), text, font=font)
        tw   = bbox[2] - bbox[0]
        x    = (WIDTH - tw) // 2
        draw.text((x, y), text, font=font, fill=(*color, int(a * 255)))

    put(title,      fonts["bold"],  cy - 90, _alpha(0.0), (255, 255, 255))
    put("─" * 30,   fonts["sep"],   cy +  8, _alpha(0.4), (110, 110, 110))
    put(subtitle,   fonts["reg"],   cy + 38, _alpha(0.6), (204, 204, 204))
    put(location,   fonts["small"], cy + 88, _alpha(1.0), (136, 136, 136))

    return Image.alpha_composite(base, over).convert("RGB")


def create_intro_clip(tmp_dir: str, title: str, subtitle: str, location: str) -> str:
    """Render intro frames with PIL, save to disk, then encode with FFmpeg."""
    import shutil
    fonts = {
        "bold":  ImageFont.truetype(_find_font(bold=True),  80),
        "reg":   ImageFont.truetype(_find_font(bold=False), 36),
        "small": ImageFont.truetype(_find_font(bold=False), 28),
        "sep":   ImageFont.truetype(_find_font(bold=False), 18),
    }

    frames_dir   = os.path.join(tmp_dir, "intro_frames")
    os.makedirs(frames_dir, exist_ok=True)
    total_frames = int(INTRO_DURATION * FPS)

    for fn in range(total_frames):
        frame = _render_intro_frame(fn / FPS, title, subtitle, location, fonts)
        frame.save(os.path.join(frames_dir, f"f{fn:05d}.png"), "PNG")

    out = os.path.join(tmp_dir, "intro.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(frames_dir, "f%05d.png"),
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-an", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(frames_dir, ignore_errors=True)  # free /tmp space
    if r.returncode != 0:
        raise RuntimeError(f"Intro FFmpeg failed:\n{r.stderr[-2000:]}")

    print(f"Intro created: {out}")
    return out


# ── FIN card ─────────────────────────────────────────────────────────────────

def create_fin_clip(tmp_dir: str) -> str:
    """Render a cinematic 'FIN' title card — black bg, serif-style fade in/out."""
    import shutil

    font_bold = _find_font(bold=True)
    fnt_fin   = ImageFont.truetype(font_bold, 120)
    fnt_line  = ImageFont.truetype(_find_font(bold=False), 20)

    D            = FIN_DURATION
    total_frames = int(D * FPS)
    frames_dir   = os.path.join(tmp_dir, "fin_frames")
    os.makedirs(frames_dir, exist_ok=True)
    black        = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

    for fn in range(total_frames):
        t = fn / FPS

        # Fade in 0-1.5s, hold 1.5-3.5s, fade out 3.5-5s
        if   t < 1.5:          a = t / 1.5
        elif t < 3.5:          a = 1.0
        elif t < D:            a = (D - t) / 1.5
        else:                  a = 0.0

        base = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 255))
        over = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(over)
        cy   = HEIGHT // 2

        def put(text, font, y, color, alpha_val):
            if alpha_val <= 0:
                return
            bbox = draw.textbbox((0, 0), text, font=font)
            tw   = bbox[2] - bbox[0]
            x    = (WIDTH - tw) // 2
            draw.text((x, y), text, font=font, fill=(*color, int(alpha_val * 255)))

        put("FIN",      fnt_fin,  cy - 65, (255, 255, 255), a)
        put("─" * 20,   fnt_line, cy + 68, (100, 100, 100), a)

        frame = Image.alpha_composite(base, over).convert("RGB")
        frame.save(os.path.join(frames_dir, f"f{fn:05d}.png"), "PNG")

    out = os.path.join(tmp_dir, "fin.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(frames_dir, "f%05d.png"),
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-an", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"FIN FFmpeg failed:\n{r.stderr[-2000:]}")

    print(f"FIN card created: {out}")
    return out


# ── Outro clip (scrolling end credits) ───────────────────────────────────────

def _credits_lines(title: str, num_photos: int, year: str) -> list[tuple[str,str]]:
    return [
        ("big",     title),
        ("empty",   ""),
        ("empty",   ""),
        ("section", "Photographs"),
        ("small",   "Family Collection"),
        ("small",   f"{num_photos} Photos"),
        ("empty",   ""),
        ("empty",   ""),
        ("section", "Music"),
        ("small",   "Courtesy of Jamendo"),
        ("small",   "Creative Commons Licensed"),
        ("empty",   ""),
        ("empty",   ""),
        ("section", "Video Production"),
        ("small",   "AWS Lambda"),
        ("small",   "FFmpeg"),
        ("empty",   ""),
        ("empty",   ""),
        ("empty",   ""),
        ("divider", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        ("empty",   ""),
        ("medium",  "With Love,"),
        ("medium",  "For Our Family & Friends"),
        ("empty",   ""),
        ("divider", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        ("empty",   ""),
        ("empty",   ""),
        ("big",     year),
        ("empty",   ""),
        ("empty",   ""),
    ]


def _render_credits_image(tmp_dir: str, lines: list[tuple[str,str]]) -> tuple[str, int]:
    """Render a tall PIL image containing all credits."""
    font_bold = _find_font(bold=True)
    font_reg  = _find_font(bold=False)

    _LH    = {"big":90, "section":65, "medium":55, "small":50, "empty":30, "divider":40}
    _FS    = {"big":56, "section":40, "medium":36, "small":28, "divider":24}
    _COLOR = {"big":(240,240,240),"section":(200,200,200),"medium":(180,180,180),
              "small":(150,150,150),"divider":(70,70,70)}
    _BOLD  = {"big","section"}

    total_h = HEIGHT + sum(_LH[t] for t,_ in lines) + HEIGHT
    img  = Image.new("RGB",(WIDTH,total_h),(0,0,0))
    draw = ImageDraw.Draw(img)

    y = HEIGHT  # content starts below the first visible window
    for ltype, text in lines:
        if ltype == "empty" or not text:
            y += _LH.get(ltype, 30)
            continue
        fnt   = ImageFont.truetype(font_bold if ltype in _BOLD else font_reg, _FS[ltype])
        color = _COLOR[ltype]
        bbox  = draw.textbbox((0,0), text, font=fnt)
        tw    = bbox[2] - bbox[0]
        draw.text(((WIDTH-tw)//2, y), text, fill=color, font=fnt)
        y += _LH[ltype]

    path = os.path.join(tmp_dir, "credits.png")
    img.save(path, "PNG")
    return path, total_h


def create_outro_clip(tmp_dir: str, title: str, num_photos: int, year: str) -> str:
    """Render scrolling credits frame-by-frame with PIL, then encode with FFmpeg."""
    import shutil

    lines = _credits_lines(title, num_photos, year)
    credits_img_path, total_h = _render_credits_image(tmp_dir, lines)
    credits_img = Image.open(credits_img_path).convert("RGB")

    D          = OUTRO_DURATION
    pause_s    = 1.0
    scroll_h   = total_h - HEIGHT
    scroll_dur = D - pause_s - 1.5
    speed      = scroll_h / scroll_dur   # px per second

    frames_dir   = os.path.join(tmp_dir, "outro_frames")
    os.makedirs(frames_dir, exist_ok=True)
    total_frames = int(D * FPS)
    black        = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

    for fn in range(total_frames):
        t = fn / FPS

        # Scroll position
        y = 0 if t < pause_s else min(scroll_h, (t - pause_s) * speed)
        y = int(y)
        frame = credits_img.crop((0, y, WIDTH, y + HEIGHT)).copy()

        # Fade in / fade out via blend with black
        if t < 1.0:
            frame = Image.blend(black, frame, t / 1.0)
        elif t > D - 1.5:
            frame = Image.blend(black, frame, (D - t) / 1.5)

        frame.save(os.path.join(frames_dir, f"f{fn:05d}.jpg"), "JPEG", quality=88)

    out = os.path.join(tmp_dir, "outro.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", os.path.join(frames_dir, "f%05d.jpg"),
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-an", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    shutil.rmtree(frames_dir, ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"Outro FFmpeg failed:\n{r.stderr[-2000:]}")

    print(f"Outro created: {out}")
    return out


# ── Main slideshow clip (no music, no intro/outro) ────────────────────────────

def build_slideshow_clip(paths: list[str], out_path: str) -> str:
    n     = len(paths)
    step  = DURATION - TRANSITION
    total = n * DURATION - (n-1) * TRANSITION

    cmd = ["ffmpeg", "-y", "-threads", "0"]
    for p in paths:
        cmd += ["-loop","1","-framerate",str(FPS),"-t",str(DURATION+TRANSITION),"-i",p]

    filters: list[str] = []
    for i in range(n):
        filters.append(
            f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}[s{i}]"
        )
    cur = "s0"
    for i in range(n-1):
        out  = "final" if i==n-2 else f"x{i}"
        tr   = TRANSITIONS[i % len(TRANSITIONS)]
        off  = step*(i+1)
        filters.append(
            f"[{cur}][s{i+1}]xfade=transition={tr}:duration={TRANSITION}:offset={off:.3f}[{out}]"
        )
        cur = out

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[final]", "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-t", f"{total:.3f}", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=840)
    if r.returncode != 0:
        raise RuntimeError(f"Slideshow FFmpeg failed:\n{r.stderr[-5000:]}")
    print(f"Slideshow created: {out_path}")
    return out_path


# ── Duration helper ───────────────────────────────────────────────────────────

def _get_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe","-v","quiet","-print_format","json","-show_format",path],
        capture_output=True, text=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


# ── Final concat + music ──────────────────────────────────────────────────────

def concat_with_music(clips: list[str], music_path: str | None, out_path: str) -> None:
    """Concatenate intro + slideshow + outro and optionally mix background music."""
    total_dur = sum(_get_duration(p) for p in clips)
    n = len(clips)

    cmd = ["ffmpeg", "-y", "-threads", "0"]
    for p in clips:
        cmd += ["-i", p]
    if music_path:
        cmd += ["-i", music_path]

    v_filter = f"{''.join(f'[{i}:v]' for i in range(n))}concat=n={n}:v=1:a=0[v]"

    if music_path:
        fade_start = total_dur - 4.0
        a_filter = (
            f"[{n}:a]aloop=loop=-1:size=2000000000,"
            f"atrim=0:{total_dur:.3f},"
            f"afade=t=out:st={fade_start:.3f}:d=4,"
            f"asetpts=PTS-STARTPTS[a]"
        )
        cmd += [
            "-filter_complex", f"{v_filter};{a_filter}",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", out_path,
        ]
    else:
        cmd += [
            "-filter_complex", v_filter,
            "-map", "[v]", "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", out_path,
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"Concat FFmpeg failed:\n{r.stderr[-2000:]}")
    print(f"Final video: {out_path} ({os.path.getsize(out_path):,} bytes)")


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    tmp_dir  = "/tmp/slideshow"
    os.makedirs(tmp_dir, exist_ok=True)
    out_path = "/tmp/slideshow/output.mp4"

    bucket = os.environ["OUTPUT_BUCKET"]
    key    = f"slideshows/slideshow_{context.aws_request_id}.mp4"

    try:
        year     = str(datetime.date.today().year)
        title    = os.environ.get("TITLE_TEXT",    "Family Memories")
        subtitle = os.environ.get("SUBTITLE_TEXT",  f"Summer {year}")
        location = os.environ.get("LOCATION_TEXT",  "Japan")

        # 1. Cinematic intro
        print("── Intro ──")
        intro_path = create_intro_clip(tmp_dir, title, subtitle, location)

        # 2. Photo scenes
        print("── Scene frames ──")
        scene_paths = create_scene_frames(tmp_dir)

        # 3. Main slideshow (no music)
        print("── Slideshow clip ──")
        slideshow_path = os.path.join(tmp_dir, "slideshow.mp4")
        build_slideshow_clip(scene_paths, slideshow_path)

        # 4. FIN card
        print("── FIN card ──")
        fin_path = create_fin_clip(tmp_dir)

        # 5. Scrolling end credits
        print("── Outro ──")
        outro_path = create_outro_clip(tmp_dir, title, NUM_SOURCE_IMAGES, year)

        # 6. Music
        all_clips  = [intro_path, slideshow_path, fin_path, outro_path]
        total_dur  = sum(_get_duration(p) for p in all_clips)
        music_path = get_music(tmp_dir, total_dur)

        # 7. Concat everything + mix music
        print("── Final concat ──")
        concat_with_music(all_clips, music_path, out_path)

        # 7. Upload to S3
        size = os.path.getsize(out_path)
        s3   = boto3.client("s3")
        s3.upload_file(out_path, bucket, key, ExtraArgs={"ContentType":"video/mp4"})

        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket":bucket,"Key":key}, ExpiresIn=3600)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message":         "Slideshow created successfully!",
                "download_url":    url,
                "s3_path":         f"s3://{bucket}/{key}",
                "total_duration_s": round(total_dur, 1),
                "file_size_bytes": size,
            }),
        }

    except Exception as exc:
        import traceback
        print(traceback.format_exc())
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}
