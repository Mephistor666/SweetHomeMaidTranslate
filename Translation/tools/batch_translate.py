#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_translate.py —— 批量翻译 Resources 下还没翻的剧情

和 sakura_translate.py 的分工：
  · sakura_translate.py 走的是老流程（uuid 资源 + extract/translate/inject）
  · 本脚本直接对着按剧情 id 整理的 Resources/<目录>/<story id>.json 干活，
    翻完写成 Translation/Resources/<目录>/<story id>_cn.json（油猴脚本认这个命名）

特点：
  · 自动找出还没翻译的资源：成品文件不存在就算没翻
  · 一篇一个成品文件，翻完一篇落一篇 —— 随时 Ctrl-C 停，重跑自动接着翻
  · 篇内并发（workers），要和 Sakura 服务的「并行工作线程数 -np」对齐
  · 每篇结果记到 work/batch_status.jsonl，方便事后查哪篇失败、要重翻

用法：
    python Translation/tools/batch_translate.py --list                # 只看看还有多少没翻
    python Translation/tools/batch_translate.py --limit 5             # 先试 5 篇
    python Translation/tools/batch_translate.py                       # 全量（可随时 Ctrl-C）
    python Translation/tools/batch_translate.py --only Card           # 只翻 Card/
    python Translation/tools/batch_translate.py --redo story1110501   # 重翻某篇
    python Translation/tools/batch_translate.py --retry-failed        # 只重翻上次失败的
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
    import titles as tt
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"[错误] 需要同目录下的 sakura_translate.py / titles.py：{e}")

# 剧本开头的标题注释（タイトル「…」）：翻正文时顺手翻掉，并写进这两张表
TITLES_TSV = "Translation/work/titles.tsv"
TITLES_JSON = "Translation/Resources/titles_cn.json"
TITLES_FLUSH_EVERY = 1           # 每翻出一条新标题就落盘（被强杀也不会丢标题）

# 这些目录不是剧情
SKIP_DIRS = {"assets", "native", "internal", "import"}

# 和油猴脚本里的 DIR_RULES 保持一致：剧本 id -> 成品可以放哪些目录。
# 放错了脚本就找不到，所以这里会顺手校验（story2 开头 Main/Card 都有，两个都行）。
DIR_RULES = [
    (r"^storyEV", ["Event"]),
    (r"^storyBT", ["Battle"]),
    (r"^story0", ["Main"]),
    (r"^story1", ["Card"]),
    (r"^story2", ["Main", "Card"]),
    (r"^(default_|tutorial)", ["Ordinary"]),
    (r"^MainGarden\d+Config$", ["Garden"]),      # 主页互动气泡，不是剧情
]


def dirs_for(story_id: str) -> list[str] | None:
    for pat, dirs in DIR_RULES:
        if re.match(pat, story_id):
            return dirs
    return None


