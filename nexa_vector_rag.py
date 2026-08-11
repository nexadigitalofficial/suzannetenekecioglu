"""
NEXA Vector RAG (P17) — document_chunks üzerinde GERÇEK vektör (cosine) araması.

ÖLÇÜLEN GERÇEK: DB'deki document_chunks.embedding sütunu 768 boyutlu float JSON listesi
içerir (1290 chunk). Ancak bu vektörler, API'si kaldırılmış ESKİ modelin
(text-embedding-004 -> 404 NOT_FOUND) uzayındadır; canlı model gemini-embedding-001(768)
ile çapraz-model cosine ~0.03-0.11'e düşer ve sıralama anlamsızlaşır (ölçüm: aynı metnin
stored vs taze embedding'i cos=0.034-0.040; rastgele çift cos=0.63 = yüksek anizotropi).

ÇÖZÜM (DB'ye/sunucuya dokunmadan): chunk metinleri canlı modelle yeniden embed edilir ve
modülün yanındaki `nexa_embedding_cache.json` dosyasına yazılır (bir kerelik ~2-4 dk).
vector_search önce önbelleğe bakar; önbellekte yoksa DB vektörleriyle dener (uyumsuz uzay
yüzünden min_score=0.35 eşiğini geçemez -> otomatik güvenli devre dışı).

Bağımsız modül: numpy ve google.genai lazy import; nexa_rag.py'den try/except ile import
edilir. Sorgu embedding'i üretilemezse vector_search [] döner (çağıran eski yönteme düşer).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("nexa.vector_rag")

# Gemini embedding modelleri (öncelik sırası): (model, output_dimensionality)
# text-embedding-004 varsayılan aday; 404 verirse gemini-embedding-001 (768'e kısıtlanır),
# o da yoksa text-embedding-005. Hiçbiri yoksa _embed_query None döner (arama devre dışı).
_EMBED_MODELS = [
    ("text-embedding-004", None),
    ("gemini-embedding-001", 768),
    ("text-embedding-005", None),
]
_EMB_DIM = 768  # document_chunks.embedding boyutu (DB'de ölçüldü)

_dead_embed_models = set()          # 404 veren modeller (tekrar denenmez)
_dead_embed_lock = threading.Lock()
_dead_keys = set()                  # kalıcı bozuk anahtarlar (403 leaked vb.)
_key_rotate = [0]                   # anahtar rotasyon sayacı (kota paylaşımı)
_key_rotate_lock = threading.Lock()
_quota_backoff = [5.0]              # 429 sonrası bekleme (saniye), başarıda sıfırlanır
_last_good_embed_model = [None]     # son başarılı embedding modeli (cache metadata'sı)

# ─── EMBEDDING CACHE (lazy, TTL) ───
_emb_cache = None          # (rows, loaded_at) — rows: [{id, chunk_text, document_title, project_id, vec}]
_emb_cache_lock = threading.Lock()
EMB_CACHE_TTL = 300        # 5 dk

_CACHE_FILE = Path(__file__).resolve().parent / "nexa_embedding_cache.json"
_CONFIG_CANDIDATES = (Path(__file__).resolve().parent / "config.json",)
_DEFAULT_DB = Path(r"C:\Users\USER\Desktop\NEXA_PRIME_v2_ENTERPRISE") / "nexa_database.db"


def _db_path():
    """nexa_rag import edilebilirse onun DB_PATH'ini (config.json'a duyarlı) kullanır;
    değilse kendi config.json -> nexa_db_dir okumasına düşer."""
    try:
        from nexa_rag import DB_PATH
        if DB_PATH:
            return str(DB_PATH)
    except Exception:
        pass
    for cfg in _CONFIG_CANDIDATES:
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            d = data.get("nexa_db_dir")
            if d:
                return str(Path(d) / "nexa_database.db")
        except Exception:
            continue
    return str(_DEFAULT_DB)


def _read_api_keys():
    """nexa_rag._read_api_keys varsa onu kullanır; yoksa .env'den GEMINI_API_KEYS okur."""
    try:
        from nexa_rag import _read_api_keys as nexa_keys
        keys = nexa_keys()
        if keys:
            return keys
    except Exception:
        pass
    keys = []
    env_keys = os.getenv("GEMINI_API_KEYS", "")
    if env_keys:
        keys += [k.strip() for k in env_keys.split(",") if k.strip()]
    env_file = Path(r"C:\Users\USER\Desktop\NEXA_PRIME_v2_ENTERPRISE\.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEYS="):
                keys += [k.strip() for k in line.split("=", 1)[1].split(",") if k.strip()]
    seen, out = set(), []
    for k in keys:
        if k and k not in seen and len(k) >= 10:
            seen.add(k)
            out.append(k)
    return out


def normalize(v):
    """L2 normalize — cosine similarity için gerekli. numpy lazy import."""
    import numpy as np
    a = np.asarray(v, dtype="float64")
    n = np.linalg.norm(a)
    if n < 1e-12:
        return a
    return a / n


