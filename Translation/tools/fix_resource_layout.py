#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_resource_layout.py —— 把放错目录的资源挪回正确位置

典型场景：下载回来的 zip 解压后多套了一层目录，变成
    Resources/sweethomemaid-missing-2026-09-19/Card/story1040303.json
而正确的应该是
    Resources/Card/story1040303.json
（油猴脚本按目录规律找译文，放错了就找不到。）

本脚本会按剧情 id 判断每个文件该在哪个目录，挪回去；译文目录同样处理。
默认只预览，加 --write 才真的移动。已存在同名文件时不动，并报告冲突。

用法：
    python Translation/tools/fix_resource_layout.py            # 预览
    python Translation/tools/fix_resource_layout.py --write    # 执行
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sakura_translate as st

# 和油猴脚本、batch_translate 里的规律保持一致
RULES = [
    (r"^storyEV", ["Event"]),
    (r"^storyBT", ["Battle"]),
    (r"^story0", ["Main"]),
    (r"^story1", ["Card"]),
    (r"^story2", ["Main", "Card"]),
    (r"^(default_|tutorial)", ["Ordinary"]),
]


def dirs_for(story_id: str):
    for pat, dirs in RULES:
        if re.match(pat, story_id):
            return dirs
    return None


def story_id_of(path: pathlib.Path) -> str:
    try:
        node = json.loads(path.read_text(encoding="utf-8-sig"))[5]
        name = node[0][1]
        return name if isinstance(name, str) else ""
    except Exception:
        return ""


def scan(root: pathlib.Path, mode: str):
    """mode: 'src' 找剧本原文；'out' 找 _cn.json 成品。"""
    moves, conflicts, dups, unknown = [], [], [], 0
    for p in sorted(root.rglob("*.json")):
        rel = p.relative_to(root)
        if len(rel.parts) < 2:
            continue                                  # 根目录下的文件（config.json 之类）
        if mode == "out" and not p.name.endswith("_cn.json"):
            continue
        if mode == "src" and p.name.endswith("_cn.json"):
            continue
        if rel.parts[0] in ("assets", "Garden"):
            continue
        sid = story_id_of(p)
        if not sid:
            unknown += 1
            continue
        want = dirs_for(sid)
        if not want:
            unknown += 1
            continue
        if rel.parts[0] in want:
            continue                                  # 已经在合法目录里
        target = root / want[0] / rel.name
        if target.exists():
            try:
                same = p.read_bytes() == target.read_bytes()
            except OSError:
                same = False
            if same:
                dups.append((p, rel.as_posix()))          # 内容一样，是同名副本
            else:
                conflicts.append((rel.as_posix(), (want[0] + "/" + rel.name)))
        else:
            moves.append((p, target, rel.as_posix(), want[0] + "/" + rel.name))
    return moves, conflicts, dups, unknown


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="把放错目录的资源挪回正确位置")
    ap.add_argument("--root", help="仓库根目录，默认自动定位")
    ap.add_argument("--src", default="Resources", help="原文根目录")
    ap.add_argument("--out", default="Translation/Resources", help="成品根目录")
    ap.add_argument("--write", action="store_true", help="真的移动（默认只预览）")
    args = ap.parse_args()

    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    total = 0
    for label, rel_root, mode in (("原文", args.src, "src"), ("成品", args.out, "out")):
        base = root / rel_root
        if not base.is_dir():
            print(f"[skip] 没有 {rel_root}")
            continue
        moves, conflicts, dups, unknown = scan(base, mode)
        print(f"== {label}（{rel_root}）")
        print(f"   需要挪动 {len(moves)} 个；重复（内容一样，可删）{len(dups)} 个；"
              f"冲突 {len(conflicts)} 个；认不出 id 的 {unknown} 个")
        for src, dst, rel, want in moves[:8]:
            print(f"     {rel}  ->  {want}")
        if len(moves) > 8:
            print(f"     … 其余 {len(moves) - 8} 个")
        for rel, want in conflicts[:5]:
            print(f"     [冲突] {rel}（目标 {want} 已存在，没动）")
        for p, rel in dups[:5]:
            print(f"     [重复] {rel}（正确位置已有同样内容）")
        if args.write:
            moved = 0
            for src, dst, rel, want in moves:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
            print(f"   已移动 {moved} 个")
            removed = 0
            for p, rel in dups:
                try:
                    p.unlink()
                    removed += 1
                except OSError as e:
                    print(f"   [失败] 删不掉 {rel}: {e}")
            if removed:
                print(f"   已删除 {removed} 个重复文件")
            # 顺手清掉挪空了的目录
            for d in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda x: -len(x.parts)):
                if d.name in ("assets", "Garden"):
                    continue
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
        total += len(moves)
    print(f"\n合计需要挪动 {total} 个" + ("（已执行）" if args.write else "（预览，加 --write 才会真的移动）"))


if __name__ == "__main__":
    main()
