import datetime
import json
import os
import subprocess
import boto3
from PIL import Image, ImageDraw, ImageFont

# /opt/bin is where Lambda layer binaries (ffmpeg, ffprobe) are placed
os.environ["PATH"] = "/opt/bin:" + os.environ.get("PATH", "")

# ── Video constants ──────────────────────────────────────────────────────────
WIDTH, HEIGHT, FPS = 1280, 720, 30
DURATION   = 4.1   # seconds each photo scene is displayed
TRANSITION = 0.5   # xfade transition duration

INTRO_DURATION = 8.0   # cinematic title card
OUTRO_DURATION = 22.0  # scrolling end credits

_COST = {"full": 1, "lr": 2, "tb": 2, "grid": 4}
_SCENE_PATTERN = [
    "full", "full", "lr",  "full", "full",
    "tb",   "full", "full","lr",   "full",
]

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


# ── S3 photo download ─────────────────────────────────────────────────────────

def _download_photos_from_s3(dest_dir: str, s3, bucket: str, prefix: str) -> list[str]:
    IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
    paginator  = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if os.path.splitext(obj["Key"])[1].lower() in IMAGE_EXTS:
                keys.append(obj["Key"])

    if not keys:
        raise RuntimeError(f"No images found at s3://{bucket}/{prefix}")
    keys.sort()

    paths: list[str] = []
    for i, key in enumerate(keys):
        ext  = os.path.splitext(key)[1].lower()
        dest = os.path.join(dest_dir, f"photo_{i:03d}{ext}")
        s3.download_file(bucket, key, dest)
        paths.append(dest)
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(keys)} downloaded")
    print(f"  {len(keys)} photos downloaded from s3://{bucket}/{prefix}")
    return paths


# ── Dynamic scene plan ────────────────────────────────────────────────────────

def _make_scene_plan(n: int) -> list[str]:
    """Generate a scene plan that consumes exactly n source images."""
    plan: list[str] = []
    remaining, pi = n, 0
    while remaining > 0:
        stype = _SCENE_PATTERN[pi % len(_SCENE_PATTERN)]
        cost  = _COST[stype]
        if cost > remaining:
            stype, cost = "full", 1
        plan.append(stype)
        remaining -= cost
        pi += 1
    return plan


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


def create_scene_frames(tmp_dir: str) -> tuple[list[str], list[str]]:
    """Returns (scene_paths, scene_plan)."""
    src_dir   = os.path.join(tmp_dir,"sources"); os.makedirs(src_dir,exist_ok=True)
    scene_dir = os.path.join(tmp_dir,"scenes");  os.makedirs(scene_dir,exist_ok=True)

    s3     = boto3.client("s3")
    bucket = os.environ.get("PHOTOS_S3_BUCKET", os.environ["OUTPUT_BUCKET"])
    prefix = os.environ["PHOTOS_S3_PREFIX"]
    print(f"Downloading photos from s3://{bucket}/{prefix}…")
    src_paths  = _download_photos_from_s3(src_dir, s3, bucket, prefix)
    scene_plan = _make_scene_plan(len(src_paths))

    scene_paths: list[str] = []
    src_idx = 0
    for si, stype in enumerate(scene_plan):
        cost = _COST[stype]
        srcs = [Image.open(src_paths[src_idx+j]) for j in range(cost)]
        src_idx += cost
        frame = compose_scene(stype, srcs)
        p = os.path.join(scene_dir, f"scene_{si:03d}.jpg")
        frame.save(p,"JPEG",quality=90)
        scene_paths.append(p)

    print(f"Composed {len(scene_plan)} scenes from {len(src_paths)} photos "
          f"({scene_plan.count('full')} full / {scene_plan.count('lr')} lr / "
          f"{scene_plan.count('tb')} tb / {scene_plan.count('grid')} grid)")
    return scene_paths, scene_plan


# ── Music download ────────────────────────────────────────────────────────────

def get_music(tmp_dir: str) -> str | None:
    s3_key = os.environ.get("MUSIC_S3_KEY")
    if not s3_key:
        print("MUSIC_S3_KEY not set — no BGM")
        return None

    path   = os.path.join(tmp_dir, "music.mp3")
    bucket = os.environ.get("MUSIC_S3_BUCKET", os.environ["OUTPUT_BUCKET"])
    print(f"Downloading music from s3://{bucket}/{s3_key}…")
    boto3.client("s3").download_file(bucket, s3_key, path)
    return path


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
        scene_paths, scene_plan = create_scene_frames(tmp_dir)
        num_photos = sum(_COST[s] for s in scene_plan)

        # 3. Main slideshow (no music)
        print("── Slideshow clip ──")
        slideshow_path = os.path.join(tmp_dir, "slideshow.mp4")
        build_slideshow_clip(scene_paths, slideshow_path)

        # 4. Scrolling end credits
        print("── Outro ──")
        outro_path = create_outro_clip(tmp_dir, title, num_photos, year)

        # 5. Music
        all_clips  = [intro_path, slideshow_path, outro_path]
        total_dur  = sum(_get_duration(p) for p in all_clips)
        music_path = get_music(tmp_dir)

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
