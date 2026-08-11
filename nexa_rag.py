"""
NEXA RAG — Bilişsel Gayrimenkul Zekası (NEXA PRIME v2 ENTEPRISE seviyesi)
- NEXA PRIME veritabanındaki proje metadata + doküman chunk'larına RAG yapar
- Gemini çoklu-anahtar çoklu-model fallback ile cevap üretir (NEXA PRIME mimarisi)
- Bulut yoksa yerel Ollama, o da yoksa None döner (çağıran taraf heuristic'e düşer)
"""
import os
import re
import json
import time
import sqlite3
import logging
import threading
from pathlib import Path

logger = logging.getLogger("nexa.rag")

NEXA_ROOT = Path(r"C:\Users\USER\Desktop\NEXA_PRIME_v2_ENTERPRISE")
DB_PATH = NEXA_ROOT / "nexa_database.db"
DOCS_DIR = NEXA_ROOT / "static" / "documents"
SUMMARIES_FILE = Path(r"C:\Users\USER\Desktop\3\nexa_project_summaries.json")

# ─── KOTA / PERFORMANS YÖNETİMİ (P8/P9) ───
_key_cooldowns = {}          # key -> süre sonu (429/401 tespitinde 60 sn yasak)
_last_good_model = {}        # key -> son başarılı model (öncelikle dene)
_reply_cache = {}            # normalize sorgu -> (yanıt, zaman)
_cache_lock = threading.Lock()
_save_lock = threading.Lock()
_global_ctx_cache = {}       # E9: global context (60 sn TTL)
REPLY_TTL = 300              # 5 dk
KEY_COOLDOWN = 60
GLOBAL_CTX_TTL = 60          # saniye

FALLBACK_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]

_dead_models = set()  # 404 veren (artık erişilemeyen) modeller

CONTACT_LINE = "Detaylı sunum, güncel fiyat listesi ve parsel raporları için **0535 489 56 56** WhatsApp hattından ulaşabilirsiniz."


def _read_api_keys():
    keys = []
    env_keys = os.getenv("GEMINI_API_KEYS", "")
    if env_keys:
        keys += [k.strip() for k in env_keys.split(",") if k.strip()]
    env_file = NEXA_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEYS="):
                keys += [k.strip() for k in line.split("=", 1)[1].split(",") if k.strip()]
    if not keys:
        try:
            from app.core.config import settings
        except Exception:
            return []
        keys = settings.api_keys_list
    seen, out = set(), []
    for k in keys:
        if k and k not in seen and "YENI_API_KEY" not in k and len(k) >= 10:
            seen.add(k)
            out.append(k)
    return out


def _merge_summary(name, project_id, summary):
    data = _load_summaries()
    data[name] = {"summary": summary, "project_id": project_id, "ts": time.time()}
    return data


def _load_db():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


# ─── BELGE BESLEME KALİTESİ (RAG feed) ───
# Çözülememiş PDF metni: kontrol karakterleri + Latin-1/IPA/Greek bloğunda 4+ ardışık karakter
# (glyph'ler: ʤʡʦʬ...; \ufffd = değiştirme karakteri, bozuk font çıktısı)
_GARBAGE_RE = re.compile(r"[\u0001-\u0008\u000b\u000c\u000e-\u001f\u007f-\u02ff\ufffd]{4,}")
_MIN_PIECE = 12


def _glyph_ratio(s):
    return sum(1 for c in s if "\u0080" <= c <= "\u02ff" or c == "\ufffd") / max(len(s), 1)


def _clean_chunk(text):
    """Çöp glyph bloklarını atıp anlamlı parçaları korur; tamamı çöpse None döner."""
    if not text:
        return None
    if not _GARBAGE_RE.search(text):
        return text
    pieces = []
    for p in _GARBAGE_RE.split(text):
        p = p.strip()
        if len(p) >= _MIN_PIECE and _glyph_ratio(p) < 0.4:
            pieces.append(p)
    return " ".join(pieces) or None