def _pick_key():
    """Çalışan anahtarlardan sırayla birini seçer (kota yükünü dağıtır)."""
    keys = [k for k in _read_api_keys() if k not in _dead_keys]
    if not keys:
        return None
    with _key_rotate_lock:
        idx = _key_rotate[0] % len(keys)
        _key_rotate[0] += 1
    return keys[idx]


def _sleep_backoff():
    """429 sonrası bekleme; ardışık kotalarda üstel büyür."""
    with _dead_embed_lock:
        wait = _quota_backoff[0]
        _quota_backoff[0] = min(wait * 2, 60.0)
    time.sleep(wait)


def _reset_backoff():
    with _dead_embed_lock:
        _quota_backoff[0] = 5.0


def _embed_query(query):
    """Sorgu embedding'ini üretir. Hiçbir model çalışmazsa None döner.
    ASLA kırpılmış/kelime sayılı sahte vektör üretmez — kaliteyi bozmaz."""
    if not query or not str(query).strip():
        return None
    try:
        from google import genai
    except Exception as e:
        logger.warning("google.genai yok, embedding kapali: %s", e)
        return None
    with _dead_embed_lock:
        models = [m for m in _EMBED_MODELS if m[0] not in _dead_embed_models]
    if not models:
        return None
    for _ in range(len(_read_api_keys()) * 2):
        key = _pick_key()
        if not key:
            return None
        try:
            client = genai.Client(api_key=key)
        except Exception as e:
            logger.warning("GenAI client kurulamadi: %s", str(e)[:100])
            continue
        for model, dim in models:
            try:
                kwargs = {"model": model, "contents": str(query)}
                if dim:
                    kwargs["config"] = {"output_dimensionality": dim}
                resp = client.models.embed_content(**kwargs)
                vals = resp.embeddings[0].values
                if not vals:
                    continue
                vec = normalize(vals)
                if vec.size != _EMB_DIM:
                    logger.warning("Embedding %s %d boyut verdi (beklenen %d); kullanilmiyor",
                                   model, vec.size, _EMB_DIM)
                    continue
                _reset_backoff()
                with _dead_embed_lock:
                    _last_good_embed_model[0] = model
                return vec
            except Exception as e:
                msg = str(e)
                if "404" in msg or "not found" in msg:
                    with _dead_embed_lock:
                        _dead_embed_models.add(model)
                    logger.warning("Embedding model %s mevcut degil, devre disi: %s",
                                   model, msg[:80])
                elif "429" in msg or "quota" in msg or "resource_exhausted" in msg.lower():
                    _sleep_backoff()
                elif "leaked" in msg or "permission" in msg.lower():
                    with _dead_embed_lock:
                        _dead_keys.add(key)
                    logger.warning("Anahtar ...%s kalici devre disi: %s", key[-6:], msg[:80])
                else:
                    logger.warning("Embedding %s basarisiz: %s", model, msg[:100])
                continue
    return None


def _embed_text(text):
    """Tek metni canlı modelle embed eder (önbellek yeniden üretimi için).
    429 kotalarında uyur ve devam eder; kalıcı hatalarda None."""
    if not text or not str(text).strip():
        return None
    try:
        from google import genai
    except Exception:
        return None
    for attempt in range(8):
        with _dead_embed_lock:
            models = [m for m in _EMBED_MODELS if m[0] not in _dead_embed_models]
        if not models:
            return None
        key = _pick_key()
        if not key:
            return None
        try:
            client = genai.Client(api_key=key)
        except Exception:
            continue
        for model, dim in models:
            try:
                kwargs = {"model": model, "contents": str(text)[:2000]}
                if dim:
                    kwargs["config"] = {"output_dimensionality": dim}
                resp = client.models.embed_content(**kwargs)
                vals = resp.embeddings[0].values
                if not vals:
                    continue
                vec = normalize(vals)
                if vec.size != _EMB_DIM:
                    continue
                _reset_backoff()
                with _dead_embed_lock:
                    _last_good_embed_model[0] = model
                return [float(x) for x in vec]
            except Exception as e:
                msg = str(e)
                if "404" in msg or "not found" in msg:
                    with _dead_embed_lock:
                        _dead_embed_models.add(model)
                    logger.warning("Embedding model %s mevcut degil, devre disi: %s",
                                   model, msg[:80])
                elif "429" in msg or "quota" in msg or "resource_exhausted" in msg.lower():
                    _sleep_backoff()
                elif "leaked" in msg or "permission" in msg.lower():
                    with _dead_embed_lock:
                        _dead_keys.add(key)
                    logger.warning("Anahtar ...%s kalici devre disi: %s", key[-6:], msg[:80])
                else:
                    logger.warning("Embedding %s basarisiz: %s", model, msg[:100])
                continue
    return None


# ─── ÖNBELTEK DOSYASI (DB vektörleri yerine canlı model uzayı) ───
def _read_cache_file():
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("dim") == _EMB_DIM:
                return {int(i): v for i, v in data.get("rows", [])}
    except Exception as e:
        logger.warning("Embedding cache okunamadi: %s", e)
    return None


