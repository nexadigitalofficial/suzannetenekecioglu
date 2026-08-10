#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
COLDWELL BANKER VIP — DUAL CLOUD UPLOADER (FOLDER 3)
Uploads BOTH:
1) Real Main TANITIM/LANSMAN MP4 -> tanitim_cloud_url
2) PDF SLIDESHOW MP4 -> slideshow_cloud_url

Updates Desktop/3 projects_map.json with both direct HTTPS Cloud CDN URLs.
"""

import sys
import json
import time
import requests
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PROJELER_DIR = Path("c:/Users/USER/Desktop/1/projeler")
JSON_MAP_3 = Path("c:/Users/USER/Desktop/3/projects_map.json")

def upload_to_litterbox(file_path):
    """Upload to Litterbox.catbox.moe (Supports up to 1GB direct stream URL)."""
    try:
        size_mb = round(file_path.stat().st_size / (1024 * 1024), 2)
        print(f"    [⬆️ Litterbox Cloud] ({size_mb} MB): {file_path.name}...")
        with open(file_path, 'rb') as f:
            resp = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": f},
                timeout=300
            )
        if resp.status_code == 200 and resp.text.startswith("http"):
            cloud_url = resp.text.strip()
            print(f"    [✓ Litterbox Cloud] Bağlantı: {cloud_url}")
            return cloud_url
    except Exception as e:
        print(f"    [❌ Litterbox Hata]: {e}")
    return None

def upload_to_catbox(file_path):
    """Upload to Catbox.moe (Supports up to 200MB direct stream URL)."""
    try:
        size_mb = round(file_path.stat().st_size / (1024 * 1024), 2)
        if size_mb > 195:
            return None
        print(f"    [⬆️ Catbox Cloud] ({size_mb} MB): {file_path.name}...")
        with open(file_path, 'rb') as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=180
            )
        if resp.status_code == 200 and resp.text.startswith("http"):
            cloud_url = resp.text.strip()
            print(f"    [✓ Catbox Cloud] Bağlantı: {cloud_url}")
            return cloud_url
    except Exception as e:
        print(f"    [❌ Catbox Hata]: {e}")
    return None

def upload_file_to_cloud(file_path):
    if file_path.stat().st_size < 190 * 1024 * 1024:
        url = upload_to_catbox(file_path)
        if url: return url
    return upload_to_litterbox(file_path)

def batch_upload_dual_videos():
    print("\n" + "=" * 80)
    print(" 🎬 COLDWELL BANKER VIP — İKİLİ BULUT VİDEO YÜKLEYİCİ (TANITIM + SLIDESHOW)")
    print("=" * 80 + "\n")

    if not JSON_MAP_3.exists():
        print(f"[❌] projects_map.json bulunamadı: {JSON_MAP_3}")
        return

    with open(JSON_MAP_3, "r", encoding="utf-8") as f:
        projects = json.load(f)

    for idx, project in enumerate(projects, start=1):
        print(f"[{idx}/{len(projects)}] {project['title']}")
        folder_name = project['folder_name']
        folder_path = PROJELER_DIR / folder_name

        if not folder_path.exists():
            print(f"  [⚠️] Klasör bulunamadı: {folder_path}")
            continue

        mp4_files = sorted(folder_path.glob("*.mp4"))

        # Find Real TANITIM video
        tanitim_video = None
        for f in mp4_files:
            if f.stat().st_size > 500 * 1024 and not f.name.startswith("SLIDESHOW"):
                tanitim_video = f
                break

        # Find SLIDESHOW video
        slideshow_video = None
        for f in mp4_files:
            if f.name.startswith("SLIDESHOW"):
                slideshow_video = f
                break

        # 1) Upload TANITIM video if present
        if tanitim_video:
            existing_t = project.get("tanitim_cloud_url", "")
            existing_f = project.get("tanitim_filename", "")
            if existing_t and existing_f == tanitim_video.name and ("catbox" in existing_t or "tmpfiles" in existing_t):
                print(f"  [ℹ️ Tanıtım Videosu Yüklü]: {existing_t}")
            else:
                print(f"  [🎯 Tanıtım Videosu]: {tanitim_video.name}")
                t_url = upload_file_to_cloud(tanitim_video)
                if t_url:
                    project["tanitim_cloud_url"] = t_url
                    project["tanitim_filename"] = tanitim_video.name
                    project["cloud_direct_url"] = t_url
                    project["cloud_video_url"] = t_url

        # 2) Upload SLIDESHOW video if present
        if slideshow_video:
            existing_s = project.get("slideshow_cloud_url", "")
            existing_sf = project.get("slideshow_filename", "")
            if existing_s and existing_sf == slideshow_video.name and ("catbox" in existing_s or "tmpfiles" in existing_s):
                print(f"  [ℹ️ Slayt Videosu Yüklü]: {existing_s}")
            else:
                print(f"  [📸 Slayt Videosu]: {slideshow_video.name}")
                s_url = upload_file_to_cloud(slideshow_video)
                if s_url:
                    project["slideshow_cloud_url"] = s_url
                    project["slideshow_filename"] = slideshow_video.name

        # Save progress after each project
        with open(JSON_MAP_3, "w", encoding="utf-8") as f_out:
            json.dump(projects, f_out, ensure_ascii=False, indent=2)

        time.sleep(1)

    print("\n" + "=" * 80)
    print(" [SUMMARY] İkili Bulut Yükleme İşlemi Tamamlandı!")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    batch_upload_dual_videos()
