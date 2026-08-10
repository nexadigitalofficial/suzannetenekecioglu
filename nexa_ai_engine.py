#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEXA AI v2 — PROJE ZEKASI ANALİZ PİPERLİNE (FOLDER 3)
Masaüstü NEXA_PRIME_v2_ENTERPRISE veritabanından aktarılan GERÇEK proje/portföy verisiyle çalışır.
Her sorgu, bütçe / bölge / oda / amaç kıstaslarına göre GERÇEK puanlanır;
her proje için kendi verisinden (ilçe, ada/parsel, TKGM onayı, fiyat, oda, alan) üretilen
farklı gerekçeler (rationale) döndürülür.
"""

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "nexa_portfolio_data.json"
MAP_FILE = BASE_DIR / "projects_map.json"

# ─── VERİ YÜKLEME ───
def load_portfolio():
    """Öncelik: zengin veri dosyası; yoksa proje haritasına düş."""
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    if MAP_FILE.exists():
        with open(MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

# ─── NİTELİK ÇIKARIMI ───
ILCELER = [
    "çankaya", "cankaya", "etimesgut", "yenimahalle", "pursaklar", "çubuk", "cubuk",
    "gölbaşı", "golbasi", "sincan", "odunpazarı", "odunpazari", "bodrum", "alanya",
    "yahşihan", "yahsihan", "keçiören", "kecioren", "mamak", "eryaman", "batıkent",
]
MAHALLELER = [
    "beytepe", "yaşamkent", "yasamkent", "çakırlar", "cakirlar", "incek", "çayyolu",
    "cayyolu", "ümitköy", "umitkoy", "yalıkavak", "cevizlidere", "eğrikin", "egrikin",
    "horos", "mustafa kemal", "atatürk", "adil bey",
]
PROJE_SINONIMLERI = {
    "angim": "ANGİM BEYTEPE", "angim beytepe": "ANGİM BEYTEPE", "beytepe": "ANGİM BEYTEPE",
    "ankaport": "ANKAPORT - SARAY", "evart": "EVART YALIKAVAK",
    "grande": "GRANDE YAŞAMKENT", "grande yaşamkent": "GRANDE YAŞAMKENT",
    "gökdemir": "GÖKDEMİR İMZA", "gokdemir": "GÖKDEMİR İMZA",
    "idea": "IDEA - START BRAVO", "start bravo": "IDEA - START BRAVO",
    "monza": "MONZA EYLÜL CONCEPT - VIP ÇAKIRLAR",
    "monza moon": "MONZA MOON", "narcin": "NARÇİN RONYA CITY - 1 (VIP WEST)",
    "neva": "NEVA - START BRAVO", "s point": "S POINT - VIP SARAY",
    "triole": "TRIOLE YAŞAM", "verde": "VERDE MONA", "verde mona": "VERDE MONA",
    "vip akademi": "VIP AKADEMİ", "vip akademi 2": "VIP AKADEMİ 2",
    "vip marin": "VIP MARIN", "vip yaşamkent": "VIP YAŞAMKENT - GÖKDEMİR STAR",
    "vip yenikent": "VIP YENİKENT", "vip çakırlar": "VIP ÇAKIRLAR",
    "vip üniversite": "VIP ÜNİVERSİTE", "viva": "VIVA - START BRAVO",
    "wm prime": "WM - PRIME", "wm": "WM - PRIME",
}

def norm_text(t):
    return (t or "").lower().replace("ı", "i").replace("ş", "s").replace("ğ", "g").replace("ü", "u").replace("ö", "o").replace("ç", "c")

def extract_budget(text):
    """Bütçe: '5 milyon', '3.5m', '60 bin', '5000000', '5.000.000 tl' veya '5-10M' aralığı."""
    t = text.lower()
    rng = re.search(r'(\d[\d.,]*)\s*[-–]\s*(\d[\d.,]*)\s*(?:milyon|mln|m\b|bin|tl)', t)
    if rng:
        g0 = rng.group(0)
        hi = g0[-2:].lower()
        lo = float(rng.group(1).replace(',', '.') or 0)
        hi_n = float(rng.group(2).replace(',', '.'))
        if 'milyon' in g0 or 'mln' in g0 or ('m' in hi and 'bin' not in g0):
            return {"min": int(lo * 1_000_000), "max": int(hi_n * 1_000_000)}
        if 'bin' in g0:
            return {"min": int(lo * 1_000), "max": int(hi_n * 1_000)}
        return {"min": int(lo), "max": int(hi_n)}
    # Tekli: milyon / m / mln / bin
    m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(milyon|mln|m\b|bin)', t)
    if m:
        num = float(m.group(1).replace(',', '.'))
        mul = 1_000 if m.group(2) == 'bin' else 1_000_000
        return {"min": int(num * mul), "max": None}
    m2 = re.search(r'(\d{1,3}(?:\.\d{3})*)+\s*(?:tl|lira)', t)
    if m2:
        raw = re.sub(r'\D', '', m2.group(0))
        if len(raw) >= 6:
            return {"min": int(raw), "max": None}
    return None

def extract_region(text):
    """İlçe + mahalle + proje adı bölge eşleşmeleri."""
    t = norm_text(text)
    found = []
    for ilce in ILCELER:
        if ilce in t:
            label = {"cankaya": "Çankaya", "etimesgut": "Etimesgut", "yenimahalle": "Yenimahalle",
                     "pursaklar": "Pursaklar", "cubuk": "Çubuk", "golbasi": "Gölbaşı",
                     "sincan": "Sincan", "odunpazari": "Odunpazarı", "bodrum": "Bodrum",
                     "alanya": "Alanya", "yahsihan": "Yahşihan", "kecioren": "Keçiören",
                     "mamak": "Mamak", "eryaman": "Eryaman", "batikent": "Batıkent"}.get(ilce, ilce.capitalize())
            if label not in found:
                found.append(label)
    for mah in MAHALLELER:
        if mah in t:
            label = {"beytepe": "Beytepe", "yasamkent": "Yaşamkent", "cakirlar": "Çakırlar",
                     "incek": "İncek", "cayyolu": "Çayyolu", "umitkoy": "Ümitköy",
                     "yalikavak": "Yalıkavak", "cevizlidere": "Cevizlidere",
                     "egrikin": "Eğrikin", "horos": "Horos"}.get(mah, mah.capitalize())
            if label not in found:
                found.append(label)
    return found

def extract_rooms(text):
    """Oda tipi: '3+1', '4+1', '2 1' vb."""
    m = re.search(r'(\d)\s*\+\s*(\d)', text)
    if m:
        return f"{m.group(1)}+{m.group(2)}"
    return None

def extract_goals(text):
    """Yatırım amaçları ve istem tipi (satılık/kiralık)."""
    t = text.lower()
    goals = []
    if "oturum" in t or "yaşam" in t or "yasam" in t or "kendim" in t or "taşınma" in t or "tasinma" in t:
        goals.append("oturum")
    if "yatırım" in t or "yatirim" in t or "prim" in t or "kazanç" in t or "kazanc" in t or "değer" in t or "deger" in t:
        goals.append("yatirim")
    if "kiralık" in t or "kiralik" in t or "kira" in t or "kiracı" in t or "kiraci" in t or "amortisman" in t:
        goals.append("kiralik")
    want_type = None
    if "kiralık" in t or "kiralik" in t or "kira" in t:
        want_type = "Kiralık"
    elif "satılık" in t or "satilik" in t or "satın" in t or "satin" in t or "alma" in t:
        want_type = "Satılık"
    return goals, want_type

def extract_keywords_and_projects(text):
    """Kullanıcının sorgusunda adı geçen projeler."""
    t = norm_text(text)
    hits = [title for key, title in PROJE_SINONIMLERI.items() if key in t]
    return list(dict.fromkeys(hits))

# ─── PUANLAMA ───
def price_numeric(item):
    """Portföy fiyatını sayıya çevirir (Kiralık'ta aylık)."""
    pd = item.get("price_display") or ""
    m = re.search(r'([\d.,]+)\s*₺', pd)
    if not m:
        return None
    return int(float(m.group(1).replace('.', '').replace(',', '.')))

def score_item(item, budget, regions, rooms, goals, want_type, named_projects):
    """Her kayıt için gerçek veriyle puan üretir."""
    score = 20
    parts = []

    ilce = norm_text(item.get("ilce") or "")
    il = norm_text(item.get("il") or "")
    mahalle = norm_text(item.get("mahalle") or "")
    title = item.get("title") or ""
    t = norm_text(title)

    # 1) Bölge eşleşmesi
    region_hit = False
    for reg in regions:
        rn = norm_text(reg)
        if rn and (rn in ilce or rn in mahalle or rn in t or (rn == "ankara" and rn in il)):
            score += 35
            region_hit = True
            parts.append(f"Bölgeniz {reg} ile eşleşiyor")
            break
    if not region_hit and regions and regions[0] not in ("Ankara",) and ilce:
        # sorguda bölge yoksa puanlama tarafsız kalır
        pass

    # 2) Proje adı eşleşmesi (doğrudan arama)
    if title in named_projects or any(np in t for np in named_projects):
        score += 30
        parts.append("Sorgunuzda bu projenin adi gecti")
    elif any(syn in t for syn in PROJE_SINONIMLERI):
        pass

    # 3) Oda eşleşmesi
    if rooms:
        room_info = norm_text(item.get("room_info") or "")
        if rooms in room_info or re.search(re.escape(rooms), room_info):
            score += 25
            parts.append(f"{rooms} daire tipi aranizla uyumlu")
        elif room_info and ("daire" in room_info or "+" in room_info):
            score += 5
            parts.append(f"Oda tipi: {item.get('room_info')}")

    # 4) Bütçe eşleşmesi (sayısal fiyatı olanlar için)
    pn = price_numeric(item)
    is_rent = item.get("listing_type") == "Kiralık"
    if budget and pn:
        if is_rent:
            # aylık kira, bütçe doğrudan aylık karşılaştırılır
            if budget["min"] and budget["min"] >= pn * 0.85:
                score += 25
                parts.append(f"Aylik {item.get('price_display')} kira bütcenize uygun")
            elif budget["min"]:
                score += 12
                parts.append(f"Aylik kira: {item.get('price_display')}")
        else:
            if budget["max"] and (budget["min"] or 0) * 0.85 <= pn <= budget["max"] * 1.05:
                score += 25
                parts.append(f"{item.get('price_display')} fiyati bütce araliginizda")
            elif budget["min"] and budget["min"] <= pn * 1.15:
                score += 20
                parts.append(f"{item.get('price_display')} fiyati bütce araliginizda")
            else:
                score += 5
                parts.append(f"Fiyat: {item.get('price_display')}")
    elif budget and not pn:
        score += 8
        parts.append("Guncel fiyat icin danisman bilgi verebilir")

    # 5) İlan tipi (satılık / kiralık)
    if want_type and item.get("listing_type") == want_type:
        score += 15
        parts.append(f"{want_type} istemiyle uyumlu")
    elif want_type and item.get("listing_type"):
        score -= 15

    # 6) TKGM onayı
    if item.get("tkgm_verified"):
        score += 6
        parts.append(f"TKGM onayli parsel (Ada {item.get('ada_no')}/{item.get('parsel_no')})")

    # 7) Kiralık hedefinde kiralık ilanlara öncelik
    if "kiralik" in goals and is_rent:
        score += 12
    if "yatirim" in goals and not is_rent:
        score += 8
    if "oturum" in goals and item.get("property_category") in ("Konut / Daire", "Villa"):
        score += 6

    # 8) Kriter belirtilmediyse (ör. "En Uygun Projeler") Ankara portföyü öne çıkar
    ankara_scope = (not regions) or (len(regions) == 1 and norm_text(regions[0]) == "ankara")
    if ankara_scope and not named_projects and not rooms and not budget:
        if il == "ankara":
            score += 10
            parts.append("Ankara portföyünde öne çıkan proje")

    return max(15, min(99, score)), parts

def item_region_label(item):
    return item.get("ilce") or item.get("mahalle") or "Ankara"

def item_price_label(item):
    pd = item.get("price_display")
    return pd if pd else "Fiyat Sorulacak"

def _load_project_summaries():
    try:
        p = Path(__file__).parent / "nexa_project_summaries.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def build_rationale(item, parts):
    """Gerçek verilerden türetilmiş gerekçe."""
    extra = []
    if item.get("ada_no"):
        extra.append(f"Ada {item.get('ada_no')} / Parsel {item.get('parsel_no')}")
    if item.get("tkgm_verified"):
        extra.append("TKGM onaylı")
    if item.get("room_info"):
        extra.append(item.get("room_info"))
    if item.get("net_gross_area"):
        extra.append(item.get("net_gross_area"))
    loc = item_region_label(item)
    lead = f"{loc} bölgesinde {item.get('title')}"
    txt = " • ".join(parts) if parts else "Kriterlerinize yüksek uyum göstermektedir"
    if extra:
        txt += f". Kayıtlı veri: {item.get('title')} ({', '.join(extra)})"
    return f"{lead}. {txt}."

# ─── ANA İŞLEME ───
def process_nexa_query(user_query):
    try:
        items = load_portfolio()
    except Exception:
        items = []
    q = user_query.strip()
    ql = norm_text(q)

    budget = extract_budget(q)
    regions = extract_region(q)
    rooms = extract_rooms(q)
    goals, want_type = extract_goals(q)
    named_projects = extract_keywords_and_projects(q)

    scored = []
    for it in items:
        if it.get("type") == "portfolio":
            continue
        s, parts = score_item(it, budget, regions, rooms, goals, want_type, named_projects)
        scored.append((s, it, parts))
    scored.sort(key=lambda x: -x[0])

    cbs = [(s, it, p) for s, it, p in scored if it.get("type") == "project" and not it["id"].startswith("cb-")]
    if regions:
        # Bölge sorulduysa yalnızca o bölgedeki projeler gösterilir
        region_hits = [x for x in cbs if any("eşleşiyor" in (p or "") for p in x[2])]
        if region_hits:
            cbs = region_hits
    if named_projects:
        # Proje adı ile sorulduysa yalnızca adı geçen projeler gösterilir
        name_hits = [x for x in cbs if any("adi gecti" in (p or "") for p in x[2])]
        if name_hits:
            cbs = name_hits
    cb_matches = cbs[:3]

    # Rapor başlığı
    def fmt_money(v):
        return f"{v/1_000_000:g} Milyon TL" if v >= 1_000_000 else f"{v/1_000:g} Bin TL"
    bütçe_str = "Belirtilmedi"
    if budget:
        if budget["max"]:
            bütçe_str = f"{fmt_money(budget['min'])} - {fmt_money(budget['max'])}"
        else:
            bütçe_str = fmt_money(budget["min"])
    bölge_str = ", ".join(regions) if regions else "Ankara Genel"
    amaç_map = {"oturum": "Oturum", "yatirim": "Yatırım", "kiralik": "Kiralık"}
    amaç_str = ", ".join(amaç_map.get(g, g.capitalize()) for g in goals) if goals else "Oturum + Yatırım"

    lines = [
        f"**Nexa AI Proje Zekası Analiz Raporu:**\n",
        f"• **Bütçe:** {bütçe_str}",
        f"• **Bölge:** {bölge_str}",
        f"• **Oda Tercihi:** {rooms or 'Belirtilmedi'}",
        f"• **Yatırım Amacı:** {amaç_str}",
        "",
        "Tüm portföy proje verileri, ada/parsel ve TKGM kayıtlarıyla taranarak **gerçek uyum puanları** hesaplandı:",
    ]

    if not cb_matches:
        lines.append("\n_Ölçütlerinizle eşleşen markalı proje bulunamadı; portföy verileri değerlendirildi._")

    # Çekirdek proje kartları
    for s, it, parts in cb_matches:
        ip = item_price_label(it)
        label = f"**{it['title']}** — %{s} Uyumlu\n📍 {item_region_label(it)} • 💰 {ip}"
        lines.append(f"\n{label}\n💡 {build_rationale(it, parts)}")

    lines.append("\n---\n_Detaylı sunum, güncel fiyat listesi ve parsel raporları için **0535 489 56 56** WhatsApp hattından ulaşabilirsiniz._")

    # Kart formatı (site.html uyumlu)
    summaries = _load_project_summaries()
    project_cards = []
    for s, it, parts in cb_matches:
        ozet = summaries.get(it["title"], {}).get("summary") or ""
        project_cards.append({
            "id": it["id"],
            "db_id": it.get("db_id"),
            "title": it["title"],
            "region": item_region_label(it),
            "price_display": item_price_label(it),
            "match_percent": s,
            "rationale": ozet or build_rationale(it, parts),
            "summary": ozet,
            "media": {
                "promo_video_url": it.get("tanitim_cloud_url") or it.get("cloud_direct_url") or it.get("cloud_video_url") or f"/stream/video/{it['id']}",
                "slideshow_video_url": it.get("slideshow_cloud_url") or it.get("cloud_video_url") or f"/stream/video/{it['id']}",
                "pdf_url": ("/" + it["pdf_path"]) if it.get("pdf_path") else "",
                "thumbnail_url": it.get("thumbnail") or "/static/img/pdf_previews/pdf_cover_1.png"
            }
        })

    return {
        "success": True,
        "response": "\n".join(lines),
        "extracted_info": {
            "budget_tl": budget,
            "regions": regions,
            "rooms": rooms,
            "goals": goals
        },
        "projects": project_cards
    }

if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    tests = [
        "Ankara'da 3+1 proje arıyorum, bütçem 5 milyon",
        "Çankaya İlanları",
        "En Uygun Projeler",
        "Kiralık ofis arıyorum Çankaya'da bütçem 60 bin",
        "5-10M yatırım için lüks proje önerir misin?",
        "Gölbaşı'nda arsa veya villa var mı?",
        "MONZA MOON hakkında bilgi ver",
    ]
    for t in tests:
        print("=" * 70)
        print("SORU:", t)
        res = process_nexa_query(t)
        print(res["response"])
        for p in res["projects"]:
            print(f"  -> {p['title']} | %{p['match_percent']} | {p['region']} | {p['price_display']}")
        print()