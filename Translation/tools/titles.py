#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""titles.py —— 剧情标题表：抽取 / 翻译 / 生成

游戏里的剧情标题不在剧情文件里（文件里只是一行注释），而是随接口
`/scenario/{card,event,main,mini-event}/scenarios/...` 下发，响应还是加密的
（JAES，见 jaes.py）。所以标题要单独翻一份表，油猴脚本解密接口响应后按
story id 换掉 `scenario_title`。

本脚本负责维护这张表，三件小事：

    extract    扫描 Resources/**/story*.json 里 `タイトル「…」` 注释，抽成
               work/titles.tsv（三列：story id、日文、中文），已有的中文不会丢
    translate  调本地 Sakura 把中文列填上（可 Ctrl-C，重跑接着翻）
    build      生成 Translation/Resources/titles_cn.json —— 油猴脚本读的就是它

用法：
    python Translation/tools/titles.py extract
    python Translation/tools/titles.py translate --limit 20      # 先试 20 条
    python Translation/tools/titles.py translate                 # 全量
    python Translation/tools/titles.py build
    python Translation/tools/titles.py stats

TSV 可以直接手改：中文列填好了 build 就会用，留空表示还没翻。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import re
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    import sakura_translate as st
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"[错误] 需要同目录下的 sakura_translate.py：{e}")

# 这些目录不是剧情资源
SKIP_DIRS = {"assets", "native", "internal", "import"}

# 剧情文件里的标题注释，例如：
#   //★好感度Aパート　タイトル「スカートめくりはダメです！」
# 只取「…」里的内容；个别剧本的标题没写收尾的」，
# 所以取到换行为止（剧本 JSON 里换行是转义的 \r\n，得按字面判断）。
TITLE_RE = re.compile(r"タイトル「([^」\r\n]{1,120})")
ESCAPED_NEWLINE = re.compile(r"\\r\\n|\\n")

# 剧本里那一行标题注释：把「」里的内容换成中文，前面的说明保持原样
#   //★好感度Aパート　タイトル「スカートめくりはダメです！」
# 标题本身可能带「」嵌套（例如 会忍不住「哈喵～～」），所以取第一个「到最后一个」。
TITLE_MARK = "タイトル「"


def clean_jp_title(raw: str) -> str:
    return ESCAPED_NEWLINE.split(raw)[0].strip()


def split_title_line(line: str):
    """拆成 (head, title, tail)；tail 是以」开头的收尾部分，没写收尾时为空串。"""
    i = line.find(TITLE_MARK)
    if i < 0:
        return None
    head = line[: i + len(TITLE_MARK)]
    body = line[i + len(TITLE_MARK):]
    j = body.rfind("」")
    if j < 0:
        return head, body, ""
    return head, body[:j], body[j:]


def find_title_line(lines: "list[str]") -> "tuple[int | None, str]":
    """在剧本正文行里找标题注释，返回 (行号, 日文标题)。"""
    for i, ln in enumerate(lines):
        parts = split_title_line(ln)
        if parts:
            jp = clean_jp_title(parts[1]).strip()
            if jp:
                return i, jp
    return None, ""


def put_title(lines: "list[str]", idx: int, zh: str) -> bool:
    """把第 idx 行的标题换成中文（只动「」里的内容）。"""
    parts = split_title_line(lines[idx])
    if not parts:
        return False
    head, _old, tail = parts
    lines[idx] = head + zh + (tail or "」")
    return True


def extract_line_title(line: str) -> str:
    """从一行注释里取标题（兼容标题自带「」）。"""
    parts = split_title_line(line)
    return parts[1].strip() if parts else ""


