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
import logging
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from flask import Flask, send_file, send_from_directory, request, jsonify, Response, redirect

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ─── CONFIG (P15: config.json ile taşınabilirlik, ENV override) ───
BASE_DIR = Path(__file__).parent

_CONFIG_DEFAULTS = {
    "host": "0.0.0.0",
    "port": 5002,
    "projeler_dir": "c:/Users/USER/Desktop/1/projeler",
    "cb_listings_url": "https://www.cb.com.tr/ilanlar?officeid=470&officeuserid=17983",
    "log_dir": "logs",
    "telemetry_file": "logs/telemetry.jsonl",
    "chat_rate_limit_per_min": 12,
}
CONFIG_FILE = BASE_DIR / "config.json"


def _load_config():
    cfg = dict(_CONFIG_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    for env_key, cfg_key in (("NEXA_PORT", "port"), ("NEXA_PROJELER_DIR", "projeler_dir"),
                             ("NEXA_HOST", "host"), ("NEXA_CB_URL", "cb_listings_url")):
        val = os.getenv(env_key)
        if val:
            if cfg_key == "port":
                val = int(val)
            cfg[cfg_key] = val
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return cfg


CFG = _load_config()

# ─── LOGGING (P16: döngüsel dosya logu) ───
LOG_DIR = BASE_DIR / CFG["log_dir"]
LOG_DIR.mkdir(exist_ok=True)
_log_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024,
                                   backupCount=3, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logging.basicConfig(level=logging.INFO,
                    handlers=[_log_handler, logging.StreamHandler()])
logger = logging.getLogger("nexa.app")

# ─── TELEMETRİ (E6: JSONL kaydı) ───
_telemetry_lock = threading.Lock()


_TELEMETRY_MAX_BYTES = 10 * 1024 * 1024


def telemetry(event: dict):
    try:
        line = json.dumps({"ts": datetime.now().isoformat(), **event}, ensure_ascii=False)
        with _telemetry_lock:
            tele_file = BASE_DIR / CFG["telemetry_file"]
            # O3: 10 MB üzeri JSONL'i .1'e rotate et, yenisini başlat
            if tele_file.exists() and tele_file.stat().st_size > _TELEMETRY_MAX_BYTES:
                try:
                    tele_file.replace(tele_file.with_suffix(".jsonl.1"))
                except OSError:
                    pass
            with open(tele_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass

# ─── SETUP ───
app = Flask(__name__)
PROJELER_DIR = Path(CFG["projeler_dir"])
STATIC_DIR = BASE_DIR / "static"
JSON_FILE = BASE_DIR / "projects_map.json"

STATIC_DIR.mkdir(exist_ok=True)

# ─── CHAT RATE LIMIT (production koruması) ───
_rate_lock = threading.Lock()
_rate_hits = {}


def _check_rate_limit(ip):
    now = time.time()
    with _rate_lock:
        # O5: hafıza temizliği — 60 saniyeden eski IP kayıtlarını sil
        for old_ip in [k for k, ts_list in _rate_hits.items()
                       if not ts_list or now - ts_list[-1] >= 60]:
            del _rate_hits[old_ip]
        hits = [t for t in _rate_hits.get(ip, []) if now - t < 60]
        if len(hits) >= int(CFG.get("chat_rate_limit_per_min", 12)):
            return False
        hits.append(now)
        _rate_hits[ip] = hits
    return True


# ─── CB LISTINGS SCRAPER ───
CB_LISTINGS_URL = CFG["cb_listings_url"]
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

from nexa_ai_engine import process_nexa_query, extract_keywords_and_projects
from nexa_rag import (cognitive_chat, _find_project_by_name, DOCS_DIR as NEXA_DOCS_DIR,
                      DB_PATH as NEXA_DB_PATH, generate_all_project_summaries,
                      get_project_summary)

@app.route("/api/projects", methods=["GET"])
def api_projects():
    if JSON_FILE.exists():
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"success": True, "data": data})
    return jsonify({"success": False, "message": "projects_map.json bulunamadı"})