def find_stories(src_root: pathlib.Path) -> list[dict]:
    """按剧情 id 整理过的原文：Resources/Card/storyXXXX.json 这种。"""
    out, skipped = [], 0
    for p in sorted(src_root.rglob("*.json")):
        rel = p.relative_to(src_root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if p.name.endswith("_cn.json"):
            continue
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                name = str(json.load(fh)[5][0][1])
        except Exception:
            skipped += 1
            continue
        out.append({"id": name, "dir": rel.parts[0] if len(rel.parts) > 1 else "", "path": p})
    return out


def has_untranslated(out_path: pathlib.Path, cmds: list[str]) -> bool:
    """成品里还有没有没翻的正文（对话块里出现假名就算）。"""
    try:
        node = json.loads(out_path.read_text(encoding="utf-8-sig"))[5][0][2]
    except Exception:
        return False
    if not isinstance(node, str):
        return False
    lines, _ = st.split_lines(node)
    for b in st.scan_blocks(lines, cmds):
        for r in b["region"]:
            if r["kind"] == "text" and st.looks_untranslated(r["text"]):
                return True
    return False


def has_speaker(src_path: pathlib.Path, names: list[str], cmds: list[str]) -> bool:
    """原文件里有没有这些说话人的台词（只看对话指令，站着不动的不算）。"""
    try:
        node = json.loads(src_path.read_text(encoding="utf-8-sig"))[5][0][2]
    except Exception:
        return False
    if not isinstance(node, str):
        return False
    return any(f"{c},{name}" in node for c in cmds for name in names)


def has_dialogue(src_path: pathlib.Path, cmds: list[str]) -> bool:
    """原文件里有没有可以翻的正文（现场扫，不信旧记录）。"""
    try:
        node = json.loads(src_path.read_text(encoding="utf-8-sig"))[5][0][2]
    except Exception:
        return False
    if not isinstance(node, str):
        return False
    lines, _ = st.split_lines(node)
    return any(r["kind"] == "text" for b in st.scan_blocks(lines, cmds) for r in b["region"])


def load_status(path: pathlib.Path) -> dict:
    """读上次跑的结果：{story id: 最后一条记录}"""
    got = {}
    if not path.exists():
        return got
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("story"):
                got[rec["story"]] = rec
    return got


def translate_title(cfg: dict, glossary: str, jp: str) -> str:
    """翻一条标题。失败就返回空串 —— 标题翻不出来不能影响正文。"""
    try:
        outs = tt.translate_chunk(cfg, glossary, [("", jp)])
        return (outs[0] if outs else "").strip()
    except Exception as e:
        st.log(f"[fail] 标题「{jp}」-> {e}")
        return ""


def translate_story(cfg: dict, s: dict, glossary: str, names: set[str], workers: int,
                    titles: dict | None = None) -> dict:
    """翻一篇，返回 {ok, failed, blocks, lines, title}。"""
    raw, has_bom = st.read_json_text(s["path"])
    data = json.loads(raw)
    text_path = st.find_text_path(data)
    if text_path is None:
        return {"ok": 0, "failed": 0, "blocks": 0, "lines": 0, "note": "定位不到正文"}
    lines, sep = st.split_lines(st.get_at(data, text_path))

    # 开头的标题：表里有就直接用，没有就现翻一条，顺手写回标题表
    title_idx, title_jp = tt.find_title_line(lines)
    new_title = None
    if title_idx is not None and titles is not None:
        zh = str(titles.get(s["id"], ["", ""])[1]).strip()
        if not zh:
            zh = translate_title(cfg, glossary, title_jp)
            if zh:
                new_title = [s["id"], title_jp, zh]
        if zh:
            tt.put_title(lines, title_idx, zh)

    blocks = st.scan_blocks(lines, cfg["text_commands"])
    if not blocks:
        return {"ok": 0, "failed": 0, "blocks": 0, "lines": 0, "note": "没有对白",
                "title": new_title}
    orig_count = {"cmd": 0, "param": 0, "cmt": 0}
    for ln in lines:
        t = ln.strip()
        if t.startswith("@"):
            orig_count["cmd"] += 1
        elif t.startswith("$$"):
            orig_count["param"] += 1
        elif t.startswith("//"):
            orig_count["cmt"] += 1

    records = []
    for n, b in enumerate(blocks):
        src = [r["text"] for r in b["region"] if r["kind"] == "text"]
        if not src:
            continue          # 只有 $$ 语音行（有语音没字幕），不用翻
        records.append({
            "id": f"{s['id']}#{b['cmd_line']}",
            "file": s["path"].name,
            "asset": s["id"],
            "block": n,
            "cmd_line": b["cmd_line"],
            "speaker": b["speaker"],
            "region": b["region"],
            "src": src,
        })

    results, ok, failed = {}, 0, 0
    batches = st.make_batches(records, cfg)
    history = ""
    with cf.ThreadPoolExecutor(max(1, workers)) as pool:
        for start in range(0, len(batches), max(1, workers)):
            wave = batches[start:start + max(1, workers)]
            outs = list(pool.map(lambda b: st.translate_batch(cfg, b, glossary, names, history), wave))
            for batch, res in zip(wave, outs):
                tail = ""
                for r in batch:
                    got = res["results"].get(r["id"])
                    if got:
                        texts, spans = got
                        results[r["id"]] = (texts, spans)
                        ok += 1
                        tail = "\n".join(texts)
                    else:
                        failed += 1
                        st.log(f"[fail] {r['id']} {r['src'][:1]} -> {res['error'][:120]}")
                if tail:
                    history = tail[-int(cfg["history_chars"]):]

    # 注意：要从后往前写。合并会让行数变少，从前往后写会让后面所有块的行号错位。
    for r in sorted(records, key=lambda x: x["cmd_line"], reverse=True):
        got = results.get(r["id"])
        if got:
            texts, spans = got
            st.apply_translation(lines, {**r, "zh": texts, "zh_span": spans})

    st.set_at(data, text_path, sep.join(lines))

    # 结构自检：指令行 / $$ 参数行 / 注释行的数量必须和原文一致，否则宁可不写
    now_count = {"cmd": 0, "param": 0, "cmt": 0}
    for ln in lines:
        t = ln.strip()
        if t.startswith("@"):
            now_count["cmd"] += 1
        elif t.startswith("$$"):
            now_count["param"] += 1
        elif t.startswith("//"):
            now_count["cmt"] += 1
    if now_count != orig_count:
        return {"ok": 0, "failed": failed, "blocks": len(blocks), "lines": 0,
                "note": "结构自检没过（指令行数变了），没有写文件"}
    style = st.detect_style(raw)
    tmp = s["out"].with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(st.dump_like(data, style, has_bom))
    tmp.replace(s["out"])                  # 原子落盘：崩了也不会留半成品
    return {"ok": ok, "failed": failed, "blocks": len(blocks),
            "lines": sum(len(r["src"]) for r in records), "note": "", "title": new_title}


def fmt_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return (f"{h} 小时 " if h else "") + (f"{m} 分 " if h or m else "") + f"{s} 秒"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="批量翻译还没翻的剧情资源")
    p.add_argument("--root", help="仓库根目录，默认自动定位")
    p.add_argument("--config", help="配置文件，默认 Translation/config.json")
    p.add_argument("--src", default="Resources", help="原文根目录")
    p.add_argument("--out", default="Translation/Resources", help="成品根目录")
    p.add_argument("--status", default="Translation/work/batch_status.jsonl", help="进度记录文件")
    p.add_argument("--only", action="append", default=None, help="只翻路径含该字符串的，可重复")
    p.add_argument("--redo", action="append", default=None, help="强制重翻含该字符串的，可重复")
    p.add_argument("--retry-failed", action="store_true", help="只重翻上次有失败块的")
    p.add_argument("--redo-untranslated", action="store_true",
                   help="把成品里还留着日文的篇重新翻（比如刚给白名单补了新指令）")
    p.add_argument("--redo-speaker", action="append", default=None,
                   help="重翻某个说话人出场的篇，可重复（例如 --redo-speaker スカーレット）")
    p.add_argument("--limit", type=int, help="最多翻多少篇")
    p.add_argument("--workers", type=int, help="并发数，覆盖配置（要和 -np 对齐）")
    p.add_argument("--list", action="store_true", help="只列出待翻译，不翻译")
    p.add_argument("--fill-titles", action="store_true",
                   help="不翻译，只把标题表里的中文标题补进已翻好的成品剧本")
    p.add_argument("--titles", default=TITLES_TSV, help="标题表底稿（work/titles.tsv）")
    p.add_argument("--titles-json", default=TITLES_JSON, help="油猴脚本读的标题表")
    p.add_argument("--no-health", action="store_true", help="跳过服务在线检查")
    args = p.parse_args()

    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    src_root = (root / args.src).resolve()
    out_root = (root / args.out).resolve()
    status_path = (root / args.status).resolve()
    cfg = st.load_config(root, args.config)
    if args.workers:
        cfg["workers"] = args.workers
    if not src_root.is_dir():
        raise SystemExit(f"[错误] 找不到原文目录 {src_root}")

    titles_path = (root / args.titles).resolve()
    titles_json = (root / args.titles_json).resolve()

    if args.fill_titles:                    # 只补标题，不翻译
        if not out_root.is_dir():
            raise SystemExit(f"[错误] 找不到成品目录 {out_root}")
        rows = tt.load_rows(titles_path, titles_json)
        stat = tt.inject_titles(rows, out_root)
        print(f"成品剧本标题：替换 {stat['done']} 篇；"
              f"标题表里没有译文 {stat['no_translation']} 篇；"
              f"剧本里没有标题注释 {stat['no_title_line']} 篇；失败 {stat['failed']} 篇")
        return

    stories = find_stories(src_root)
    if not stories:
        raise SystemExit(f"[错误] {src_root} 下没找到剧本（要看剧情 id，读到 _name 才算）")

    status = load_status(status_path)
    done, pending, misplaced, stale = 0, [], [], 0
    for s in stories:
        s["out"] = out_root / s["dir"] / (s["id"] + "_cn.json")
        want = dirs_for(s["id"])
        if want and s["dir"] and s["dir"] not in want:
            misplaced.append((s["id"], s["dir"], want[0]))
        forced = bool(args.redo) and any(r.lower() in s["id"].lower() or r.lower() in str(s["path"]).lower()
                                        for r in args.redo)
        if args.redo_speaker and not forced and s["out"].exists() \
                and has_speaker(s["path"], args.redo_speaker, cfg["text_commands"]):
            forced = True
        if args.retry_failed and not forced:
            rec = status.get(s["id"])
            if not rec or not rec.get("failed"):
                done += 1
                continue
        elif not forced:
            if s["out"].exists():
                dirty = has_untranslated(s["out"], cfg["text_commands"])
                if dirty and not args.redo_untranslated:
                    stale += 1
                if not (args.redo_untranslated and dirty):
                    done += 1
                    continue
            # 没成品的，现场扫一眼原文：确实没有正文的（纯演出文件）不算待翻；
            # 不靠进度记录里的 skipped —— 早期版本可能因为白名单缺指令而误记。
            if not has_dialogue(s["path"], cfg["text_commands"]):
                done += 1
                continue
        if args.only and not any(o.lower() in str(s["path"]).lower() or o.lower() in s["id"].lower()
                                 for o in args.only):
            continue
        pending.append(s)
    if args.limit:
        pending = pending[:args.limit]

    by_dir: dict[str, int] = {}
    for s in pending:
        by_dir[s["dir"]] = by_dir.get(s["dir"], 0) + 1

    print(f"原文 {len(stories)} 篇，已完成 {done} 篇，待翻译 {len(pending)} 篇")
    if by_dir:
        print("  待翻译分布：" + "  ".join(f"{k or '(根)'} {v}" for k, v in sorted(by_dir.items())))
    if misplaced:
        print(f"  [警告] {len(misplaced)} 篇的目录和油猴脚本的规律不一致（脚本可能找不到）：")
        for sid, got, want in misplaced[:5]:
            print(f"          {sid} 在 {got}/，按规律应在 {want}/")
    if stale:
        print(f"  [提示] 另有 {stale} 篇成品里还有日文（新补的指令或排版没翻到）。"
              f"要补翻就在命令后面加 --redo-untranslated")

    if args.list:
        for s in pending[:40]:
            print(f"    {s['dir']}/{s['id']}")
        if len(pending) > 40:
            print(f"    … 其余 {len(pending) - 40} 篇")
        list_path = status_path.parent / "pending_list.txt"
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text("\n".join(f"{s['dir']}/{s['id']}" for s in pending) + "\n",
                             encoding="utf-8", newline="\n")
        print(f"\n完整清单（{len(pending)} 条）已写到 {list_path}")
        print("\n（--list 只看看，不翻译）")
        return
    if not pending:
        print("没有需要翻译的了")
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

    glossary, speakers = st.load_glossary(cfg, root)
    cfg["speakers_map"] = speakers
    names = set(speakers) | set(speakers.values())
    print(f"术语表 {len(glossary.splitlines())} 条，说话人 {len(speakers)} 个\n")

    # 标题表：翻正文时顺手翻开头的标题，新翻出来的攒够一批就落盘
    titles = tt.load_rows(titles_path, titles_json)
    titles_dirty = 0
    print(f"标题表 {len(titles)} 条（已翻 {sum(1 for v in titles.values() if v[1].strip())} 条）\n")

    def flush_titles(force: bool = False) -> None:
        nonlocal titles_dirty
        if not titles_dirty and not force:
            return
        tt.write_tsv(titles_path, titles)
        n = tt.write_title_json(titles_json, titles)
        titles_dirty = 0
        print(f"  标题表已更新：{titles_json}（{n} 条）")

    status_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    done_n = fail_n = skip_n = blocks_n = 0
    stopped = False
    try:
        with status_path.open("a", encoding="utf-8", newline="\n") as logf:
            for i, s in enumerate(pending, 1):
                t1 = time.time()
                try:
                    res = translate_story(cfg, s, glossary, names, int(cfg["workers"]), titles)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    res = {"ok": 0, "failed": 0, "blocks": 0, "lines": 0, "note": f"异常：{e}"}
                dt = time.time() - t1
                status_name = "ok" if res["blocks"] and not res["failed"] else (
                    "skipped" if not res["blocks"] else "partial")
                if not res["blocks"]:
                    skip_n += 1
                elif res["failed"]:
                    fail_n += 1
                else:
                    done_n += 1
                blocks_n += res["ok"]
                if res.get("title"):
                    sid, jp, zh = res["title"]
                    titles[sid] = [jp, zh]
                    titles_dirty += 1
                    if titles_dirty >= TITLES_FLUSH_EVERY:
                        flush_titles()
                logf.write(json.dumps({
                    "story": s["id"], "dir": s["dir"], "status": status_name,
                    "blocks": res["blocks"], "ok": res["ok"], "failed": res["failed"],
                    "seconds": round(dt, 1), "note": res["note"], "at": int(time.time()),
                }, ensure_ascii=False) + "\n")
                logf.flush()

                spent = time.time() - t0
                eta = spent / i * (len(pending) - i)
                flag = {"ok": "完成", "partial": f"部分失败 {res['failed']} 块",
                        "skipped": res["note"] or "跳过"}[status_name]
                print(f"[{i}/{len(pending)}] {s['dir']}/{s['id']}  {flag}"
                      f"（{res['ok']} 块 / {dt:.0f}s）  剩余约 {fmt_time(eta)}"
                      + (f"  标题「{res['title'][2]}」" if res.get("title") else ""))
    except KeyboardInterrupt:
        stopped = True
        print("\n已手动停止。已翻好的都已经落盘，重跑同一条命令就会接着翻。")
    finally:
        flush_titles()

    print(f"\n本次：完成 {done_n} 篇，部分失败 {fail_n} 篇，跳过 {skip_n} 篇，"
          f"共 {blocks_n} 块，用时 {fmt_time(time.time() - t0)}"
          + ("（中途停止）" if stopped else ""))
    if fail_n:
        print("  有失败块的用 --retry-failed 重翻，或 --redo <关键字> 指定")
    print(f"  进度记录：{status_path}")


if __name__ == "__main__":
    main()
