#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COLDWELL BANKER VIP - CLOUD MEDIA STREAMING SYSTEM (FOLDER 3)
Zero permission walls, zero Google login prompts, HTTP 206 fast video streaming
Folder: c:/Users/USER/Desktop/3
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from flask import Flask, render_template_string, send_file, send_from_directory, request, jsonify, Response

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ─── SETUP ───
app = Flask(__name__)
BASE_DIR = Path(__file__).parent
PROJELER_DIR = Path("c:/Users/USER/Desktop/1/projeler")
STATIC_DIR = BASE_DIR / "static"
JSON_FILE = BASE_DIR / "projects_map.json"

STATIC_DIR.mkdir(exist_ok=True)

# ─── CB LISTINGS SCRAPER ───
CB_LISTINGS_URL = "https://www.cb.com.tr/ilanlar?officeid=470&officeuserid=17983"
CB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_listings_cache = {"data": [], "ts": 0.0}
_listings_lock = threading.Lock()

def fetch_cb_listings() -> list:
    if _requests is None or BeautifulSoup is None:
        return []
    try:
        response = _requests.get(CB_LISTINGS_URL, headers=CB_HEADERS, timeout=15)
        if response.status_code != 200:
            return []
        soup = BeautifulSoup(response.content, "html.parser")
        listings = []
        cards = soup.select(".card.locationDiv") or soup.select(".cb-list-item")
        for card in cards:
            try:
                title_el = card.select_one(".cb-list-item-info h2") or card.select_one(".card-title")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title: continue
                price_el = card.select_one(".feature-item .text-primary") or card.select_one("span.h5.text-primary")
                price = price_el.get_text(strip=True) if price_el else ""
                link_el = card.select_one(".cb-list-img-container a") or card.select_one("a.title")
                link = link_el["href"] if link_el else "#"
                if link and not link.startswith("http"):
                    link = "https://www.cb.com.tr" + link
                img_el = card.select_one(".cb-list-img-container img") or card.select_one("img.card-img-top")
                img_url = img_el.get("src") if img_el else "https://via.placeholder.com/400x300"
                listings.append({"title": title, "price": price, "img": img_url, "link": link})
            except Exception:
                continue
        return listings
    except Exception:
        return []

def _refresh_cb_listings_bg():
    def _run():
        try:
            data = fetch_cb_listings()
            with _listings_lock:
                _listings_cache["data"] = data
                _listings_cache["ts"] = time.time()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

@app.route("/api/listings", methods=["GET"])
def api_listings():
    now = time.time()
    if now - _listings_cache["ts"] >= 300:
        _refresh_cb_listings_bg()
    return jsonify({"success": True, "data": _listings_cache["data"]})

from nexa_ai_engine import process_nexa_query

@app.route("/api/projects", methods=["GET"])
def api_projects():
    if JSON_FILE.exists():
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"success": True, "data": data})
    return jsonify({"success": False, "message": "projects_map.json bulunamadı"})

@app.route("/api/nexa-ai-chat", methods=["POST"])
def api_nexa_ai_chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    if not message:
        return jsonify({"success": False, "response": "Lütfen bir soru yazın."}), 400
    
    result = process_nexa_query(message)
    return jsonify(result)

@app.route("/site")
def site():
    site_file = BASE_DIR / "site.html"
    if not site_file.exists():
        return "site.html bulunamadı", 404
    resp = send_file(str(site_file))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)

@app.route("/projeler/<path:filename>")
def projeler_files(filename):
    return send_from_directory(PROJELER_DIR, filename)