@app.route("/api/nexa-ai-chat", methods=["POST"])
def api_nexa_ai_chat():
    client_ip = request.remote_addr or "?"
    if not _check_rate_limit(client_ip):
        telemetry({"event": "rate_limited", "ip": client_ip})
        return jsonify({"success": False,
                        "response": "Çok hızlı soru gönderiyorsunuz. Lütfen birkaç saniye bekleyip tekrar deneyin."}), 429

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "response": "Lütfen bir soru yazın."}), 400
    if len(message) > 2000:
        return jsonify({"success": False, "response": "Soru çok uzun (en fazla 2000 karakter)."}), 400
    history = data.get("history") or []
    if not isinstance(history, list) or len(history) > 20:
        history = []

    t0 = time.time()
    try:
        result = process_nexa_query(message)
    except Exception as e:
        logger.exception("process_nexa_query hatasi")
        telemetry({"event": "engine_error", "ip": client_ip, "err": str(e)[:200]})
        return jsonify({"success": False,
                        "response": "Sistem kısa süreliğine meşgul. Lütfen bir dakika sonra tekrar deneyin."}), 500
    cards = result.get("projects", [])
    mode = "heuristic"

    project = None
    try:
        named = extract_keywords_and_projects(message)
        if len(named) == 1:
            project = _find_project_by_name(named[0])
    except Exception:
        project = None

    try:
        rag_reply = cognitive_chat(message, project=project, history=history)
        if rag_reply:
            mode = "cognitive-rag"
            payload = {
                "success": True,
                "response": rag_reply,
                "projects": cards,
                "mode": mode,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
            telemetry({"event": "chat", "ip": client_ip, "mode": mode,
                       "msg": message[:120], "projects": [c.get("title") for c in cards],
                       "elapsed_ms": payload["elapsed_ms"]})
            return jsonify(payload)
    except Exception as e:
        logger.exception("cognitive_chat hatasi — heuristic'e dusuluyor")

    payload = {
        "success": True,
        "response": result.get("response", "Nexa AI Analizi tamamlandı."),
        "projects": cards,
        "mode": mode,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
    telemetry({"event": "chat", "ip": client_ip, "mode": mode,
               "msg": message[:120], "projects": [c.get("title") for c in cards],
               "elapsed_ms": payload["elapsed_ms"]})
    return jsonify(payload)


@app.route("/api/track", methods=["POST"])
def api_track():
    """Frontend olay telemetrisi: proje kartı tıklama, WhatsApp tıklama, lead formu."""
    data = request.get_json(silent=True) or {}
    ev = data.get("event") or "click"
    telemetry({"event": f"ui_{ev}", "ip": request.remote_addr or "?",
               "project": data.get("project") or "",
               "target": data.get("target") or "",
               "extra": (data.get("extra") or {}) if isinstance(data.get("extra"), dict) else {}})
    return jsonify({"success": True})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "nexa-cb-vip",
                    "time": datetime.now().isoformat(),
                    "port": CFG["port"]})

