#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fiyat_import.py — NEXA fiyat/oda zenginleştirme aracı
=====================================================
Markalı (portföy dışı) projelerin price_display / room_info alanlarını
documents.chunk_text içindeki fiyat listesi / ödeme planı kalıplarından çıkarır
ve hem veritabanına (projects tablosu) hem nexa_portfolio_data.json'a yazar.

Kurallar:
  * Çalışma öncesi DB ve JSON yedeklenir (Temp dizinine).
  * İdempotent: dolu olan alanlar (portföy ilanları dahil) ASLA ezilmez.
  * Güvenilirlik: yalnızca AÇIK ve TUTARLI eşleşme varsa yazılır
    (>=2 ayrı fiyat değeri veya tek değerin en az 2 tekrarı).
  * Çıkarım güvenilir değilse ve JSON'da dolu değer varsa DB o değerle beslenir.
  * Şüpheli/yetersiz projeler raporlanır, alana dokunulmaz.
"""

import io
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB = r"C:\Users\USER\Desktop\NEXA_PRIME_v2_ENTERPRISE\nexa_database.db"
JSON = Path(r"C:\Users\USER\Desktop\3\nexa_portfolio_data.json")
BACKUP_DB = r"C:\Users\USER\AppData\Local\Temp\opencode\nexa_database_backup_fiyat.db"
BACKUP_JSON = r"C:\Users\USER\AppData\Local\Temp\opencode\nexa_portfolio_data_backup_fiyat.json"

MIN_FIYAT = 1_000_000          # tam daire fiyatı alt eşiği (taksit/peşinat gürültüsü elenir)
PRICE_DOC_RE = re.compile(r"fiyat|ödeme|odeme|başlangıç|baslangic", re.I)

# Fiyat kalıpları: '4.900.000₺', '1.990.000/uni20BA', '5.000.000 TL', '₺2.400.000',
# "X'den başlayan", '3 milyon'dan başlıyor'
PRICE_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{3})+)\s*(?:₺|TL|lira)?"      # sayı + (isteğe bağlı) birim
    r"(?:'den|’den|den)?\s*(?:[Bb]aşlayan|[Bb]aşlıyor)?"  # "...den başlayan"
    r"|(?:₺|TL|lira)\s*(\d{1,3}(?:[.,]\d{3})+)",     # '₺2.400.000'
    re.I,
)
MILYON_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[Mm]ilyon(?:'dan|’dan|dan)?\s*(?:[Bb]aşlıyor|[Bb]aşlayan)")
ROOM_RE = re.compile(r"([1-9])\s*\+\s*([1-9])(?!\d)")           # 3+1 / 2 +1; "6+18" elenir
ROOM_RANGE_RE = re.compile(r"([1-9])\+1(?:'den|’den)\s*(?:[^0-9+\n]{0,40}?)([1-9])\+1(?:'e|’e|\s*kadar)")

# Peşinat/kalan ödeme gürültüsü: sayıdan önceki 45 veya sonraki 30 karakterde
# bu ifadeler varsa düşür (docx tablo akışında etiket sayıdan sonra gelebilir)
GUARD_RE = re.compile(r"kalan\s*ödeme|kalan\s*ödenen|taksit\s*miktarı|peşinat\s*\(|peşinat:|pesinat", re.I)
GUARD_RE_AFTER = re.compile(r"kalan\s*ödeme|kalan\s*ödenen|taksit\s*miktarı", re.I)


def norm_key(t):
    t = (t or "").lower()
    return re.sub(r"\s+", " ", t).strip()


def norm_text(t):
    """Chunk metnini parse'a hazırlar: bozuk ₺ glifleri ve satır bölünmesi."""
    t = re.sub(r"/uni20BA|/uni20BF", "₺", t)
    t = re.sub(r"(?<=\d)\s*\n\s*(?=\d)", "", t)   # '5.75\n0.000' -> '5.750.000'
    return t


def parse_num(s):
    s = s.strip().replace(",", ".")
    try:
        v = float(s.replace(".", ""))
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def fmt_tl(v):
    return f"₺{v:,.0f}".replace(",", ".")


def fmt_rooms(room_set):
    rooms = sorted(room_set, key=lambda r: (int(r.split("+")[0]), int(r.split("+")[1])))
    return ", ".join(rooms)


