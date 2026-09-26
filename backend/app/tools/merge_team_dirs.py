#!/usr/bin/env python3
"""把媒体库中一个队伍目录合并进另一个队伍目录（队伍改名/合并时使用）。

在宿主机运行（需要 python3）。默认 dry-run，加 --apply 才执行：
  1. 按 Season 目录逐个移动场次文件夹（同名冲突跳过并报告）
  2. 更新 downloads 表里的路径前缀
  3. 把旧的队伍目录外壳移动到媒体库外的备份目录

用法：
  python3 tools/merge_team_dirs.py --src YBS --dst 预备生            # 预览
  python3 tools/merge_team_dirs.py --src YBS --dst 预备生 --apply    # 执行
"""

import argparse
import os
import shutil
import sqlite3
import time


def main():
    parser = argparse.ArgumentParser(description="合并媒体库中的队伍目录")
    parser.add_argument("--src", required=True, help="源队伍目录名，如 YBS")
    parser.add_argument("--dst", required=True, help="目标队伍目录名，如 预备生")
    parser.add_argument("--library-root", default="/mnt/nas/anime/video/live48")
    parser.add_argument("--db", default="/root/snh48-downloader/config/snh48.db")
    parser.add_argument("--backup-root", default="/mnt/nas/anime/video/live48_merged_backup")
    parser.add_argument("--apply", action="store_true", help="真正执行合并")
    args = parser.parse_args()

    src_dir = os.path.join(args.library_root, args.src)
    dst_dir = os.path.join(args.library_root, args.dst)
    if not os.path.isdir(src_dir):
        raise SystemExit(f"源目录不存在: {src_dir}")
    if not os.path.isdir(dst_dir):
        raise SystemExit(f"目标目录不存在: {dst_dir}")

    moves = []
    conflicts = []
    for season in sorted(os.listdir(src_dir)):
        season_src = os.path.join(src_dir, season)
        if not season.startswith("Season") or not os.path.isdir(season_src):
            continue
        season_dst = os.path.join(dst_dir, season)
        for name in sorted(os.listdir(season_src)):
            src_path = os.path.join(season_src, name)
            if not os.path.isdir(src_path):
                continue   # 文件（海报/season.nfo）留在旧外壳，随后一起备份
            dst_path = os.path.join(season_dst, name)
            if os.path.exists(dst_path):
                conflicts.append((src_path, dst_path))
            else:
                moves.append((season, src_path, dst_path))

    print(f"计划移动 {len(moves)} 个场次目录，冲突 {len(conflicts)} 个")
    for season, src_path, _ in moves:
        print(f"  [{season}] {os.path.basename(src_path)}")
    for _, dst_path in conflicts:
        print(f"  ⚠️ 已存在，跳过: {dst_path}")

    if not args.apply:
        print("dry-run：未做任何修改")
        return

    for _, src_path, dst_path in moves:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        os.rename(src_path, dst_path)

    src_prefix = f"/downloads/{args.src}/"
    dst_prefix = f"/downloads/{args.dst}/"
    conn = sqlite3.connect(args.db)
    cursor = conn.execute(
        "UPDATE downloads SET path = REPLACE(path, ?, ?) WHERE path LIKE ?",
        (src_prefix, dst_prefix, f"{src_prefix}%"),
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"数据库路径已更新: {updated} 条")

    backup_dir = os.path.join(
        args.backup_root, f"{args.src}-merged-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(args.backup_root, exist_ok=True)
    shutil.move(src_dir, backup_dir)
    print(f"旧目录外壳已备份到: {backup_dir}")


if __name__ == "__main__":
    main()
