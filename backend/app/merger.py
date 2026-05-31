import os
import re
import subprocess
import uuid


def _get_video_codec(filepath: str) -> str:
    """获取视频轨的编码名称，用于校验各分P编码一致性。"""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'quiet',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'csv=p=0',
            filepath
        ], capture_output=True, text=True, timeout=30)
        return result.stdout.strip()
    except Exception:
        return ''


def _get_duration_ms(filepath: str) -> int:
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'quiet',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0',
            filepath
        ], capture_output=True, text=True, timeout=30)
        return int(float(result.stdout.strip()) * 1000)
    except Exception:
        return 0


def _ms_to_hhmmss(ms: int) -> str:
    """毫秒 → HH:MM:SS.nnnnnnnnn（mkvmerge 章节 XML 格式）。"""
    total_sec = ms / 1000.0
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:012.9f}"


def _clean_chapter_title(raw_title: str) -> str:
    """从分P完整标题中提取章节名。

    输入示例:
      '【SNH48】TEAM NII原创公演《时之卷》20201227 p01 《雪之国》...'
    输出:
      '《雪之国》《光明与黑暗》《命运齿轮》《十洲集结》'
    """
    # 去除完整标题中「p01」「P02」之前的所有公共前缀
    cleaned = re.sub(r'^.*?[pP]\s*\d{2,3}\s*', '', raw_title).strip()
    if cleaned:
        return cleaned
    # 回退：只去除 「P1.」「P1-」等简洁前缀
    cleaned = re.sub(r'^[Pp]\d+[\.\-\s、】]*', '', raw_title).strip()
    return cleaned if cleaned else raw_title


def _generate_chapter_xml(part_files: list[str], part_titles: list[str]) -> str:
    """生成 Matroska 章节 XML 文件，返回文件路径。"""
    xml_path = f"/tmp/chapters_{uuid.uuid4().hex}.xml"
    cumulative_ms = 0

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Chapters>',
        '  <EditionEntry>',
    ]

    for pf, pt in zip(part_files, part_titles):
        dur_ms = _get_duration_ms(pf)
        if dur_ms <= 0:
            continue
        start_str = _ms_to_hhmmss(cumulative_ms)
        end_str = _ms_to_hhmmss(cumulative_ms + dur_ms)
        chapter_name = _clean_chapter_title(pt)

        lines.append('    <ChapterAtom>')
        lines.append(f'      <ChapterTimeStart>{start_str}</ChapterTimeStart>')
        lines.append(f'      <ChapterTimeEnd>{end_str}</ChapterTimeEnd>')
        lines.append('      <ChapterDisplay>')
        lines.append(f'        <ChapterString>{chapter_name}</ChapterString>')
        lines.append('        <ChapterLanguage>chi</ChapterLanguage>')
        lines.append('      </ChapterDisplay>')
        lines.append('    </ChapterAtom>')

        cumulative_ms += dur_ms

    lines.append('  </EditionEntry>')
    lines.append('</Chapters>')

    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return xml_path


def merge_parts(part_files: list[str], part_titles: list[str],
                output_path: str) -> bool:
    """使用 mkvmerge 无损合并 MKV 分P，嵌入 Matroska 章节标记。

    mkvmerge 能正确处理 MKV 容器层面的拼接，避免 ffmpeg concat
    demuxer 在跨分P边界时的解码器状态断裂问题。
    """
    if len(part_files) < 2:
        return False

    # 编码一致性校验：不同编码的分P无法无损拼接
    codecs = {}
    for pf in part_files:
        c = _get_video_codec(pf)
        codecs.setdefault(c, []).append(os.path.basename(pf))
    if len(codecs) > 1:
        print(f"  ❌ 分P编码不一致，无法合并:")
        for c, files in codecs.items():
            print(f"     {c}: {len(files)} 个文件 ({files[0]} ...)")
        return False

    mkv_output = f"{output_path}.mkv"
    temp_merged = f"{output_path}.temp.mkv"
    chapter_xml = _generate_chapter_xml(part_files, part_titles)

    try:
        # mkvmerge 拼接命令: mkvmerge -o out.mkv --chapters ch.xml in1.mkv + in2.mkv + ...
        # 使用 "+" 分隔每个输入文件实现追加拼接
        cmd = ['mkvmerge', '-o', temp_merged, '--chapters', chapter_xml]
        for i, pf in enumerate(part_files):
            if i > 0:
                cmd.append('+')
            cmd.append(pf)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        # mkvmerge 退出码: 0=成功, 1=成功但有警告, >=2=错误
        if result.returncode >= 2:
            err_detail = result.stderr or result.stdout
            print(f"  ❌ mkvmerge 合并失败 (rc={result.returncode}): {err_detail[-500:]}")
            return False
        if not os.path.exists(temp_merged):
            print(f"  ❌ mkvmerge 未生成输出文件")
            return False
        if result.returncode == 1:
            print(f"  ⚠ mkvmerge 完成（有非致命警告）")

        # 原子替换
        os.replace(temp_merged, mkv_output)

        # 打印摘要
        total_ms = sum(_get_duration_ms(pf) for pf in part_files)
        print(f"  ✓ 合并完成: {mkv_output} ({len(part_files)} 个分P → {total_ms/1000:.0f}s, {len(part_titles)} 个章节)")

        # 清理分P文件
        for pf in part_files:
            try:
                os.remove(pf)
            except OSError:
                pass
            base = os.path.splitext(pf)[0]
            for ext in ('.danmaku.xml', '.nfo'):
                extra = base + ext
                try:
                    os.remove(extra)
                except OSError:
                    pass

        return True

    except Exception as e:
        print(f"  ❌ 合并过程异常: {e}")
        return False

    finally:
        for tmp in (chapter_xml, temp_merged):
            try:
                os.remove(tmp)
            except OSError:
                pass