# SUNUM/ÖDEME/FİYAT içeren belgeler önce, IBAN/BANKA/SÖZLEŞME/HİSSE en son
_DOC_PRIORITY_SQL = """
    CASE
        WHEN UPPER(d.title) LIKE '%SUNUM%' OR UPPER(d.title) LIKE '%ÖDEME%'
          OR UPPER(d.title) LIKE '%FIYAT%' OR UPPER(d.title) LIKE '%FİYAT%' THEN 0
        WHEN UPPER(d.title) LIKE '%IBAN%' OR UPPER(d.title) LIKE '%BANKA%'
          OR UPPER(d.title) LIKE '%SÖZLEŞME%' OR UPPER(d.title) LIKE '%SOZLESME%'
          OR UPPER(d.title) LIKE '%HİSSE%' OR UPPER(d.title) LIKE '%HISSE%' THEN 2
        ELSE 1
    END"""


def build_project_context(db_id, query=None):
    db = _load_db()
    db.row_factory = sqlite3.Row
    try:
        p = dict(db.execute("SELECT * FROM projects WHERE id = ?", (db_id,)).fetchone())
        ptype = (f"BİREYSEL PORTFÖY İLANI ({p['listing_type'] or 'İlan'})"
                 if p.get("is_portfolio") else "MARKALI PROJE")
        meta = "\n".join([
            "=== PORTFÖY / PROJE METADATA ===",
            f"[AD]: {p['name']}",
            f"[EKOSİSTEM]: {ptype}",
            f"[GAYRİMENKUL TİPİ]: {p.get('property_category') or 'Belirtilmedi'}",
            f"[FİYAT / BEDEL]: {p.get('price_display') or 'Fiyat Belirtilmedi'}",
            f"[ODA / YAPI]: {p.get('room_info') or 'Belirtilmedi'}",
            f"[NET / BRÜT ALAN]: {p.get('net_gross_area') or 'Belirtilmedi'}",
            f"[LOKASYON]: {p.get('location') or ''} ({p.get('ilce') or ''} / {p.get('il') or ''})",
            f"[ADA/PARSEL]: {p.get('ada_no') or '-'}/{p.get('parsel_no') or '-'} (TKGM: {'Evet' if p.get('tkgm_verified') else 'Hayır'})",
            f"[AÇIKLAMA]: {p.get('description') or 'Açıklama girilmedi.'}",
        ])
        chunks = []
        raw_chunks = []
        for r in db.execute(f"""
            SELECT d.title, d.category, dc.chunk_text
            FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
            WHERE d.project_id = ? AND LENGTH(TRIM(dc.chunk_text)) > 0
            ORDER BY {_DOC_PRIORITY_SQL}, d.id, dc.id
            LIMIT 24
        """, (db_id,)):
            txt = _clean_chunk(r["chunk_text"])
            if txt is None:
                continue
            raw_chunks.append((f"[{r['category'] or 'Belge'} - {r['title']}]: {txt[:400]}", txt))
        chosen = _rank_chunks(query, raw_chunks) if query else raw_chunks
        chunks = []
        for line, _txt in chosen[:12]:
            chunks.append(line)
        if not chunks:
            chunks.append("[BELGE]: Bu proje için sistemde henüz anlamlı doküman içeriği bulunmuyor (belge yüklenmemiş veya çözümlenememiş olabilir).")
        return meta + "\n" + "\n\n".join(chunks)
    finally:
        db.close()