@app.route("/api/nexa-documents", methods=["GET"])
def api_nexa_documents():
    project_id = request.args.get("project_id", type=int)
    folder = request.args.get("folder", type=str)  # D1: kategori adı (string)
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{NEXA_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        if project_id:
            rows = conn.execute(
                "SELECT id, project_id, doc_type, title, file_url, category FROM documents WHERE project_id = ? ORDER BY id",
                (project_id,)).fetchall()
        elif folder:
            rows = conn.execute(
                "SELECT id, project_id, doc_type, title, file_url, category FROM documents WHERE (doc_type='doc' OR doc_type='html') AND file_url LIKE '/static/documents/%' AND category LIKE ? ORDER BY project_id, id",
                (f"%{folder}%",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, doc_type, title, file_url, category FROM documents ORDER BY project_id, id").fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            url = d.get("file_url") or "#"
            if url.startswith("/static/documents/"):
                url = url.replace("/static/documents/", "/nexa-docs/", 1)
            d["download_url"] = url
            out.append(d)
        return jsonify({"success": True, "count": len(out), "documents": out})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/nexa-summaries", methods=["GET"])
def api_nexa_summaries():
    try:
        from nexa_rag import _load_summaries
        data = _load_summaries()
        items = [{"project_id": v.get("project_id"), "title": k, "summary": v.get("summary", "")}
                 for k, v in data.items()]
        return jsonify({"success": True, "count": len(items), "summaries": items})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/nexa-docs/<path:filename>")
def nexa_docs_file(filename):
    base = NEXA_DOCS_DIR.resolve()
    target = (base / filename).resolve()
    if base not in target.parents and target != base:
        return "Erişim engellendi", 403
    if not target.exists():
        return "Dosya bulunamadı", 404
    resp = send_file(str(target))
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

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
    resp = send_from_directory(STATIC_DIR, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

# ─── P15: güvenlik başlıkları (tüm yanıtlara) ───
@app.after_request
def _add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# ─── JSON hata handler'ları (yalnızca /api/ prefix'li istekler) ───
@app.errorhandler(404)
def _handle_404(err):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "message": "Uç nokta bulunamadı"}), 404
    return "Sayfa bulunamadı", 404

@app.errorhandler(500)
def _handle_500(err):
    logger.exception("Sunucu hatasi")
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "message": "Sunucu hatası"}), 500
    return "Sunucu hatası", 500

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
        base = PROJELER_DIR.resolve()
        target = (base / rel).resolve()
        # O2: prefix yerine gerçek ebeveyn kontrolü (resolve() symlink'leri de çözer)
        if target != base and base not in target.parents:
            logger.warning("Yol disari cikma denemesi: %s", path_arg)
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

    # P3: MP4 seçim önceliği — 1) tanıtım, 2) slayt, 3) en büyük dosya (500KB filtre korunur)
    _PRIORITY_WORDS_1 = ("tanitim", "tanıtım", "intro", "main", "ana")
    _PRIORITY_WORDS_2 = ("slayt", "slideshow", "slaytlar")

    def _mp4_priority(f: Path):
        name = f.stem.lower()
        for i, kw in enumerate(_PRIORITY_WORDS_1):
            if kw in name:
                return (0, i, -f.stat().st_size)
        for i, kw in enumerate(_PRIORITY_WORDS_2):
            if kw in name:
                return (1, i, -f.stat().st_size)
        return (2, 0, -f.stat().st_size)

    mp4_files = list(target_dir.glob("*.mp4")) if target_dir.exists() else []
    mp4_files.sort(key=_mp4_priority)
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

    # O4/B8: gerçek özet + fiyat/oda verisiyle zengin danışman notu
    summary = ""
    try:
        summary = get_project_summary(title)
    except Exception:
        summary = ""
    pricing = {}
    try:
        pf = BASE_DIR / "nexa_portfolio_data.json"
        if pf.exists():
            pdata = json.loads(pf.read_text(encoding="utf-8"))
            pool = pdata if isinstance(pdata, list) else pdata.get("projects", [])
            for item in pool:
                if str(item.get("title")) == str(title) or str(item.get("name")) == str(title):
                    pricing = item
                    break
    except Exception:
        pricing = {}

    price_display = pricing.get("price_display") or "Fiyat için danışmanımızdan bilgi alınız"
    room_info = pricing.get("room_info") or "Daire tipleri için danışmanımızdan bilgi alınız"
    loc = pricing.get("location") or project.get("location") or "Prestij Lokasyonu"

    report = (
        f"DANISMAN NOTU — {title}\n"
        "===============================================\n\n"
        "📌 PROJE ÖZETİ\n"
        f"• Proje: {title}\n"
        f"• Bölge: {loc}\n"
        f"• Fiyat: {price_display}\n"
        f"• Daire Tipleri: {room_info}\n"
        f"• Geliştirici: Coldwell Banker VIP\n\n"
    )
    if summary:
        report += f"💡 NEXA AI PROJE ÖZETİ\n{summary}\n\n"
    else:
        report += (
            "💡 NEXA AI DEĞERLENDİRMESİ\n"
            "Proje için henüz otomatik özet üretilmedi; ayrıntılı bilgi için "
            "Suzanne Hanım ile iletişime geçiniz.\n\n"
        )
    report += "📞 0535 489 56 56\nWhatsApp üzerinden anlık bilgi alabilirsiniz."
    return jsonify({"success": True, "report": report})

# ─── ANA SAYFA: /site vitrinine yönlendir (eski galeri şablonu kaldırıldı, P14) ───
@app.route("/")
def index():
    return redirect("/site", code=302)

if __name__ == "__main__":
    logger.info("[START] NEXA CB VIP — http://localhost:%s", CFG["port"])
    threading.Thread(target=generate_all_project_summaries, daemon=True, name="auto-summaries").start()
    app.run(host=CFG["host"], port=int(CFG["port"]), debug=False, use_reloader=False)