def _write_cache_file(rows_by_id):
    try:
        with _dead_embed_lock:
            model = _last_good_embed_model[0] or _EMBED_MODELS[0][0]
        data = {"model": model, "dim": _EMB_DIM, "rows": list(rows_by_id.items())}
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
        logger.info("Embedding cache yazildi: %d vektor (%s)", len(rows_by_id), _CACHE_FILE)
        return True
    except Exception as e:
        logger.warning("Embedding cache yazilamadi: %s", e)
        return False


def rebuild_embedding_cache(include_missing=True, workers=4):
    """Tüm chunk metinlerini canlı modelle yeniden embed edip yerel önbelleğe yazar.
    Bir kerelik (kota izin verdiği hızda); tekrar çalıştırmak yalnızca eksikleri tamamlar
    (idempotent). DB'ye YAZMAZ, sunucuya dokunmaz. Döner: yeni embed edilen sayısı."""
    import numpy as np  # noqa: F401  (lazy import kalıbı)
    db = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        cur = db.execute("""
            SELECT dc.id, dc.chunk_text FROM document_chunks dc
            WHERE LENGTH(TRIM(dc.chunk_text)) > 0
        """)
        all_rows = [(int(r["id"]), (r["chunk_text"] or "").strip()) for r in cur]
    finally:
        db.close()
    cache = _read_cache_file() or {}
    todo = [(cid, text) for cid, text in all_rows if cid not in cache and text]
    done = 0
    if not todo:
        logger.info("rebuild: onbellek guncel (%d vektor)", len(cache))
        return 0
    logger.info("rebuild: %d chunk embed edilecek (mevcut onbellek: %d)", len(todo), len(cache))
    idx = [0]

    def work(item):
        cid, text = item
        vec = _embed_text(text)
        if vec is None:
            logger.warning("rebuild: chunk %d embed edilemedi", cid)
            return None
        return (cid, vec)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(work, todo):
            if res is None:
                continue
            cid, vec = res
            cache[cid] = vec
            done += 1
            idx[0] += 1
            if idx[0] % 100 == 0:
                _write_cache_file(cache)
                logger.info("rebuild: %d/%d tamam", idx[0], len(todo))
    _write_cache_file(cache)
    logger.info("rebuild tamam: %d yeni vektor", done)
    return done


# ─── VERTÖR YÜKLEME (DB + önbellek birleşimi) ───
def _load_embeddings():
    """Chunk metin/title/project_id'yi DB'den, vektörü ÖNCE canlı-model önbelleğinden,
    yoksa DB sütunundan alır. Sonuç lazy + TTL cache (1290 x 768 ≈ 8MB)."""
    global _emb_cache
    now = time.time()
    with _emb_cache_lock:
        if _emb_cache is not None and now - _emb_cache[1] < EMB_CACHE_TTL:
            return _emb_cache[0]
    import numpy as np
    cache = _read_cache_file()
    rows = []
    db = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        cur = db.execute("""
            SELECT dc.id, dc.chunk_text, dc.embedding, d.project_id, d.title
            FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
        """)
        for r in cur:
            text = (r["chunk_text"] or "").strip()
            if not text:
                continue
            vec = None
            if cache is not None:
                vec = cache.get(r["id"])
            if vec is None:
                raw = r["embedding"]
                if not raw or len(raw) <= 10:
                    continue
                try:
                    vals = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(vals, list) or len(vals) != _EMB_DIM:
                    continue
                vec = vals
            rows.append({
                "id": r["id"],
                "chunk_text": text,
                "document_title": r["title"],
                "project_id": r["project_id"],
                "vec": np.asarray(vec, dtype="float64"),
            })
    finally:
        db.close()
    with _emb_cache_lock:
        _emb_cache = (rows, now)
    logger.info("Vector RAG: %d chunk yuklendi (onbellek vektoru: %s)",
                len(rows), "VAR" if cache else "YOK")
    return rows


def vector_search(query, project_id=None, top_k=6, min_score=0.35):
    """Sorguya en benzer chunk'ları cosine similarity ile döner.

    Döner: [{"chunk_text":..., "document_title":..., "score":...}]
    Embedding üretilemez veya eşleşme bulunamazsa [] döner — çağıran eski yönteme düşer.
    """
    qvec = _embed_query(query)
    if qvec is None:
        logger.info("vector_search: sorgu embedding'i uretilemedi, sonuc yok")
        return []
    rows = _load_embeddings()
    if not rows:
        return []
    import numpy as np
    results = []
    for r in rows:
        if project_id is not None and r["project_id"] != project_id:
            continue
        v = r["vec"]
        nn = np.linalg.norm(v)
        if nn < 1e-12:
            continue
        score = float(np.dot(qvec, v) / nn)
        if score >= min_score:
            results.append((score, r))
    results.sort(key=lambda x: -x[0])
    out = []
    for score, r in results[:top_k]:
        out.append({
            "chunk_text": r["chunk_text"],
            "document_title": r["document_title"],
            "score": round(float(score), 4),
        })
    return out