# ─── HIGH PERFORMANCE HTTP 206 CLOUD VIDEO STREAMING ENDPOINT ───
def stream_file_response(path: Path, mimetype: str):
    file_size = path.stat().st_size
    range_header = request.headers.get('Range', None)

    if not range_header:
        return send_file(str(path), mimetype=mimetype)

    byte1, byte2 = 0, None
    m = re.search(r'bytes=(\d+)-(\d*)', range_header)
    if m:
        g = m.groups()
        byte1 = int(g[0])
        if g[1]:
            byte2 = int(g[1])

    length = file_size - byte1
    if byte2 is not None:
        length = byte2 - byte1 + 1

    chunk_size = 1024 * 1024  # 1MB chunks
    def generate():
        with open(path, 'rb') as f:
            f.seek(byte1)
            remaining = length
            while remaining > 0:
                read_bytes = min(chunk_size, remaining)
                data = f.read(read_bytes)
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), 206, mimetype=mimetype, content_type=mimetype, direct_passthrough=True)
    resp.headers.add('Content-Range', f'bytes {byte1}-{byte1 + length - 1}/{file_size}')
    resp.headers.add('Accept-Ranges', 'bytes')
    resp.headers.add('Content-Length', str(length))
    return resp

@app.route("/file")
def file_serve():
    path_arg = request.args.get("path", "")
    if not path_arg:
        return "path parametresi gerekli", 400
    try:
        rel = os.path.normpath(path_arg).lstrip("/\\")
        if rel.lower().startswith("projeler" + os.sep) or rel.lower().startswith("projeler/"):
            rel = rel[len("projeler"):].lstrip("/\\")
        target = (PROJELER_DIR / rel).resolve()
        if not str(target).startswith(str(PROJELER_DIR.resolve())):
            return "Geçersiz yol", 400
        if not target.exists() or not target.is_file():
            return "Dosya bulunamadı", 404
    except Exception:
        return "Hatalı yol", 400

    suffix = target.suffix.lower()
    if suffix == ".mp4":
        return stream_file_response(target, "video/mp4")
    if suffix == ".pdf":
        return send_file(str(target), mimetype="application/pdf")
    return send_file(str(target))

@app.route("/stream/video/<project_id>")
def stream_video(project_id):
    if not JSON_FILE.exists():
        return "Map file not found", 404

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        projects = json.load(f)

    project = next((p for p in projects if str(p.get("id")) == str(project_id) or str(p.get("db_id")) == str(project_id)), None)
    if not project:
        return "Project not found", 404

    folder_name = project.get("folder_name")
    target_dir = PROJELER_DIR / folder_name

    mp4_files = list(target_dir.glob("*.mp4")) if target_dir.exists() else []
    real_mp4 = None
    for file in mp4_files:
        if file.stat().st_size > 500 * 1024:
            real_mp4 = file
            break

    if not real_mp4 and mp4_files:
        real_mp4 = mp4_files[0]

    if not real_mp4 or not real_mp4.exists():
        return "Video file not found", 404

    return stream_file_response(real_mp4, "video/mp4")

@app.route("/api/projects/<project_id>/report")
def api_project_report(project_id):
    if not JSON_FILE.exists():
        return jsonify({"success": False, "message": "projects_map.json bulunamadı"}), 404
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        projects = json.load(f)
    project = next((p for p in projects if str(p.get("id")) == str(project_id) or str(p.get("db_id")) == str(project_id)), None)
    if not project:
        return jsonify({"success": False, "message": "Proje bulunamadı"}), 404

    title = project.get("title", "Prestij Projesi")
    report = (
        f"DANISMAN NOTU — {title}\n"
        "===============================================\n\n"
        "📌 PROJE ÖZETİ\n"
        f"• Proje: {title}\n"
        f"• Bölge: Ankara / Prestij Lokasyonu\n"
        f"• Geliştirici: Coldwell Banker VIP\n"
        f"• Durum: Öne Çıkan Seçkin Proje\n\n"
        "💡 NEXA AI DEĞERLENDİRMESİ\n"
        "Lokasyon, yapı kalitesi ve bölge değer artış potansiyeli açısından "
        "yüksek yatırım değeri taşıyan bir projedir. Detaylı fiyat, daire tipi "
        "ve ödeme planı bilgisi için Suzanne Hanım ile iletişime geçiniz.\n\n"
        "📞 0535 489 56 56\n"
        "WhatsApp üzerinden anlık bilgi alabilirsiniz."
    )
    return jsonify({"success": True, "report": report})