def build_global_context():
    now = time.time()
    with _cache_lock:
        cached = _global_ctx_cache.get("ctx")
        if cached and now - cached[1] < GLOBAL_CTX_TTL:
            return cached[0]
    db = _load_db()
    db.row_factory = sqlite3.Row
    try:
        projects = db.execute("""
            SELECT id, name, location, il, ilce, mahalle, description, ada_no, parsel_no,
                   tkgm_verified, is_portfolio, listing_type, property_category,
                   price_display, room_info, net_gross_area
            FROM projects WHERE COALESCE(is_portfolio,0) = 0 ORDER BY id ASC
        """).fetchall()
        if not projects:
            return "Sistemde henüz kayıtlı proje bulunmamaktadır."
        parts = []
        for proj in projects:
            ptype = (f"BİREYSEL PORTFÖY ({proj['listing_type'] or 'İlan'})"
                     if proj['is_portfolio'] else "MARKALI PROJE")
            specs = (f"Kategori: {proj['property_category'] or '-'}, "
                     f"Oda: {proj['room_info'] or '-'}, Alan: {proj['net_gross_area'] or '-'}")
            loc = proj['location'] or f"{proj['ilce'] or ''} / {proj['il'] or ''}"
            chunks = []
            for r in db.execute(f"""
                SELECT d.title, d.doc_type, dc.chunk_text
                FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
                WHERE d.project_id = ? AND LENGTH(TRIM(dc.chunk_text)) > 0
                ORDER BY {_DOC_PRIORITY_SQL}, d.id, dc.id
                LIMIT 16
            """, (proj['id'],)):
                txt = _clean_chunk(r["chunk_text"])
                if txt is None:
                    continue
                chunks.append(f"  • [{r['doc_type'].upper()} - {r['title']}]: {txt[:260]}")
                if len(chunks) >= 8:
                    break
            if not chunks:
                chunks = ["  • (Henüz taranmış özel belge bulunmuyor)"]
            parts.append("\n".join([
                "---",
                f"İLAN/PROJE ID: {proj['id']} [{ptype}]",
                f"AD: {proj['name']}",
                f"FİYAT: {proj['price_display'] or 'Fiyat Belirtilmedi'} | {specs}",
                f"LOKASYON: {loc} (İl: {proj['il'] or '-'}, İlçe: {proj['ilce'] or '-'}, Mahalle: {proj['mahalle'] or '-'})",
                f"ADA/PARSEL: {proj['ada_no'] or '-'}/{proj['parsel_no'] or '-'} (TKGM Onay: {'Evet' if proj['tkgm_verified'] else 'Hayır'})",
                f"AÇIKLAMA: {proj['description'] or 'Açıklama yok.'}",
                "BELGE/VERİ ÖZETLERİ:",
                "\n".join(chunks),
            ]))
        result = "\n\n".join(parts)
        with _cache_lock:
            _global_ctx_cache["ctx"] = (result, now)
        return result
    finally:
        db.close()


# ─── SORGU ODAKLI CHUNK SEÇİMİ (Y4 pragmatik iyileştirme) ───
def _query_tokens(query):
    """Sorgudan anlamlı Türkçe kelimeleri çıkarır (stopword'süz, sayısal filtreli)."""
    if not query:
        return []
    stop = {"bir", "bana", "bize", "en", "iyi", "uygun", "var", "mı", "mu", "mi",
            "olan", "olanlar", "proje", "projeler", "projesini", "anlat", "söyle",
            "hangi", "hangi", "istiyorum", "istiyoruz", "arayıorum", "arıyorum",
            "lütfen", "acaba", "ile", "ve", "veya", "ne", "nasıl", "nerede",
            "lazım", "gerek", "bütçe", "bütçem", "bütçemiz"}
    tokens = re.findall(r"[a-zçğıöşü]{3,}", (query or "").lower())
    return [t for t in tokens if t not in stop]


def _rank_chunks(query, candidates):
    """Chunk'ları sorgu token'larının geçiş sıklığına göre sıralar (üst 12 seçilir)."""
    tokens = _query_tokens(query)
    if not tokens or len(candidates) <= 12:
        return candidates
    scored = []
    for pair in candidates:
        if isinstance(pair, tuple):
            text = pair[1]
        else:
            text = pair
        tl = (text or "").lower()
        score = sum(tl.count(t) for t in tokens)
        scored.append((score, pair))
    scored.sort(key=lambda x: -x[0])
    out = [pair for s, pair in scored if s > 0]
    return (out or candidates)[:12]


def fetch_proximity_geo_intelligence(il, ilce, mahalle, proj_name):
    loc = f"{mahalle or ''} {ilce or ''} {il or ''}".strip()
    if not loc:
        return ""
    prompt = f"""
Sen NEXA'nn Bölgesel Konum & Çevre Aksı Araştırma Ajanısın (Geo-Intelligence Agent).
Şu lokasyon için Türkiye şehir planlama bilgini kullanarak ulaşım, üniversite, hastane,
metro/tramvay, otoyol ve gelişen aks bilgilerini 3 maddede özetle:

Proje: {proj_name}
Lokasyon: {loc}

GÖREVİN:
- En yakın Üniversiteler ve Eğitim Aksı
- En yakın Hastane ve Sağlık Merkezleri
- En yakın Tramvay/Metro/Bus ve Otoyol Ulaşım Aksları
- Bölgenin yatırım ve prim gelişim potansiyeli

Kısa, şık ve maddeler halinde yaz. Dokümanda yer almasa bile gerçek coğrafi lokasyondan hareketle anlat.
"""
    try:
        return _gemini_generate(prompt)
    except Exception as e:
        logger.warning("Geo intelligence failed: %s", e)
        return ""