def find_titles(src_root: pathlib.Path) -> dict[str, str]:
    """扫出 {story id: 日文标题}。空标题（タイトル「」）不要。"""
    found: dict[str, str] = {}
    for path in sorted(src_root.rglob("story*.json")):
        if path.name.endswith("_cn.json") or "_cn." in path.name:
            continue
        parts = set(path.relative_to(src_root).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        m = TITLE_RE.search(text)
        if not m:
            continue
        title = clean_jp_title(m.group(1))
        if title:
            found[path.stem] = title
    return found


def read_tsv(path: pathlib.Path) -> "dict[str, list[str]]":
    rows: dict[str, list[str]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split("\t")
        while len(cells) < 3:
            cells.append("")
        rows[cells[0]] = [cells[1], cells[2]]
    return rows


def write_tsv(path: pathlib.Path, rows: "dict[str, list[str]]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#story_id\t日文\t中文", *[f"{k}\t{v[0]}\t{v[1]}" for k, v in sorted(rows.items())]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def cmd_extract(args, rows_path: pathlib.Path, src_root: pathlib.Path) -> None:
    found = find_titles(src_root)
    if not found:
        raise SystemExit(f"[错误] {src_root} 下没找到标题注释")
    rows = read_tsv(rows_path)          # 保留已经翻好的
    added = 0
    for sid, jp in found.items():
        old = rows.get(sid)
        if old is None:
            rows[sid] = [jp, ""]
            added += 1
        elif old[0] != jp:
            rows[sid] = [jp, ""]        # 原文改了，译文作废
    write_tsv(rows_path, rows)
    done = sum(1 for v in rows.values() if v[1].strip())
    print(f"剧情文件里共 {len(found)} 条标题（新加 {added} 条）")
    print(f"标题表 {rows_path}：{len(rows)} 条，已翻 {done} 条，待翻 {len(rows) - done} 条")


def clean_title(zh: str, jp: str) -> str:
    s = zh.strip()
    s = re.sub(r"^[「『\"'`]+", "", s)
    s = re.sub(r"[」』\"'`]+$", "", s)
    s = s.splitlines()[0].strip() if s else s      # 标题只会占一行
    if jp and not jp.endswith("。") and s.endswith("。"):
        s = s[:-1]
    return s


def translate_chunk(cfg: dict, glossary: str, items: list[tuple[str, str]]) -> list[str]:
    """翻一小批标题；整批行数对不上就逐条重来。"""
    user = st.build_user_prompt(cfg, glossary, "", items)
    outs = None
    for attempt in (1, 2):
        try:
            text = st.call_sakura(cfg, user, strict_lines=len(items) if attempt == 2 else None)
        except Exception:
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) == len(items):
            outs = lines
            break
    if outs is None and len(items) > 1:
        outs = []
        for _, jp in items:
            one = translate_chunk(cfg, glossary, [("", jp)])
            outs.append(one[0] if one else "")
    if outs is None:
        outs = [""] * len(items)
    return [clean_title(z, jp) for z, (_, jp) in zip(outs, items)]


def cmd_translate(args, rows_path: pathlib.Path, cfg: dict, root: pathlib.Path) -> None:
    rows = read_tsv(rows_path)
    if not rows:
        raise SystemExit(f"[错误] {rows_path} 是空的，先跑 extract")
    todo = [(sid, v[0]) for sid, v in sorted(rows.items()) if not v[1].strip()]
    if args.only:
        todo = [t for t in todo if any(s in t[0] for s in args.only)]
    if args.redo:
        todo = [(sid, v[0]) for sid, v in sorted(rows.items()) if any(s in sid for s in args.redo)]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("没有要翻的标题了")
        return

    if not args.no_health:
        try:
            with urllib.request.urlopen(cfg["health_url"], timeout=10) as r:
                r.read()
            print(f"Sakura 服务在线：{cfg['health_url']}（并发 {cfg['workers']}）")
        except Exception as e:
            raise SystemExit(
                f"[错误] 连不上 Sakura（{cfg['health_url']}）：{e}\n"
                f"       请先用 Sakura_Launcher_GUI 启动模型，且并发数要和 -np 对齐。"
            )

    glossary, _speakers = st.load_glossary(cfg, root)
    batch = max(1, int(args.batch))
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    workers = max(1, int(cfg["workers"]))
    print(f"待翻 {len(todo)} 条，{len(chunks)} 批，并发 {workers}\n")

    t0, done_n = time.time(), 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(translate_chunk, cfg, glossary, [("", jp) for _, jp in ch]): ch
                   for ch in chunks}
        try:
            for fut in cf.as_completed(futures):
                ch = futures[fut]
                try:
                    outs = fut.result()
                except Exception as e:
                    outs = [""] * len(ch)
                    print(f"  [fail] {ch[0][0]} 起的一批：{e}")
                for (sid, _jp), zh in zip(ch, outs):
                    if zh:
                        rows[sid][1] = zh
                        done_n += 1
                write_tsv(rows_path, rows)          # 每批落一次盘，随时可以停
                speed = done_n / max(1e-6, time.time() - t0)
                left = len(todo) - done_n
                print(f"  [{done_n}/{len(todo)}] {ch[0][0]} … {outs[0] if outs else ''} "
                      f"（{speed:.1f} 条/秒，剩余约 {left / max(speed, 1e-6) / 60:.1f} 分）")
        except KeyboardInterrupt:
            print("\n已停下（进度已保存，重跑接着翻）")

    done = sum(1 for v in rows.values() if v[1].strip())
    print(f"\n标题表：已翻 {done} 条，待翻 {len(rows) - done} 条")


def cmd_build(args, rows_path: pathlib.Path, out_path: pathlib.Path) -> None:
    rows = load_rows(rows_path, out_path)
    n = write_title_json(out_path, rows)
    print(f"{out_path}：{n} 条（标题表共 {len(rows)} 条，还没翻的没写进去）")


def load_rows(tsv_path: pathlib.Path, json_path: pathlib.Path | None = None) -> "dict[str, list[str]]":
    """标题表底稿（tsv）。json 里有、tsv 里没有的条目也并进来，别丢。"""
    rows = read_tsv(tsv_path)
    if json_path and pathlib.Path(json_path).exists():
        try:
            got = json.loads(pathlib.Path(json_path).read_text(encoding="utf-8"))
        except Exception:
            got = {}
        for sid, zh in got.items():
            zh = str(zh).strip()
            if not zh:
                continue
            if sid not in rows:
                rows[sid] = ["", zh]
            elif not rows[sid][1].strip():
                rows[sid][1] = zh
    return rows


def write_title_json(out_path: pathlib.Path, rows: "dict[str, list[str]]") -> int:
    """写出油猴脚本读的 titles_cn.json，返回条数。"""
    table = {sid: v[1].strip() for sid, v in sorted(rows.items()) if v[1].strip()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8", newline="\n")
    return len(table)


def inject_titles(rows: "dict[str, list[str]]", out_root: pathlib.Path) -> dict:
    """把标题表里的中文标题写进已经翻好的成品剧本（Translation/Resources/**/*_cn.json）。"""
    table = {sid: v[1].strip() for sid, v in rows.items() if v[1].strip()}
    stat = {"done": 0, "no_title_line": 0, "no_translation": 0, "failed": 0}
    for path in sorted(out_root.rglob("*_cn.json")):
        sid = path.name[: -len("_cn.json")]
        zh = table.get(sid, "")
        if not zh:
            stat["no_translation"] += 1
            continue
        try:
            raw, has_bom = st.read_json_text(path)
            data = json.loads(raw)
            text_path = st.find_text_path(data)
            if text_path is None:
                stat["no_title_line"] += 1
                continue
            lines, sep = st.split_lines(st.get_at(data, text_path))
            idx, _jp = find_title_line(lines)
            if idx is None or not put_title(lines, idx, zh):
                stat["no_title_line"] += 1
                continue
            st.set_at(data, text_path, sep.join(lines))
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(st.dump_like(data, st.detect_style(raw), has_bom))
            tmp.replace(path)
            stat["done"] += 1
        except Exception as e:
            stat["failed"] += 1
            print(f"  [fail] {path.name}：{e}")
    return stat


def cmd_inject(args, rows_path: pathlib.Path, out_path: pathlib.Path, out_root: pathlib.Path) -> None:
    if not out_root.is_dir():
        raise SystemExit(f"[错误] 找不到成品目录 {out_root}")
    rows = load_rows(rows_path, out_path)
    stat = inject_titles(rows, out_root)
    print(f"成品剧本标题：替换 {stat['done']} 篇；"
          f"标题表里没有译文 {stat['no_translation']} 篇；"
          f"剧本里没有标题注释 {stat['no_title_line']} 篇；失败 {stat['failed']} 篇")


def cmd_sync(args, rows_path: pathlib.Path, out_path: pathlib.Path,
             out_root: pathlib.Path, src_root: pathlib.Path) -> None:
    """把成品剧本里已经翻好的标题回收进标题表（进程被强杀时丢的那批就靠它补）。"""
    rows = load_rows(rows_path, out_path)
    added = updated = skipped = 0
    added_ids = []
    for p in sorted(out_root.rglob("*_cn.json")):
        sid = p.name[: -len("_cn.json")]
        try:
            data = json.loads(st.read_json_text(p)[0])
            text_path = st.find_text_path(data)
        except Exception:
            continue
        if text_path is None:
            continue
        title = ""
        for ln in st.get_at(data, text_path).splitlines():
            if "タイトル「" in ln:
                title = extract_line_title(ln)
                break
        if not title or re.search(r"[ぁ-んァ-ヶ]", title):
            continue                      # 没标题，或者还是日文

        if sid in rows and rows[sid][1].strip():
            if rows[sid][1].strip() != title:
                if args.overwrite:
                    rows[sid][1] = title
                    updated += 1
                else:
                    skipped += 1
            continue

        jp = ""
        src = src_root / p.relative_to(out_root).parent / (sid + ".json")
        if src.exists():
            try:
                sdata = json.loads(st.read_json_text(src)[0])
                stp = st.find_text_path(sdata)
                if stp is not None:
                    for ln in st.get_at(sdata, stp).splitlines():
                        if "タイトル「" in ln:
                            jp = extract_line_title(ln)
                            break
            except Exception:
                pass
        rows[sid] = [jp, title]
        added_ids.append(sid)
        added += 1

    write_tsv(rows_path, rows)
    n = write_title_json(out_path, rows)
    print(f"回收完成：新增 {added} 条，更新 {updated} 条，跳过 {skipped} 条")
    if added_ids:
        print("  新增：" + ", ".join(added_ids[:12]) + (" …" if len(added_ids) > 12 else ""))
    print(f"{out_path}：{n} 条")


def cmd_import(args, rows_path: pathlib.Path) -> None:
    """把 <story id>\\t<中文> 的小文件并进标题表（外面翻好的稿子、手改的都能用）。"""
    rows = read_tsv(rows_path)
    if not rows:
        raise SystemExit(f"[错误] {rows_path} 是空的，先跑 extract")
    merged, unknown, dup = 0, [], 0
    for path in args.files:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            cells = [c.strip() for c in line.split("\t")]
            if len(cells) < 2 or not cells[1]:
                continue
            sid, zh = cells[0], cells[1]
            if sid not in rows:
                unknown.append(sid)
                continue
            if rows[sid][1].strip() and rows[sid][1].strip() != zh:
                dup += 1
            rows[sid][1] = zh
            merged += 1
    write_tsv(rows_path, rows)
    done = sum(1 for v in rows.values() if v[1].strip())
    print(f"并入 {merged} 条（覆盖已有译文 {dup} 条）")
    if unknown:
        print(f"标题表里没有这些 id，已跳过：{', '.join(unknown[:10])}"
              + (" …" if len(unknown) > 10 else ""))
    print(f"标题表：{len(rows)} 条，已翻 {done} 条，待翻 {len(rows) - done} 条")


def cmd_stats(rows_path: pathlib.Path, out_path: pathlib.Path) -> None:
    rows = read_tsv(rows_path)
    done = sum(1 for v in rows.values() if v[1].strip())
    print(f"{rows_path}：{len(rows)} 条，已翻 {done} 条，待翻 {len(rows) - done} 条")
    if out_path.exists():
        print(f"{out_path}：{len(json.loads(out_path.read_text(encoding='utf-8')))} 条（上次 build 的结果）")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="剧情标题表：抽取 / 翻译 / 生成")
    p.add_argument("cmd", choices=["extract", "translate", "build", "stats", "import", "inject", "sync"])
    p.add_argument("files", nargs="*", help="import 用：<story id>\\t<中文> 的小文件，可多个")
    p.add_argument("--root", help="仓库根目录，默认自动定位")
    p.add_argument("--config", help="配置文件，默认 Translation/config.json")
    p.add_argument("--src", default="Resources", help="原文根目录")
    p.add_argument("--tsv", default="Translation/work/titles.tsv", help="标题表（日文 + 中文）")
    p.add_argument("--out", default="Translation/Resources/titles_cn.json", help="给油猴脚本读的标题表")
    p.add_argument("--out-root", default="Translation/Resources", help="成品剧本根目录（inject 用）")
    p.add_argument("--src-root", default="Resources", help="原文根目录（sync 时用来补日文标题）")
    p.add_argument("--overwrite", action="store_true", help="sync 时用成品里的标题覆盖表里已有的")
    p.add_argument("--limit", type=int, help="最多翻多少条")
    p.add_argument("--batch", type=int, default=10, help="每次请求塞几条标题（默认 10）")
    p.add_argument("--workers", type=int, help="并发数，覆盖配置（要和 -np 对齐）")
    p.add_argument("--only", action="append", default=None, help="只翻 id 含该字符串的，可重复")
    p.add_argument("--redo", action="append", default=None, help="重翻 id 含该字符串的，可重复")
    p.add_argument("--no-health", action="store_true", help="跳过服务在线检查")
    args = p.parse_args()

    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    src_root = (root / args.src).resolve()
    rows_path = (root / args.tsv).resolve()
    out_path = (root / args.out).resolve()

    if args.cmd == "extract":
        if not src_root.is_dir():
            raise SystemExit(f"[错误] 找不到原文目录 {src_root}")
        cmd_extract(args, rows_path, src_root)
        return
    if args.cmd == "build":
        cmd_build(args, rows_path, out_path)
        return
    if args.cmd == "stats":
        cmd_stats(rows_path, out_path)
        return
    if args.cmd == "import":
        if not args.files:
            raise SystemExit("[错误] import 后面要给文件，例：titles.py import work/titles_cn_01.tsv")
        cmd_import(args, rows_path)
        return
    if args.cmd == "inject":
        cmd_inject(args, rows_path, out_path, (root / args.out_root).resolve())
        return
    if args.cmd == "sync":
        cmd_sync(args, rows_path, out_path,
                 (root / args.out_root).resolve(), (root / args.src_root).resolve())
        return

    cfg = st.load_config(root, args.config)
    if args.workers:
        cfg["workers"] = args.workers
    cmd_translate(args, rows_path, cfg, root)


if __name__ == "__main__":
    main()