# ─── FRONTEND TEMPLATE ───
MAIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CB VIP Projeleri | Cloud Stream Galerisi (Klasör 3)</title>
    <meta name="description" content="Coldwell Banker VIP - Bulut Altyapılı Kesintisiz Proje Oynatıcısı">
    
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">

    <style>
        :root {
            --bg: #0B0F17;
            --surface: #151D2A;
            --surface-card: #1C2638;
            --accent-cyan: #06B6D4;
            --accent-blue: #3B82F6;
            --text-primary: #F8FAFC;
            --text-secondary: #94A3B8;
            --border: rgba(255, 255, 255, 0.08);
            --radius: 20px;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg); color: var(--text-primary); line-height: 1.6; }

        .navbar {
            position: sticky; top: 0; z-index: 100;
            background: rgba(11, 15, 23, 0.85); backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border);
            padding: 1.2rem 5%; display: flex; justify-content: space-between; align-items: center;
        }

        .logo { font-family: 'Outfit', sans-serif; font-size: 22px; font-weight: 700; color: var(--accent-cyan); display: flex; align-items: center; gap: 10px; text-decoration: none; }
        .cloud-badge { background: rgba(6, 182, 212, 0.15); color: var(--accent-cyan); border: 1px solid var(--accent-cyan); padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }

        .hero { text-align: center; padding: 4rem 1rem 2rem; max-width: 900px; margin: 0 auto; }
        .hero h1 { font-family: 'Outfit', sans-serif; font-size: 42px; font-weight: 700; margin-bottom: 1rem; }
        .hero p { color: var(--text-secondary); font-size: 17px; margin-bottom: 2rem; }

        .search-bar { max-width: 500px; margin: 0 auto 2rem; position: relative; }
        .search-bar input { width: 100%; padding: 1rem 1rem 1rem 3rem; background: var(--surface); border: 1px solid var(--border); border-radius: 30px; color: #fff; font-size: 15px; outline: none; }
        .search-bar i { position: absolute; left: 1.2rem; top: 50%; transform: translateY(-50%); color: var(--text-secondary); }

        .projects-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 28px; padding: 0 5%; max-width: 1400px; margin: 0 auto 4rem; }

        .project-card { background: var(--surface-card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; display: flex; flex-direction: column; transition: transform 0.3s ease; }
        .project-card:hover { transform: translateY(-6px); border-color: rgba(6, 182, 212, 0.4); }

        .card-media-wrapper { position: relative; width: 100%; height: 230px; background: #000; overflow: hidden; }
        .card-media-wrapper video { width: 100%; height: 100%; object-fit: cover; }
        
        .card-body { padding: 1.4rem; display: flex; flex-direction: column; gap: 12px; flex-grow: 1; }
        .card-title { font-family: 'Outfit', sans-serif; font-size: 19px; font-weight: 600; }
        .card-meta { display: flex; gap: 15px; font-size: 13px; color: var(--text-secondary); }

        .card-actions { display: flex; gap: 10px; margin-top: auto; }
        .btn { flex: 1; padding: 10px 14px; border-radius: 12px; font-size: 13px; font-weight: 600; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px; text-decoration: none; }
        .btn-primary { background: linear-gradient(135deg, #06B6D4 0%, #3B82F6 100%); color: #fff; }
        .btn-secondary { background: var(--surface); color: #fff; border: 1px solid var(--border); }

        /* Modal */
        .modal { display: none; position: fixed; inset: 0; z-index: 2000; background: rgba(0,0,0,0.85); backdrop-filter: blur(10px); align-items: center; justify-content: center; }
        .modal.active { display: flex; }
        .modal-content { background: var(--surface-card); border-radius: var(--radius); width: 90%; max-width: 950px; height: 85vh; display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--border); }
        .modal-header { padding: 1rem 1.5rem; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
        .modal-body { flex-grow: 1; background: #000; }
        .modal-body iframe, .modal-body video { width: 100%; height: 100%; border: none; }
        .close-btn { background: none; border: none; color: #fff; font-size: 24px; cursor: pointer; }
    </style>
</head>
<body>
    <header class="navbar">
        <a href="/" class="logo"><i class="fa-solid fa-cloud"></i> COLDWELL BANKER VIP</a>
        <div style="display:flex; gap:12px;">
            <a href="/site" class="cloud-badge" style="text-decoration:none;"><i class="fa-solid fa-globe"></i> Kişisel Portal (/site)</a>
            <span class="cloud-badge"><i class="fa-solid fa-bolt"></i> Bulut Stream (Sıfır İzin Engeli)</span>
        </div>
    </header>

    <section class="hero">
        <h1>CB VIP Bulut Medya Galerisi</h1>
        <p>Erişim izni engeli ve Google giriş zorunluluğu olmayan kesintisiz video & sunum altyapısı.</p>
        <div class="search-bar">
            <i class="fa-solid fa-search"></i>
            <input type="text" id="searchInput" placeholder="Proje ara..." onkeyup="filterProjects()">
        </div>
    </section>

    <main class="projects-grid" id="projectsGrid">
        {% for project in projects %}
        <div class="project-card" data-title="{{ project.title|lower }}">
            <div class="card-media-wrapper">
                <video controls preload="metadata" poster="{{ project.thumbnail }}">
                    <source src="{{ project.cloud_video_url }}" type="video/mp4">
                </video>
            </div>
            <div class="card-body">
                <h3 class="card-title">{{ project.title }}</h3>
                <div class="card-meta">
                    <span><i class="fa-solid fa-cloud-arrow-up" style="color:var(--accent-cyan);"></i> Bulut Stream Active</span>
                    <span><i class="fa-solid fa-shield-check" style="color:#22C55E;"></i> Halka Açık</span>
                </div>
                <div class="card-actions">
                    {% if project.has_presentation %}
                    <button class="btn btn-secondary" onclick="openPdfModal('{{ project.title }}', '/{{ project.presentations[0].path }}')"><i class="fa-solid fa-file-pdf"></i> Sunum PDF</button>
                    {% endif %}
                    <button class="btn btn-primary" onclick="openVideoModal('{{ project.title }}', '{{ project.cloud_video_url }}')"><i class="fa-solid fa-expand"></i> Tam Ekran İzle</button>
                </div>
            </div>
        </div>
        {% endfor %}
    </main>

    <div class="modal" id="mediaModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modalTitle">Medya Önizleme</h3>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body" id="modalContainer"></div>
        </div>
    </div>

    <script>
        function openPdfModal(title, pdfPath) {
            document.getElementById('modalTitle').innerText = title + ' — PDF Sunum';
            document.getElementById('modalContainer').innerHTML = `<iframe src="${pdfPath}"></iframe>`;
            document.getElementById('mediaModal').classList.add('active');
        }

        function openVideoModal(title, videoUrl) {
            document.getElementById('modalTitle').innerText = title + ' — Bulut Oynatıcı';
            document.getElementById('modalContainer').innerHTML = `<video controls autoplay src="${videoUrl}" style="width:100%; height:100%;"></video>`;
            document.getElementById('mediaModal').classList.add('active');
        }

        function closeModal() {
            document.getElementById('mediaModal').classList.remove('active');
            document.getElementById('modalContainer').innerHTML = '';
        }

        function filterProjects() {
            const query = document.getElementById('searchInput').value.toLowerCase();
            const cards = document.querySelectorAll('.project-card');
            cards.forEach(card => {
                const title = card.getAttribute('data-title');
                if (title.includes(query)) {
                    card.style.display = 'flex';
                } else {
                    card.style.display = 'none';
                }
            });
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    if JSON_FILE.exists():
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            projects = json.load(f)
    else:
        projects = []
    return render_template_string(MAIN_TEMPLATE, projects=projects)

if __name__ == "__main__":
    print("[START] COLDWELL BANKER VIP - CLOUD STREAM SYSTEM (FOLDER 3)")
    print("[PORT] Sunucu Baslatiliyor: http://localhost:5002")
    app.run(host="0.0.0.0", port=5002, debug=False, use_reloader=False)
