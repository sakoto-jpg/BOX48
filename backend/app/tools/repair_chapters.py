#!/usr/bin/env python3
"""修复历史 mkv 的章节标题（只改章节名，不重新编码）。

默认 dry-run，只扫描并打印需要修复的文件；加 --apply 才用 mkvpropedit 写回。

容器内用法：
  python tools/repair_chapters.py            # 扫描
  python tools/repair_chapters.py --apply    # 修复
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import uuid
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merger import _clean_chapter_title  # noqa: E402


def read_chapters(path: str):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(result.stdout or "{}").get("chapters") or []
    except json.JSONDecodeError:
        return []


def _to_hhmmss(value) -> str:
    total = float(value)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:012.9f}"


def build_xml(chapters) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<Chapters>", "  <EditionEntry>"]
    for chapter in chapters:
        title = (chapter.get("tags") or {}).get("title", "")
        lines.append("    <ChapterAtom>")
        lines.append(f"      <ChapterTimeStart>{_to_hhmmss(chapter['start_time'])}</ChapterTimeStart>")
        if chapter.get("end_time"):
            lines.append(f"      <ChapterTimeEnd>{_to_hhmmss(chapter['end_time'])}</ChapterTimeEnd>")
        lines.append("      <ChapterDisplay>")
        lines.append(f"        <ChapterString>{escape(title)}</ChapterString>")
        lines.append("        <ChapterLanguage>chi</ChapterLanguage>")
        lines.append("      </ChapterDisplay>")
        lines.append("    </ChapterAtom>")
    lines.append("  </EditionEntry>")
    lines.append("</Chapters>")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="修复 mkv 章节标题")
    parser.add_argument("--root", default="/downloads")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    files = glob.glob(os.path.join(args.root, "**", "*.mkv"), recursive=True)
    affected = []
    for path in files:
        chapters = read_chapters(path)
        if not chapters:
            continue
        changes = []
        for chapter in chapters:
            old = (chapter.get("tags") or {}).get("title", "")
            new = _clean_chapter_title(old)
            if new != old:
                changes.append((old, new))
        if changes:
            affected.append((path, chapters, changes))

    print(f"扫描 {len(files)} 个文件，章节标题需要修复: {len(affected)} 个")
    for path, _, changes in affected:
        print(f"  {os.path.basename(path)}")
        for old, new in changes[:2]:
            print(f"    - {old[:70]}")
            print(f"    + {new[:70]}")

    if not args.apply:
        print("dry-run：未修改任何文件")
        return

    backup_dir = "/tmp/chapter_backup"
    os.makedirs(backup_dir, exist_ok=True)
    for path, chapters, _ in affected:
        original_xml = build_xml(chapters)
        backup_path = os.path.join(backup_dir, os.path.basename(path) + ".xml")
        with open(backup_path, "w", encoding="utf-8") as fh:
            fh.write(original_xml)

        for chapter in chapters:
            chapter["tags"] = dict(chapter.get("tags") or {})
            chapter["tags"]["title"] = _clean_chapter_title(chapter["tags"].get("title", ""))
        tmp_xml = f"/tmp/chapters_fix_{uuid.uuid4().hex}.xml"
        with open(tmp_xml, "w", encoding="utf-8") as fh:
            fh.write(build_xml(chapters))
        try:
            result = subprocess.run(
                ["mkvpropedit", path, "--chapters", tmp_xml],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode == 0:
                print(f"  ✓ 已修复 {os.path.basename(path)}")
            else:
                print(f"  ✗ 修复失败 {os.path.basename(path)} rc={result.returncode} "
                      f"{(result.stderr or '')[-200:]}")
        finally:
            if os.path.exists(tmp_xml):
                os.remove(tmp_xml)


if __name__ == "__main__":
    main()
