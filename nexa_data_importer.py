#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEXA VERİ ZENGİNLEŞTİRİCİ (P6)
NEXA DB'deki doküman chunk'larından markalı projelerin FİYAT / ODA / TESLİM
verilerini çıkarır:
  1) nexa_project_prices.json  → proje bazlı çıkarılmış veri
  2) nexa_portfolio_data.json  → price_display / room_info / description güncelle
Kullanım: python nexa_data_importer.py
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).parent
DB_PATH = Path(r"C:\Users\USER\Desktop\NEXA_PRIME_v2_ENTERPRISE\nexa_database.db")
PRICES_OUT = BASE_DIR / "nexa_project_prices.json"
PORTFOLIO_OUT = BASE_DIR / "nexa_portfolio_data.json"

GARBAGE = re.compile(r"[\u0080-\u02FF]{4,}")
PRICE_RE = re.compile(r"([\d][\d.,]*)\s*(?:₺|TL)|(?:₺)\s*([\d][\d.,]*)", re.I)
ROOM_RE = re.compile(r"(\d)\s*\+\s*(\d)")
MONTH_RE = re.compile(r"(\d{1,2})\s*Ay\b", re.I)
# Taksit/peşinat/kapora/ayda gibi ödeme bağlamı: bu sözcüklere bitişik fiyatlar
# toplam fiyat DEĞİLDİR, elenir.
BAD_CTX = re.compile(r"(kapora|kaparo|taksit|peşinat|pesinat|ayda|aylık|aylik|vade|öde|ode|%50|%40|%30|hisse|gönderim|maksimum|minimum)", re.I)
CTX_BEFORE, CTX_AFTER = 30, 45


def is_bad_price_context(text, start, end):
    """Fiyat eşleşmesinin çevresinde ödeme bağlamı varsa True."""
    low = text
    s = max(0, start - CTX_BEFORE)
    e = min(len(text), end + CTX_AFTER)
    return bool(BAD_CTX.search(low[s:e]))


def norm_price(raw):
    """Binlik/ondalık ayraçları Türkçe gayrimenkul formatında normalleştirir."""
    s = raw.strip()
    if not s:
        return None
    if "." in s and "," in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if s.count(",") == 1 and len(s.split(",")[1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or len(parts[-1]) == 3:
            s = s.replace(".", "")
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if not (500_000 <= v <= 500_000_000):
        return None
    return int(v)


def good_text(chunk):
    """Anlamlı metin mi? (çöp glyph yok, yeterli kelime var)"""
    if not chunk:
        return False
    if GARBAGE.search(chunk):
        return False
    words = [w for w in chunk.split() if w.strip()]
    if len(words) < 8 or len(chunk) < 90:
        return False
    if "belirtilen" in chunk.lower() and len(words) < 20:
        return False
    return True


def main():
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    projects = db.execute(
        "SELECT * FROM projects WHERE COALESCE(is_portfolio,0)=0 ORDER BY id").fetchall()

    result = {}
    for p in projects:
        pid = p["id"]
        name = p["name"]
        chunks = db.execute("""
            SELECT d.title, dc.chunk_text
            FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
            WHERE d.project_id = ? ORDER BY dc.id
        """, (pid,)).fetchall()

        prices, rooms, months, desc_cand = [], set(), [], []
        for c in chunks:
            t = c["chunk_text"] or ""
            title = c["title"] or ""
            low = t.lower()
            for m in PRICE_RE.finditer(t):
                raw = m.group(1) or m.group(2)
                if is_bad_price_context(t, m.start(), m.end()):
                    continue
                v = norm_price(raw)
                if v:
                    prices.append(v)
            for rm in ROOM_RE.finditer(t):
                rooms.add(f"{rm.group(1)}+{rm.group(2)}")
            if re.search(r"taksit|vade|ödeme|öde|peşinat|kapora", low):
                continue
            for mm in MONTH_RE.finditer(t):
                months.append(int(mm.group(1)))
            if good_text(t) and "iban" not in low and "bank" not in low:
                desc_cand.append(t)

        prices.sort()
        if prices:
            if len(prices) >= 4:
                lo, hi = prices[len(prices) // 4], prices[-1]
            else:
                lo, hi = prices[0], prices[-1]
            if lo == hi:
                price_display = (f"₺{lo:,}").replace(",", ".")
            else:
                price_display = (f"₺{lo:,} - ₺{hi:,}").replace(",", ".")
            price_min, price_max = lo, hi
        else:
            price_display, price_min, price_max = "", None, None

        room_list = sorted(rooms, key=lambda r: (int(r.split("+")[0]), int(r.split("+")[1])))
        teslim_ay = max(months) if months else None
        desc = ""
        if desc_cand:
            best = max(desc_cand, key=len)
            desc = re.sub(r"\s+", " ", best).strip()[:320]

        result[name] = {
            "price_display": price_display,
            "price_min": price_min,
            "price_max": price_max,
            "rooms": room_list,
            "teslim_ay": teslim_ay,
            "description": desc,
            "chunk_count": len(chunks),
        }
        print(f"[{pid}] {name}: {price_display or 'fiyat yok'} | odalar: {', '.join(room_list) or '-'} | teslim: {teslim_ay or '-'} ay | chunk: {len(chunks)}")

    PRICES_OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    if PORTFOLIO_OUT.exists():
        pf = json.loads(PORTFOLIO_OUT.read_text(encoding="utf-8"))
        updated = 0
        for item in pf:
            if item.get("type") != "project":
                continue
            info = result.get(item.get("title"))
            if not info:
                continue
            item["price_display"] = info["price_display"] or ""
            item["room_info"] = ", ".join(info["rooms"]) if info["rooms"] else ""
            if info.get("description"):
                item["description"] = info["description"]
            updated += 1
        PORTFOLIO_OUT.write_text(json.dumps(pf, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nnexa_portfolio_data.json guncellendi: {updated}/{len(result)} proje")
    else:
        print("\nnexa_portfolio_data.json bulunamadi — yalnizca fiyat dosyasi yazildi.")

    db.close()
    print(f"nexa_project_prices.json yazildi ({len(result)} proje)")


if __name__ == "__main__":
    main()