def extract_project(curs, pid):
    """Projenin TÜM chunk'larından fiyat adayları + oda tiplerini toplar."""
    cands = []            # (deger, fiyat_belgesi_mi)
    room_set = set()
    docs = curs.execute(
        """SELECT d.id did, d.title, c.chunk_text
           FROM document_chunks c
           JOIN documents d ON d.id = c.document_id
           WHERE d.project_id = ? ORDER BY c.id""", (pid,)).fetchall()
    # Belge bazında grupla; satır bölünmüş sayıları birleştirmek için chunk'lar "\n" ile eklenir
    by_doc = {}
    for d in docs:
        by_doc.setdefault((d["did"], d["title"]), []).append(d["chunk_text"] or "")

    for (did, title), chunks in by_doc.items():
        t = norm_text("\n".join(chunks))
        is_price_doc = bool(PRICE_DOC_RE.search(title or ""))
        for m in PRICE_RE.finditer(t):
            g = m.group(1) or m.group(2) or ""
            v = parse_num(g)
            if not v or v < MIN_FIYAT:
                continue
            once = t[max(0, m.start() - 45):m.start()]
            sonra = t[m.end():m.end() + 30]
            if GUARD_RE.search(once) or GUARD_RE_AFTER.search(sonra):
                continue
            cands.append((v, is_price_doc))
        for m in MILYON_RE.finditer(t):
            try:
                v = int(float(m.group(1).replace(",", ".")) * 1_000_000)
            except ValueError:
                continue
            if v >= MIN_FIYAT:
                once = t[max(0, m.start() - 45):m.start()]
                if not GUARD_RE.search(once):
                    cands.append((v, is_price_doc))
        for m in ROOM_RE.finditer(t):
            room_set.add(f"{m.group(1)}+{m.group(2)}")
        for m in ROOM_RANGE_RE.finditer(t):
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi >= lo:
                for k in range(lo, hi + 1):
                    room_set.add(f"{k}+1")
    return cands, room_set


def karar(cands):
    """Açık/tutarlı karar: (durum, min, max, neden) — 'range'|'single'|'skip'."""
    vals = [v for v, _ in cands]
    distinct = sorted(set(vals))
    if not distinct:
        return "skip", None, None, "fiyat verisi bulunamadı"
    keep = [v for v in distinct
            if not any(w == 2 * v for w in distinct)      # peşinat (tam yarı değer)
            and not any(v == int(round(w * 0.9)) for w in distinct if w > v)]  # %10 ödeme planı varyantı
    keep_price_doc = [v for v in keep if any(v == w and src for w, src in cands)]
    if len(keep_price_doc) >= 2:
        return "range", min(keep_price_doc), max(keep_price_doc), "fiyat belgesinden aralık"
    if len(keep) >= 2:
        if max(keep) <= 8 * min(keep):
            return "range", min(keep), max(keep), "birden çok fiyat kalıbından aralık"
        return "skip", None, None, "dağınık/çelişkili değerler (uç genişliği 8x'ten fazla) — yazılmadı"
    if len(keep) == 1 and vals.count(keep[0]) >= 2:
        return "single", keep[0], keep[0], "tek tutarlı fiyat (2+ eşleşme)"
    return "skip", None, None, "şüpheli/tek eşleşme — yazılmadı"


