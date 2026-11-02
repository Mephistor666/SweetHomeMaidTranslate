#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_dialogue_wrappers.py —— 清掉译文里多余的「说话人『台词』」外壳

Sakura 有时会把输入里的 名字「原文」 格式整个复述回来，而且用的名字可能来自
术语表的昵称（例如把 紅葉 写成「小红叶」），于是译文里出现：

    小红叶「如果房东不明白的话，」

游戏本身会显示名字和引号，这些外壳是多余的。本脚本以「原文那一行没有「」」
为准，把外层的 名字「...」 剥掉。

两处一起改，避免以后重跑 inject 把译文又改回去：
  - Translation/work/translated.jsonl（翻译记录，inject 的数据源）
  - Translation/Resources/**.json（已回填的成品，用 Resources/ 里的原文对齐）

用法：
    python Translation/tools/fix_dialogue_wrappers.py --check   # 只报告，不改
    python Translation/tools/fix_dialogue_wrappers.py           # 修复
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import sakura_translate as st
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"[错误] 需要同目录下的 sakura_translate.py：{e}")

# 名字「台词」——名字 1~12 字、不含引号
WRAP_RE = re.compile(r"^(?P<name>[^「」\n]{1,12})「(?P<body>.*)」$", re.S)


def unwrap(text: str, source: str) -> tuple[str, str | None]:
    """按原文形态剥掉译文外壳，返回 (新文本, 说明)；不需要改就原样返回。

    真正的剥离规则在 sakura_translate.unwrap_dialogue_wrapper 里，
    流水线和本脚本共用同一份实现，避免两边判断不一致。
    """
    if not isinstance(text, str) or not text.strip():
        return text, None
    new = st.unwrap_dialogue_wrapper(text, source)
    if new == text:
        return text, None
    m = WRAP_RE.match(text.strip())
    why = f"去掉外壳「{m.group('name')}」" if m else "去掉多余的引号"
    return new, why


def fix_jsonl(path: pathlib.Path, dry: bool, verbose: bool) -> tuple[int, int]:
    """修 translated.jsonl，返回 (改动行数, 改动块数)。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, changed_lines, changed_blocks, samples = [], 0, 0, []
    for line in lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        if "_meta" in rec:
            out.append(line)
            continue
        src, zh = rec.get("src") or [], rec.get("zh") or []
        span = [max(1, int(x)) for x in (rec.get("zh_span") or [1] * len(zh))]
        if zh and sum(span) == len(src):
            hit = 0
            new_zh = []
            si = 0
            for i, b in enumerate(zh):
                a = src[si] if si < len(src) else ""
                si += max(1, int(span[i]))
                nb, why = unwrap(b, a)
                if why:
                    hit += 1
                    if verbose and len(samples) < 12:
                        samples.append((rec.get("id"), why, b, nb))
                new_zh.append(nb)
            if hit:
                rec["zh"] = new_zh
                changed_lines += hit
                changed_blocks += 1
                line = json.dumps(rec, ensure_ascii=False)
        out.append(line)

    for i, (rid, why, before, after) in enumerate(samples):
        print(f"    {rid}\n      {why}\n      - {before}\n      + {after}")
    if not dry and changed_lines:
        path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return changed_lines, changed_blocks


def fix_json_files(src_root: pathlib.Path, out_root: pathlib.Path, dry: bool, verbose: bool):
    """修已回填的成品 JSON，用 Resources/ 里的原文逐行对齐。"""
    total_lines = total_files = 0
    for out_path in sorted(out_root.rglob("*.json")):
        rel = out_path.relative_to(out_root)
        src_path = src_root / rel
        if not src_path.exists():
            print(f"[warn] 找不到原文，跳过：{rel.as_posix()}")
            continue
        raw = out_path.read_bytes()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        text_out = raw.decode("utf-8-sig")
        data = json.loads(text_out)
        path_out = st.find_text_path(data)
        if path_out is None:
            print(f"[warn] 定位不到正文，跳过：{rel.as_posix()}")
            continue

        src_data = json.loads(src_path.read_text(encoding="utf-8-sig"))
        path_src = st.find_text_path(src_data)
        src_lines, _ = st.split_lines(st.get_at(src_data, path_src))
        out_lines, sep = st.split_lines(st.get_at(data, path_out))
        if len(src_lines) != len(out_lines):
            print(f"[warn] 行数不一致（{len(src_lines)} / {len(out_lines)}），跳过：{rel.as_posix()}")
            continue

        hit, samples = 0, []
        for i, (a, b) in enumerate(zip(src_lines, out_lines)):
            if a.startswith(("@", "$$", "//")):
                continue
            nb, why = unwrap(b, a)
            if why:
                out_lines[i] = nb
                hit += 1
                if verbose and len(samples) < 8:
                    samples.append((i, why, b, nb))

        if not hit:
            print(f"[ok]   {rel.as_posix()}：无需修改")
            continue
        total_lines += hit
        total_files += 1
        print(f"[fix]  {rel.as_posix()}：{hit} 行")
        for i, why, before, after in samples:
            print(f"    L{i} {why}\n      - {before}\n      + {after}")
        if dry:
            continue

        st.set_at(data, path_out, sep.join(out_lines))
        style = st.detect_style(text_out)
        out_path.write_bytes(st.dump_like(data, style, has_bom))
    return total_lines, total_files


def main() -> None:
    parser = argparse.ArgumentParser(description="清掉译文里多余的 名字「台词」 外壳")
    parser.add_argument("--root", help="仓库根目录，默认自动定位")
    parser.add_argument("--src", default="Resources", help="原文根目录")
    parser.add_argument("--out", default="Translation/Resources", help="译文根目录")
    parser.add_argument("--jsonl", default="Translation/work/translated.jsonl", help="翻译记录")
    parser.add_argument("--check", action="store_true", help="只报告，不写盘")
    parser.add_argument("--quiet", action="store_true", help="只打印统计")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    src_root = root / args.src
    out_root = root / args.out
    jsonl_path = root / args.jsonl
    verbose = not args.quiet

    print(f"仓库：{root}")
    if args.check:
        print("模式：只检查\n")

    n_json = n_jsonl = 0
    if jsonl_path.exists():
        print(f"== {args.jsonl} ==")
        n_jsonl, blocks = fix_jsonl(jsonl_path, args.check, verbose)
        print(f"   改动 {n_jsonl} 行（{blocks} 个文本块）")
    else:
        print(f"[warn] 没有 {args.jsonl}，只处理成品 JSON")

    if out_root.is_dir():
        print(f"\n== {args.out} ==")
        n_json, files = fix_json_files(src_root, out_root, args.check, verbose)
        print(f"   改动 {n_json} 行（{files} 个文件）")
    else:
        print(f"[warn] 没有 {args.out}，只处理翻译记录")

    print(
        f"\n翻译记录 {n_jsonl} 行 / 成品 JSON {n_json} 行"
        + ("（同一批内容，两处都改才算修完；当前未写盘）" if args.check else "（两处均已修复）")
    )


if __name__ == "__main__":
    main()
