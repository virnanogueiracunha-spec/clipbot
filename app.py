from flask import Flask, render_template, request, jsonify
import os
import subprocess
import threading
import uuid
import re
from pathlib import Path
import imageio_ffmpeg

app = Flask(__name__)

import shutil

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
YTDLP = shutil.which("yt-dlp") or "yt-dlp"
BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CLIPS_DIR = BASE_DIR / "static" / "clips"

DOWNLOADS_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

jobs = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/process", methods=["POST"])
def process():
    data = request.json
    url = data.get("url", "").strip()
    num_clips = max(1, min(5, int(data.get("num_clips", 3))))
    clip_duration = max(10, min(30, int(data.get("clip_duration", 15))))

    if not url:
        return jsonify({"error": "URL obrigatória"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "iniciando", "progress": 0, "clips": [], "error": None, "title": ""}

    t = threading.Thread(target=process_video, args=(job_id, url, num_clips, clip_duration))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def get_status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(jobs[job_id])


def get_duration(video_path):
    result = subprocess.run([FFMPEG, "-i", str(video_path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0


def detect_scenes(video_path, threshold=0.25):
    result = subprocess.run(
        [FFMPEG, "-i", str(video_path), "-vf", f"select=gt(scene\\,{threshold}),showinfo", "-vsync", "vfr", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120
    )
    times = []
    for line in result.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            times.append(float(m.group(1)))
    return times


def cut_clip(input_path, output_path, start, duration):
    vf = (
        "[0:v]split[main][bg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=50:5[blurred];"
        "[main]scale=1080:-2[overlay];"
        "[blurred][overlay]overlay=(W-w)/2:(H-h)/2"
    )
    cmd = [
        FFMPEG, "-y",
        "-ss", str(start), "-i", str(input_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-c:a", "aac",
        "-preset", "fast", "-crf", "23",
        str(output_path)
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return r.returncode == 0


def process_video(job_id, url, num_clips, clip_duration):
    video_path = DOWNLOADS_DIR / f"{job_id}.mp4"
    try:
        # 1. Download
        jobs[job_id].update({"status": "Baixando vídeo...", "progress": 10})

        dl = subprocess.run(
            [YTDLP, url, "-o", str(video_path), "--format", "best[height<=720][ext=mp4]/best[height<=720]/best", "--no-playlist", "--no-warnings"],
            capture_output=True, text=True, timeout=300
        )

        title_r = subprocess.run([YTDLP, url, "--get-title", "--no-playlist"], capture_output=True, text=True, timeout=30)
        if title_r.returncode == 0:
            jobs[job_id]["title"] = title_r.stdout.strip()

        if dl.returncode != 0 or not video_path.exists():
            jobs[job_id].update({"status": "error", "error": "Não foi possível baixar o vídeo. Verifique o link."})
            return

        # 2. Analyze
        jobs[job_id].update({"status": "Analisando vídeo...", "progress": 35})
        duration = get_duration(video_path)

        if duration < clip_duration + 10:
            jobs[job_id].update({"status": "error", "error": "Vídeo muito curto para gerar clips."})
            return

        # 3. Find moments
        jobs[job_id].update({"status": "Encontrando melhores momentos...", "progress": 50})
        scenes = detect_scenes(video_path)

        margin = 10
        usable_start = margin
        usable_end = duration - clip_duration - margin

        if len(scenes) >= num_clips:
            valid = [t for t in scenes if usable_start < t < usable_end]
            if len(valid) >= num_clips:
                step = len(valid) // num_clips
                selected = [valid[i * step] for i in range(num_clips)]
            else:
                step = (usable_end - usable_start) / num_clips
                selected = [usable_start + i * step for i in range(num_clips)]
        else:
            step = (usable_end - usable_start) / num_clips
            selected = [usable_start + i * step for i in range(num_clips)]

        # 4. Cut clips
        clips = []
        for i, start in enumerate(selected):
            jobs[job_id].update({"status": f"Criando clip {i+1} de {num_clips}...", "progress": 55 + int(i * 40 / num_clips)})
            fname = f"{job_id}_clip{i+1}.mp4"
            out = CLIPS_DIR / fname
            if cut_clip(video_path, out, start, clip_duration):
                clips.append({"filename": fname, "url": f"/static/clips/{fname}", "number": i + 1, "start": round(start, 1)})

        jobs[job_id].update({"status": "concluido", "progress": 100, "clips": clips})

    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})
    finally:
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    app.run(debug=True, port=8080)