def _gemini_generate(contents):
    from google import genai
    keys = _read_api_keys()
    now = time.time()
    with _cache_lock:
        dead = set(_dead_models)
    targets = [m for m in FALLBACK_MODELS if m not in dead]
    for key in keys:
        with _cache_lock:
            cd = _key_cooldowns.get(key, 0)
            preferred = _last_good_model.get(key)
        if cd > now:
            logger.warning("Anahtar %s... soğutma süresinde (%ds kaldi)", key[-6:], int(cd - now))
            continue
        model_order = targets if not preferred else [preferred] + [m for m in targets if m != preferred]
        try:
            client = genai.Client(api_key=key)
            for model in model_order:
                try:
                    resp = client.models.generate_content(model=model, contents=contents)
                    if resp and resp.text:
                        with _cache_lock:
                            _last_good_model[key] = model
                        return resp.text
                except Exception as e:
                    msg = str(e)
                    if _is_quota_error(msg):
                        with _cache_lock:
                            _key_cooldowns[key] = time.time() + KEY_COOLDOWN
                        logger.warning("Anahtar %s kota yok (60sn yasak): %s", key[-6:], msg[:100])
                        break
                    if "404" in msg or "no longer available" in msg:
                        with _cache_lock:
                            _dead_models.add(model)
                    logger.warning("Model %s failed (%s)", model, msg[:120])
                    continue
        except Exception as e:
            msg = str(e)
            if _is_quota_error(msg):
                with _cache_lock:
                    _key_cooldowns[key] = time.time() + KEY_COOLDOWN
            logger.warning("Key failed: %s", msg[:120])
            continue
    return _ollama_fallback(contents)


def _is_quota_error(msg):
    m = (msg or "").lower()
    return ("429" in m or "quota" in m or "resource_exhausted" in m
            or "rate limit" in m or "permission" in m or "api key not valid" in m)


def _ollama_fallback(prompt):
    try:
        import httpx
        # P9: Ping — Ollama kapalıysa 2 sn içinde vazgeç
        try:
            ping = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
            if ping.status_code != 200:
                return None
        except Exception:
            return None
        # D8: kurulu modellerden birini seç (llama3 yoksa varsayılan yanlış 15 sn bekleme)
        model = "llama3"
        try:
            tags = ping.json().get("models") or []
            if tags:
                model = tags[0].get("name") or model
        except Exception:
            pass
        resp = httpx.post("http://localhost:11434/api/generate",
                          json={"model": model, "prompt": prompt, "stream": False},
                          timeout=15.0)
        if resp.status_code == 200:
            return (resp.json().get("response") or "")[:2000]
    except Exception:
        pass
    return None


def _find_project_by_name(name):
    db = _load_db()
    db.row_factory = sqlite3.Row
    try:
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
        target = norm(name)
        best, best_score = None, 0
        for p in db.execute("SELECT * FROM projects WHERE COALESCE(is_portfolio,0) = 0 ORDER BY id ASC"):
            t = norm(p["name"])
            if t and (target in t or t in target):
                score = min(len(target), len(t))
                if score > best_score:
                    best_score, best = score, p
        return dict(best) if best else None
    finally:
        db.close()


_LOCATION_KEYWORDS = ("ulaşım", "aks", "yakınlık", "nerede", "çevre", "hastane", "okul",
                      "üniversite", "metro", "tramvay", "otoyol", "havalimanı", "avm",
                      "konum", "mesafe", "bölge", "site", "çevresinde", "manzara")