def main():
    try:
        shutil.copy2(DB, BACKUP_DB)
        print(f"[YEDEK] DB  -> {BACKUP_DB}")
    except Exception as e:
        print(f"[YEDEK HATA] DB: {e}")
        sys.exit(1)
    try:
        shutil.copy2(JSON, BACKUP_JSON)
        print(f"[YEDEK] JSON -> {BACKUP_JSON}")
    except Exception as e:
        print(f"[YEDEK HATA] JSON: {e}")
        sys.exit(1)

    with open(JSON, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    # JSON'da db_id eski bir şema (DB projects.id ile uyuşmuyor) -> başlık eşleşmesi kullanılır
    jmap = {}
    for it in jdata:
        if it.get("type") == "project":
            jmap[norm_key(it.get("title"))] = it

    db = sqlite3.connect(DB, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")

    guncellendi = 0
    atlandi = 0
    yetersiz = []
    rows = []

    try:
        projeler = db.execute(
            "SELECT id, name, price_display, room_info FROM projects WHERE is_portfolio=0 ORDER BY id"
        ).fetchall()
        for p in projeler:
            pid, name = p["id"], p["name"]
            jit = jmap.get(norm_key(name))

            cands, rooms = extract_project(db, pid)
            durum, lo, hi, neden = karar(cands)
            yeni_fiyat = None
            if durum == "range" and lo != hi:
                yeni_fiyat = f"{fmt_tl(lo)} - {fmt_tl(hi)}"
            elif durum in ("range", "single"):
                yeni_fiyat = fmt_tl(lo)

            yeni_oda = fmt_rooms(rooms) if rooms else None
            notlar = []

            # --- DB yazımı (yalnızca boş alan) ---
            db_fiyat = (p["price_display"] or "").strip()
            db_oda = (p["room_info"] or "").strip()

            # Çıkarım güvenilir değilse, JSON'da dolu değer varsa DB onu kullanır
            if not db_fiyat:
                if yeni_fiyat:
                    db.execute("UPDATE projects SET price_display=? WHERE id=?", (yeni_fiyat, pid))
                    db_fiyat = yeni_fiyat
                    guncellendi += 1
                elif jit and (jit.get("price_display") or "").strip():
                    js = jit["price_display"].strip()
                    db.execute("UPDATE projects SET price_display=? WHERE id=?", (js, pid))
                    db_fiyat = js
                    notlar.append("JSON'daki değer DB'ye yazıldı (çıkarım güvenilir değildi)")
                    guncellendi += 1
                else:
                    yetersiz.append((pid, name, "fiyat: " + neden))
                    notlar.append("fiyat: " + neden)
            else:
                atlandi += 1
                notlar.append("DB fiyat dolu (dokunulmadı)")

            if not db_oda:
                if yeni_oda:
                    db.execute("UPDATE projects SET room_info=? WHERE id=?", (yeni_oda, pid))
                    db_oda = yeni_oda
                    guncellendi += 1
                elif jit and (jit.get("room_info") or "").strip():
                    js = jit["room_info"].strip()
                    db.execute("UPDATE projects SET room_info=? WHERE id=?", (js, pid))
                    db_oda = js
                    notlar.append("JSON oda değeri DB'ye yazıldı")
                    guncellendi += 1
                else:
                    yetersiz.append((pid, name, "oda verisi yok"))
                    notlar.append("oda verisi yok")
            else:
                atlandi += 1
                notlar.append("DB oda dolu (dokunulmadı)")

            # --- JSON yazımı (yalnızca boş alan) ---
            if jit is None:
                notlar.append("JSON'da kayıt yok")
            else:
                if (jit.get("price_display") or "").strip():
                    atlandi += 1
                    notlar.append("JSON fiyat dolu (dokunulmadı)")
                elif yeni_fiyat:
                    jit["price_display"] = yeni_fiyat
                    notlar.append("JSON fiyat yazıldı")
                    guncellendi += 1
                else:
                    notlar.append("JSON fiyat: " + neden)
                if (jit.get("room_info") or "").strip():
                    atlandi += 1
                    notlar.append("JSON oda dolu (dokunulmadı)")
                elif yeni_oda:
                    jit["room_info"] = yeni_oda
                    notlar.append("JSON oda yazıldı")
                    guncellendi += 1
                else:
                    notlar.append("JSON oda verisi yok")

            rows.append((pid, name, db_fiyat or "-", db_oda or "-", "; ".join(notlar)))

        db.commit()

        with open(JSON, "w", encoding="utf-8") as f:
            json.dump(jdata, f, ensure_ascii=False, indent=1)
            f.write("\n")

        # TUTARLILIK KONTROLÜ (DB açıkken)
        uyumsuz = []
        for p in db.execute("SELECT id, name, price_display, room_info FROM projects WHERE is_portfolio=0").fetchall():
            jit = jmap.get(norm_key(p["name"]))
            if not jit:
                continue
            jp = (jit.get("price_display") or "").strip()
            dp = (p["price_display"] or "").strip()
            if dp and jp and dp != jp:
                uyumsuz.append((p["name"], dp, jp))
    except Exception:
        try:
            db.rollback()
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        db.close()

    print("\n" + "=" * 108)
    print(f"{'proje#':<7}{'PROJE':<34}{'price_display (DB)':<30}{'room_info (DB)':<22}NOT")
    print("=" * 108)
    for pid, name, fd, ri, notlar in rows:
        print(f"{pid:<7}{name[:34]:<34}{fd[:29]:<30}{ri[:21]:<22}{notlar[:62]}")
    print("=" * 108)

    print(f"\nÖZET: {len(rows)} markalı proje değerlendirildi | alan bazında güncelleme: {guncellendi} "
          f"| atlandı (dolu alan): {atlandi} | yetersiz veri: {len(set(p for p, _, _ in yetersiz))} proje")

    if yetersiz:
        print("\nYETERSİZ VERİ (yazılmadı):")
        seen = set()
        for pid, name, neden in yetersiz:
            if (pid, neden) in seen:
                continue
            seen.add((pid, neden))
            print(f"  proj#{pid} {name}: {neden}")

    print("\nTUTARLILIK KONTROLÜ:")
    if uyumsuz:
        for name, dp, jp in uyumsuz:
            print(f"  [!] {name}: DB={dp} | JSON={jp} — değerler farklı (JSON ezilmedi, manuel kontrol önerilir)")
    else:
        print("  DB ile JSON değerleri çelişen proje yok.")
    print("  Tamamlandı.")


if __name__ == "__main__":
    main()
