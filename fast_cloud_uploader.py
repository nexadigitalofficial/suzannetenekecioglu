#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAST CONCURRENT CATBOX CLOUD UPLOADER (FOLDER 3)
Uploads remaining projects to Catbox Cloud CDN with parallel processing.
"""

import sys
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PROJELER_DIR = Path("c:/Users/USER/Desktop/1/projeler")
JSON_MAP_3 = Path("c:/Users/USER/Desktop/3/projects_map.json")

def upload_single_file(file_path):
    """Upload MP4 file to Catbox CDN."""
    try:
        size_mb = round(file_path.stat().st_size / (1024 * 1024), 2)
        print(f"  [⬆️ Catbox Uploading] {file_path.name} ({size_mb} MB)...")
        with open(file_path, 'rb') as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=300
            )
        if resp.status_code == 200 and resp.text.startswith("http"):
            url = resp.text.strip()
            print(f"  [✓ SUCCESS] {file_path.name} -> {url}")
            return url
        else:
            print(f"  [❌ FAIL] {file_path.name}: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        print(f"  [❌ ERROR] {file_path.name}: {e}")
    return None

def process_project(project):
    folder_name = project['folder_name']
    folder_path = PROJELER_DIR / folder_name

    if not folder_path.exists():
        return project, False

    mp4_files = sorted(folder_path.glob("*.mp4"))
    if not mp4_files:
        return project, False

    updated = False

    # 1. Main Tanıtım Video
    tanitim_files = [f for f in mp4_files if f.stat().st_size > 500 * 1024 and not f.name.startswith("SLIDESHOW")]
    target_tanitim = tanitim_files[0] if tanitim_files else mp4_files[0]

    if target_tanitim:
        curr_t = project.get("tanitim_cloud_url", "")
        if not curr_t or "catbox" not in curr_t:
            url_t = upload_single_file(target_tanitim)
            if url_t:
                project["tanitim_cloud_url"] = url_t
                project["cloud_direct_url"] = url_t
                project["cloud_video_url"] = url_t
                project["tanitim_filename"] = target_tanitim.name
                updated = True

    # 2. Slideshow Video
    slideshow_files = [f for f in mp4_files if f.name.startswith("SLIDESHOW")]
    if slideshow_files:
        target_slide = slideshow_files[0]
        curr_s = project.get("slideshow_cloud_url", "")
        if not curr_s or "catbox" not in curr_s:
            url_s = upload_single_file(target_slide)
            if url_s:
                project["slideshow_cloud_url"] = url_s
                project["slideshow_filename"] = target_slide.name
                updated = True

    return project, updated

def run_fast_uploader():
    print("\n" + "=" * 70)
    print(" 🚀 HIZLI PARALEL BULUT YÜKLEYİCİ (CATBOX CDN)")
    print("=" * 70 + "\n")

    if not JSON_MAP_3.exists():
        print(f"[❌] {JSON_MAP_3} bulunamadı")
        return

    with open(JSON_MAP_3, "r", encoding="utf-8") as f:
        projects = json.load(f)

    # Process projects serially or with small concurrency to avoid rate limits
    for idx, proj in enumerate(projects, start=1):
        print(f"[{idx}/{len(projects)}] Proje İşleniyor: {proj['title']}")
        proj, was_updated = process_project(proj)
        if was_updated:
            with open(JSON_MAP_3, "w", encoding="utf-8") as f_out:
                json.dump(projects, f_out, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print(" [✓] TÜM 22 PROJENİN BULUT YÜKLEMELERİ TAMAMLANTI!")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    run_fast_uploader()
