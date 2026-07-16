#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════════╗
║  HLS Subtitle Burner — حارق الترجمة في البث                         ║
║  يأخذ M3U8 + VTT ويُخرج بث HLS جديد بالترجمة مدمجة (hardcoded)     ║
╚═══════════════════════════════════════════════════════════════════════╝

المتطلبات:
  sudo apt install ffmpeg python3 python3-pip
  pip3 install flask

التشغيل:
  python3 hls-burner.py

الاستخدام:
  http://YOUR_SERVER:5000/burn?src=M3U8_URL&sub=VTT_URL&lang=ar

  أو اضبط المتغيرات أدناه وافتح:
  http://YOUR_SERVER:5000/burn

ستحصل على رابط بث HLS بالترجمة مدمجة — لا يمكن إزالتها أبداً.
"""

import os
import re
import sys
import json
import time
import shutil
import signal
import subprocess
import threading
from urllib.parse import urlparse, unquote
from flask import Flask, Response, request, jsonify, send_from_directory
import requests

# ═══════════════════════════════════════════════════════════
#  الإعدادات — عدّلها حسب حاجتك
# ═══════════════════════════════════════════════════════════
PORT = 5000
HOST = '0.0.0.0'

# مجلد العمل المؤقت
WORK_DIR = '/tmp/hls-burner'
OUTPUT_DIR = os.path.join(WORK_DIR, 'output')

# إعدادات ffmpeg
FFMPEG_PRESET = 'ultrafast'     # سرعة الترميز: ultrafast, fast, medium
VIDEO_CODEC = 'libx264'          # h264 أو libx265 (hevc)
AUDIO_CODEC = 'aac'
CRF = 23                         # جودة الفيديو (أقل = أفضل، 18-28)
AUDIO_BITRATE = '128k'

# ترجمة النصوص
SUB_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
SUB_FONT_SIZE = 24
SUB_PRIMARY_COLOR = '&H00FFFFFF'     # أبيض
SUB_OUTLINE_COLOR = '&H00000000'     # أسود
SUB_OUTLINE_WIDTH = 2
SUB_SHADOW_COLOR = '&H80000000'
SUB_SHADOW_DEPTH = 1
SUB_MARGIN_V = 30                     # المسافة من الأسفل
SUB_ALIGNMENT = 2                     # 2 = أسفل الوسط

# ═══════════════════════════════════════════════════════════
#  المسارات
# ═══════════════════════════════════════════════════════════
# تحقق من خط عربي
AR_FONT = '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf'
if os.path.exists(AR_FONT):
    SUB_FONT = AR_FONT
elif os.path.exists('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
    SUB_FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

app = Flask(__name__)

# تخزين حالة العمليات
jobs = {}
jobs_lock = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def cleanup_workdir():
    """تنظيف مجلد العمل"""
    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def download_file(url, dest):
    """تحميل ملف من رابط"""
    r = requests.get(url, stream=True, timeout=30,
                     headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest


def download_vtt(url):
    """تحميل ملف VTT وتحويله لـ SRT (ffmpeg يحتاج srt أحياناً)"""
    vtt_path = os.path.join(WORK_DIR, 'input.vtt')
    srt_path = os.path.join(WORK_DIR, 'input.srt')

    download_file(url, vtt_path)

    # تحويل VTT → SRT
    with open(vtt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # إزالة WEBVTT header و X-TIMESTAMP-MAP
    lines = content.split('\n')
    srt_lines = []
    idx = 1
    in_cue = False

    for line in lines:
        line = line.replace('\r', '')
        if line.startswith('WEBVTT') or line.startswith('X-TIMESTAMP-MAP'):
            continue
        if line.strip() == '' and in_cue:
            in_cue = False
            srt_lines.append('')
            continue
        if '-->' in line:
            in_cue = True
            # تحويل وقت VTT (00:00:01.000) لـ SRT (00:00:01,000)
            line = re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', line)
            srt_lines.append(str(idx))
            idx += 1
        elif in_cue or (line.strip() and not line.startswith('NOTE')):
            if line.strip() and not line.startswith('NOTE'):
                srt_lines.append(line)

    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(srt_lines))

    return vtt_path, srt_path


def build_srt_style():
    """بناء سطر ت styling للترجمة"""
    # ابحث عن خط يدعم العربية
    fonts_to_try = [
        '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    font = SUB_FONT
    for f in fonts_to_try:
        if os.path.exists(f):
            font = f
            break

    return (
        f"Fontname={font},"
        f"FontSize={SUB_FONT_SIZE},"
        f"PrimaryColour={SUB_PRIMARY_COLOR},"
        f"OutlineColour={SUB_OUTLINE_COLOR},"
        f"Outline={SUB_OUTLINE_WIDTH},"
        f"Shadow={SUB_SHADOW_DEPTH},"
        f"MarginV={SUB_MARGIN_V},"
        f"Alignment={SUB_ALIGNMENT}"
    )


def detect_resolution(m3u8_url):
    """اكتشاف دقة الفيديو من الماستر بلاي ليست"""
    try:
        r = requests.get(m3u8_url, timeout=15,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.ok:
            content = r.text
            # أوجد أعلى دقة
            resolutions = re.findall(r'RESOLUTION=(\d+x\d+)', content)
            if resolutions:
                # أخذ الأعلى
                res = max(resolutions, key=lambda x: int(x.split('x')[0]))
                w, h = res.split('x')
                return int(w), int(h)
    except Exception:
        pass
    return 1920, 1080


def burn_subtitles(m3u8_url, subtitle_url, job_id):
    """حرق الترجمة في البث باستخدام ffmpeg"""
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    output_m3u8 = os.path.join(job_dir, 'index.m3u8')
    segment_pattern = os.path.join(job_dir, 'seg_%03d.ts')

    try:
        # تحميل الترجمة
        log(f"[{job_id}] جاري تحميل الترجمة...")
        vtt_path, srt_path = download_vtt(subtitle_url)

        # بناء أمر ASS style
        style = build_srt_style()

        # اكتشاف الدقة
        width, height = detect_resolution(m3u8_url)
        log(f"[{job_id}] الدقة المكتشفة: {width}x{height}")

        # بناء فلتر الترجمة
        # يحتاج تحويل الأبعاد لتكون زوجية (مطلوب لـ h264)
        w_even = width if width % 2 == 0 else width + 1
        h_even = height if height % 2 == 0 else height + 1

        sub_filter = (
            f"scale={w_even}:{h_even},"
            f"subtitles='{srt_path}':"
            f"force_style='{style}'"
        )

        # أمر ffmpeg
        cmd = [
            'ffmpeg',
            '-y',
            '-user_agent', 'Mozilla/5.0',
            '-i', m3u8_url,
            '-vf', sub_filter,
            '-c:v', VIDEO_CODEC,
            '-preset', FFMPEG_PRESET,
            '-crf', str(CRF),
            '-c:a', AUDIO_CODEC,
            '-b:a', AUDIO_BITRATE,
            '-f', 'hls',
            '-hls_time', '6',
            '-hls_list_size', '0',
            '-hls_segment_filename', segment_pattern,
            '-hls_playlist_type', 'vod',
            output_m3u8,
        ]

        log(f"[{job_id}] بدء حرق الترجمة...")
        log(f"[{job_id}] {' '.join(cmd[:10])}...")

        # تشغيل ffmpeg
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # قراءة المخرجات
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                line = line.strip()
                if 'time=' in line or 'frame=' in line:
                    log(f"[{job_id}] {line}")

        return_code = process.wait()

        if return_code == 0:
            log(f"[{job_id}] تم بنجاح! ✅")
            with jobs_lock:
                jobs[job_id]['status'] = 'done'
                jobs[job_id]['progress'] = 100
                jobs[job_id]['output'] = f'/output/{job_id}/index.m3u8'
        else:
            log(f"[{job_id}] فشل ffmpeg (code: {return_code})")
            with jobs_lock:
                jobs[job_id]['status'] = 'error'
                jobs[job_id]['error'] = f'ffmpeg exited with code {return_code}'

    except Exception as e:
        log(f"[{job_id}] خطأ: {e}")
        with jobs_lock:
            jobs[job_id]['status'] = 'error'
            jobs[job_id]['error'] = str(e)


# ═══════════════════════════════════════════════════════════
#  المسارات (Routes)
# ═══════════════════════════════════════════════════════════

@app.route('/')
def index():
    """الصفحة الرئيسية — GUI بسيط"""
    return Response(GUI_HTML, mimetype='text/html; charset=utf-8')


@app.route('/burn', methods=['GET', 'POST'])
def burn():
    """بدء عملية حرق الترجمة"""
    src = request.args.get('src') or request.form.get('src', '')
    sub = request.args.get('sub') or request.form.get('sub', '')

    if not src:
        return jsonify({'error': 'الرجاء إدخال رابط M3U8'}), 400
    if not sub:
        return jsonify({'error': 'الرجاء إدخال رابط الترجمة (VTT/SRT)'}), 400

    job_id = f"burn_{int(time.time())}"

    with jobs_lock:
        jobs[job_id] = {
            'id': job_id,
            'src': src,
            'sub': sub,
            'status': 'processing',
            'progress': 0,
            'created': time.time(),
        }

    # تشغيل في Thread منفصل
    t = threading.Thread(target=burn_subtitles, args=(src, sub, job_id), daemon=True)
    t.start()

    if request.headers.get('Accept', '').startswith('text/html'):
        return jsonify({'job_id': job_id, 'status': 'processing'})
    return jsonify({'job_id': job_id, 'status': 'processing'})


@app.route('/status/<job_id>')
def status(job_id):
    """حالة العملية"""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(job)


@app.route('/output/<job_id>/<path:filename>')
def serve_output(job_id, filename):
    """تقديم الملفات المُنتجة (m3u8 + ts)"""
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    if not os.path.exists(job_dir):
        return jsonify({'error': 'Not found'}), 404
    resp = send_from_directory(job_dir, filename)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/output/<job_id>/index.m3u8')
def serve_m3u8(job_id):
    """تقديم m3u8 مع rewrite للروابط لتعمل عبر السيرفر"""
    job_dir = os.path.join(OUTPUT_DIR, job_id)
    m3u8_path = os.path.join(job_dir, 'index.m3u8')
    if not os.path.exists(m3u8_path):
        return jsonify({'error': 'Not found'}), 404

    with open(m3u8_path, 'r') as f:
        content = f.read()

    # إعادة كتابة روابط الأجزاء لتكون مطلقة
    base_url = f"/output/{job_id}/"
    lines = content.split('\n')
    for i, line in enumerate(lines):
        line = line.strip()
        if line and not line.startswith('#'):
            lines[i] = base_url + line

    resp = Response('\n'.join(lines), mimetype='application/vnd.apple.mpegurl')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/jobs')
def list_jobs():
    """قائمة كل العمليات"""
    with jobs_lock:
        return jsonify(list(jobs.values()))


# ═══════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════

GUI_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HLS Subtitle Burner</title>
<script src="https://cdn.tailwindcss.com"><\/script>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"><\/script>
<style>
*{font-family:'Segoe UI',Tahoma,sans-serif;box-sizing:border-box}
body{background:#0f0f1a;color:#e2e8f0;margin:0}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:#1a1a2e}
::-webkit-scrollbar-thumb{background:#6366f1;border-radius:3px}
.inp{background:#1a1a2e;border:1px solid #2d2d44;color:#e2e8f0;border-radius:10px;padding:10px 14px;width:100%;transition:all .2s;font-size:14px;direction:ltr;text-align:left}
.inp:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.2)}
.btn{padding:10px 24px;border-radius:10px;font-weight:600;cursor:pointer;transition:all .2s;border:none;font-size:14px;display:inline-flex;align-items:center;gap:8px}
.btn-go{background:linear-gradient(135deg,#ef4444,#f97316);color:#fff}
.btn-go:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(239,68,68,.4)}
.btn-go:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-copy{background:#1e1e2e;color:#a5b4fc;border:1px solid #2d2d44}
.btn-copy:hover{border-color:#6366f1}
.card{background:#12121f;border:1px solid #1e1e30;border-radius:16px;padding:24px}
.player-box{position:relative;background:#000;border-radius:12px;overflow:hidden;aspect-ratio:16/9}
.player-box video{width:100%;height:100%;display:block}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{width:20px;height:20px;border:3px solid rgba(255,255,255,.1);border-top-color:#ef4444;border-radius:50%;animation:spin .8s linear infinite;display:inline-block}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.fade-up{animation:fadeUp .4s ease}
.toast{position:fixed;bottom:24px;left:24px;padding:12px 20px;border-radius:12px;font-size:13px;font-weight:500;z-index:999;animation:fadeUp .3s}
</style>
</head>
<body>
<div class="max-w-4xl mx-auto p-4 md:p-8">

  <div class="flex items-center gap-3 mb-8">
    <div class="w-11 h-11 rounded-xl bg-gradient-to-br from-red-500 to-orange-500 flex items-center justify-center text-xl">🔥</div>
    <div>
      <h1 class="text-xl font-bold text-white">حارق الترجمة</h1>
      <p class="text-xs text-gray-500">HLS Subtitle Burner — ترجمة مدمجة لا تُزال</p>
    </div>
  </div>

  <div class="card mb-6">
    <h2 class="font-semibold text-sm mb-4 text-gray-300">الإعدادات</h2>
    <div class="grid md:grid-cols-2 gap-4 mb-4">
      <div>
        <label class="block text-xs text-gray-400 mb-1.5">رابط M3U8</label>
        <input id="m3u8" type="url" class="inp" placeholder="https://example.com/master.m3u8">
      </div>
      <div>
        <label class="block text-xs text-gray-400 mb-1.5">رابط الترجمة (VTT/SRT)</label>
        <input id="sub" type="url" class="inp" placeholder="https://example.com/subs/ar.vtt">
      </div>
    </div>
    <div class="flex flex-wrap gap-3">
      <button id="burnBtn" onclick="startBurn()" class="btn btn-go">
        🔥 حرق الترجمة
      </button>
      <button id="playBtn" onclick="playResult()" class="btn btn-copy" disabled>
        ▶️ تشغيل النتيجة
      </button>
      <button id="copyBtn" onclick="copyUrl()" class="btn btn-copy" disabled>
        📋 نسخ رابط البث
      </button>
    </div>
  </div>

  <!-- Progress -->
  <div id="progressCard" class="card mb-6 hidden fade-up">
    <div class="flex items-center justify-between mb-3">
      <span class="text-sm font-medium text-gray-300" id="progressText">جاري المعالجة...</span>
      <span class="text-xs text-gray-500" id="progressPct">0%</span>
    </div>
    <div class="w-full h-2 bg-gray-800 rounded-full overflow-hidden">
      <div id="progressBar" class="h-full bg-gradient-to-r from-red-500 to-orange-500 rounded-full transition-all duration-500" style="width:0%"></div>
    </div>
  </div>

  <!-- Player -->
  <div id="playerCard" class="card p-0 overflow-hidden mb-6 hidden fade-up">
    <div class="player-box">
      <video id="video" controls playsinline></video>
    </div>
  </div>

  <!-- Result URL -->
  <div id="urlCard" class="card hidden fade-up">
    <h3 class="font-semibold text-sm mb-3 text-green-400">✅ رابط البث بالترجمة المدمجة</h3>
    <div class="flex items-center gap-2">
      <input id="resultUrl" type="text" class="inp font-mono text-xs" readonly>
      <button onclick="copyUrl()" class="btn btn-copy px-3">📋</button>
    </div>
    <p class="text-xs text-gray-500 mt-2">هذا الرابط يحتوي على الترجمة مدمجة في الفيديو — لا يمكن إزالتها</p>
  </div>

</div>

<script>
let jobId = null;
let hls = null;
let pollInterval = null;

async function startBurn() {
  const m3u8 = document.getElementById('m3u8').value.trim();
  const sub = document.getElementById('sub').value.trim();
  if (!m3u8 || !sub) { showToast('أدخل الرابطين','err'); return; }

  const btn = document.getElementById('burnBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> جاري الحرق...';

  document.getElementById('progressCard').classList.remove('hidden');
  document.getElementById('playerCard').classList.add('hidden');
  document.getElementById('urlCard').classList.add('hidden');
  setProgress(0, 'جاري بدء المعالجة...');

  try {
    const r = await fetch('/burn?src=' + encodeURIComponent(m3u8) + '&sub=' + encodeURIComponent(sub));
    const data = await r.json();
    if (data.job_id) {
      jobId = data.job_id;
      pollStatus();
    } else {
      showToast(data.error || 'خطأ','err');
      btn.disabled = false;
      btn.innerHTML = '🔥 حرق الترجمة';
    }
  } catch(e) {
    showToast('فشل الاتصال بالسيرفر','err');
    btn.disabled = false;
    btn.innerHTML = '🔥 حرق الترجمة';
  }
}

function pollStatus() {
  pollInterval = setInterval(async () => {
    try {
      const r = await fetch('/status/' + jobId);
      const job = await r.json();
      if (job.status === 'done') {
        clearInterval(pollInterval);
        setProgress(100, 'تم بنجاح! ✅');
        const url = location.origin + '/output/' + jobId + '/index.m3u8';
        document.getElementById('resultUrl').value = url;
        document.getElementById('urlCard').classList.remove('hidden');
        document.getElementById('playBtn').disabled = false;
        document.getElementById('copyBtn').disabled = false;
        const btn = document.getElementById('burnBtn');
        btn.disabled = false;
        btn.innerHTML = '🔥 حرق ترجمة أخرى';
        showToast('تم حرق الترجمة بنجاح!','ok');
      } else if (job.status === 'error') {
        clearInterval(pollInterval);
        setProgress(0, '❌ ' + (job.error || 'خطأ'));
        showToast(job.error || 'خطأ في المعالجة','err');
        const btn = document.getElementById('burnBtn');
        btn.disabled = false;
        btn.innerHTML = '🔥 حرق الترجمة';
      } else {
        setProgress(30 + Math.random() * 40, 'جاري المعالجة... ffmpeg يعمل');
      }
    } catch(e) {}
  }, 3000);
}

function playResult() {
  if (!jobId) return;
  const url = location.origin + '/output/' + jobId + '/index.m3u8';
  document.getElementById('playerCard').classList.remove('hidden');

  if (hls) { hls.destroy(); hls = null; }
  const video = document.getElementById('video');

  if (Hls.isSupported()) {
    hls = new Hls();
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => video.play());
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
  }
}

function copyUrl() {
  const url = document.getElementById('resultUrl').value;
  if (!url) return;
  navigator.clipboard.writeText(url);
  showToast('تم النسخ!','ok');
}

function setProgress(pct, text) {
  document.getElementById('progressBar').style.width = pct + '%';
  document.getElementById('progressPct').textContent = Math.round(pct) + '%';
  document.getElementById('progressText').textContent = text;
}

function showToast(msg, type) {
  const colors = {ok:'background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.2)',err:'background:rgba(239,68,68,.15);color:#f87171;border:1px solid rgba(239,68,68,.2)'};
  const t = document.createElement('div');
  t.className = 'toast';
  t.style.cssText = colors[type] || colors.ok;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity='0'; setTimeout(()=>t.remove(),300); }, 3000);
}
<\/script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════
#  التشغيل
# ═══════════════════════════════════════════════════════════
if __name__ == '__main__':
    log("╔═════════════════════════════════════════════╗")
    log("║  HLS Subtitle Burner                        ║")
    log(f"║  http://{HOST}:{PORT}                       ║")
    log("╚═════════════════════════════════════════════╝")

    cleanup_workdir()
    app.run(host=HOST, port=PORT, threaded=True, debug=False)