def _load_summaries():
    try:
        if SUMMARIES_FILE.exists():
            return json.loads(SUMMARIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_summaries(data):
    try:
        with _save_lock:
            SUMMARIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    except Exception as e:
        logger.warning("Ozet kaydedilemedi: %s", e)


def build_project_summary(proj):
    context = build_project_context(proj["id"])
    prompt = f"""
Sen Nexa portföy danışmanısın. Aşağıdaki proje verilerinden kısa, net ve satış odaklı bir PROJE ÖZETİ üret.
Format (madde madde, en fazla 8 satır, başlık satırı olmadan):
- Konum ve proje kimliği
- Proje konsepti, kat/blok ve daire tipleri
- Fiyat ve ödeme planı özeti (varsa gerçek rakamlar)
- Teslim süresi (varsa)
- Tapu/TKGM durumu (ada/parsel)

Uydurma veri ekleme; bilinmeyeni yazma.

PROJE: {proj['name']}
VERİ:
{context}
"""
    reply = _gemini_generate(prompt)
    if reply and len(reply.strip()) > 40:
        return reply.strip()
    return None


def generate_all_project_summaries(force=False):
    """Drive (markalı) projelerin her biri için özeti otomatik üretir ve önbelleğe alır."""
    data = {} if force else _load_summaries()
    db = _load_db()
    db.row_factory = sqlite3.Row
    try:
        projects = db.execute(
            "SELECT * FROM projects WHERE COALESCE(is_portfolio,0) = 0 ORDER BY id ASC").fetchall()
    finally:
        db.close()
    done = 0
    for p in projects:
        name = p["name"]
        if not force and data.get(name, {}).get("summary"):
            continue
        try:
            s = build_project_summary(dict(p))
            if s:
                data[name] = {"summary": s, "project_id": p["id"], "ts": time.time()}
                done += 1
                logger.info("Ozet uretildi: %s", name)
                _save_summaries(data)
            time.sleep(0.7)
        except Exception as e:
            logger.warning("Ozet uretilemedi %s: %s", name, e)
            continue
    _save_summaries(data)
    return done


def get_project_summary(name):
    return _load_summaries().get(name, {}).get("summary") or ""


def cognitive_chat(user_message, project=None, history=None):
    """
    NEXA PRIME seviyesinde bilişsel cevap üretir.
    project: name ile eşleşen project dict (varsa tekil proje modu).
    history: son 6-8 mesajdan oluşan [{"role": "user"/"assistant", "text": ...}] listesi (E3).
    Başarısızlıkta None döner; çağıran heuristic'e düşer.
    """
    msg = (user_message or "").strip()
    if not msg:
        return None
    is_location_query = any(kw in msg.lower() for kw in _LOCATION_KEYWORDS)

    # P8: 5 dk TTL'li yanıt önbelleği — aynı soru Gemini'yi tekrar meşgul etmez
    cache_key = ("P:" + project["name"]) if project else ("G:" + msg.lower().strip())
    with _cache_lock:
        hit = _reply_cache.get(cache_key)
        if hit and time.time() - hit[1] < REPLY_TTL:
            return hit[0]

    def _cached(reply):
        if reply:
            with _cache_lock:
                _reply_cache[cache_key] = (reply, time.time())
                if len(_reply_cache) > 200:
                    stale = [k for k, (_, ts) in _reply_cache.items() if time.time() - ts > REPLY_TTL]
                    for k in stale:
                        _reply_cache.pop(k, None)
        return reply

    history_block = ""
    if history:
        turns = []
        for h in history[-8:]:
            role = h.get("role") if isinstance(h, dict) else "user"
            text = (h.get("content") or h.get("text") if isinstance(h, dict) else str(h) or "").strip()
            if text:
                turns.append(f"{'Müşteri' if role == 'user' else 'Nexa'}: {text[:400]}")
        if turns:
            history_block = "ÖNCEKİ SOHBET (aynı ziyaretçi, bağlam için kullan; çelişiyorsa son mesaja uy):\n" + "\n".join(turns) + "\n\n"

    if project:
        cached = get_project_summary(project["name"])
        if cached:
            return _cached(f"**{project['name']} — Proje Özeti**\n\n{cached}\n\n{CONTACT_LINE}")
        try:
            s = build_project_summary(project)
            if s:
                _save_summaries(_merge_summary(project["name"], project["id"], s))
                return _cached(f"**{project['name']} — Proje Özeti**\n\n{s}\n\n{CONTACT_LINE}")
        except Exception:
            pass
        context = build_project_context(project["id"], query=msg)
        geo = ""
        if is_location_query or not context:
            geo = fetch_proximity_geo_intelligence(
                project.get("il") or "", project.get("ilce") or "",
                project.get("mahalle") or "", project["name"])
        system = f"""
Sen Nexa — Bilişkin Gayrimenkul Ekosistemi'nin kıdemli lüks yatırım danışmanısın (NEXA PRIME v2).
Son derece profesyonel, elit, ikna edici ve karizmatik bir dille yanıt ver.

İncelenen Proje: {project['name']}

{history_block}
RAG BAĞLAMI (metadata + dokümanlar):
{context if context else 'Bu proje için özel doküman bağlamı yok; genel portföy verisi geçerli.'}

GEO-INTELLIGENCE:
{geo if geo else '(lokasyon aksı sorgusu bağlamdan yanıtlanacak)'}

Kullanıcı: {msg}

Kurallar:
1. Fiyat, teslim, metrekare, ödeme planı varsa RAG'daki rakamları birebir ver.
2. ULAŞIM/ÇEVRE sorularında GEO verisini kullanarak tramvay, hastane, üniversite, otoyol akslarını anlat; 'dokümanda yok' deme.
3. Uydurma veri ekleme; bilinmeyeni 'danışmanımız netleştirecektir' diyerek kapat.
4. Madde/liste kullan, markdown. Sonuna şu iletişim satırını ekle: {CONTACT_LINE}
Cevabı 450 kelimeyi aşmadan Türkçe yaz.
"""
    else:
        context = build_global_context()
        summaries = _load_summaries()
        summ_block = "\n".join(
            f"- {n}: {d.get('summary', '')}" for n, d in summaries.items() if d.get("summary"))
        prompt_summaries = f"""
ÖNCEDEN ÜRETİLMİŞ PROJE ÖZETLERİ (bu blok doğrudan kullanılabilir, eksik proje varsa aşağıdaki RAG bağlamından tamamla):
{summ_block[:6000] if summ_block else '(henüz üretilmemiş — RAG bağlamından yararlan)'}
"""
        system = f"""
Sen Nexa — Bilişkin Gayrimenkul Ekosistemi'nin Baş Portföy & Yatırım Stratejisti AI Danışmanısın (NEXA PRIME v2).
Tüm portföydeki MARKALI PROJELERİ çapraz analiz eden kıdemli danışmansın.

{history_block}
{prompt_summaries}

TÜM PORTFÖY RAG BAĞLAMI (proje metadata + kayıtlı doküman özetleri/fiyat listeleri):
{context}

Kullanıcı: {msg}

Kurallar:
1. Soruyu markalı projelerin verilerine dayanarak cevapla; kişisel portföy ilanları bu sistemde değildir.
2. Projeleri tanıtırken her proje için KISA PROJE ÖZETİ formatı kullan: adı — lokasyon — konsept/tipler — fiyat/ödeme özeti — teslim (varsa) — TKGM/ada-parsel. En fazla 5-6 satır/proje.
3. Karşılaştırma sorularında şık bir Markdown tablosu kullan; rakamları yalnızca veriden al, uydurma.
4. Bilgi bağlamda yoksa "danışmanımız netleştirecektir" de. İlgisiz sorularda kısa ve nazik bilgilendir.
5. Markdown formatında, madde ve tablo kullan. Cevabı 500 kelimeyi aşmadan Türkçe yaz.
Sonuna şu iletişim satırını ekle: {CONTACT_LINE}
"""
    try:
        reply = _gemini_generate(system)
        if reply and len(reply.strip()) > 20:
            return _cached(reply.strip())
    except Exception as e:
        logger.error("Cognitive generation failed: %s", e)
    return None


if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("— GLOBAL TEST —")
    print(cognitive_chat("En uygun fiyatlı 3+1 projeler hangileri? 5 milyon bütçem var."))
    print()
    print("— TEKİL PROJE TESTI —")
    proj = _find_project_by_name("MONZA MOON")
    print(cognitive_chat("Bu projenin fiyat ve ödeme planını anlatır mısın?", project=proj))