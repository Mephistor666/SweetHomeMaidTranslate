#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bundle_map.py —— 读懂 Cocos 的 config.<hash>.json，在「逻辑路径 / uuid / 磁盘文件」之间互查

游戏用的是 Cocos Creator 的资源包（bundle）。每个包目录下有一份 config.<hash>.json，
它才是资源索引，字段含义：

    name            包名（AdvStory / GardenMain001 / main …）
    uuids[i]        第 i 个资源的压缩 uuid（22 字符，可解压成 36 字符标准 uuid）
    paths[str(i)]   第 i 个资源的 [逻辑路径, ?, 类型索引]
    types[t]        类型索引对应的类型，剧情都是 cc.TextAsset
    versions.import [索引, 哈希, 索引, 哈希, …] 磁盘文件名里的那串哈希

磁盘文件位置：
    <包目录>/import/<uuid 前两位>/<uuid>.<版本哈希>.json

而「逻辑路径」就是游戏代码里用的那个名字，例如 Card/story1110501 —— 别人整理好的
Card/、Event/、Main/ 目录用的正是这套路径。

用法：
    python Translation/tools/bundle_map.py info
    python Translation/tools/bundle_map.py map --path Card/story1110501
    python Translation/tools/bundle_map.py map --uuid ccc01625-2ee8-4741-85fc-7e8c38d298d7
    python Translation/tools/bundle_map.py check
    python Translation/tools/bundle_map.py export --out 输出目录 --dir Card        # 默认只预览
    python Translation/tools/bundle_map.py manifest -o Translation/work/advstory_manifest.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import shutil
import sys

BASE64_KEYS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_VALUES = [-1] * 123
for _i, _c in enumerate(BASE64_KEYS):
    _VALUES[ord(_c)] = _i
_HEX = "0123456789abcdef"
_T4 = [""] * 4
_TEMPLATE = _T4 + _T4 + ["-"] + _T4 + ["-"] + _T4 + ["-"] + _T4 + ["-"] + _T4 + _T4 + _T4
_SLOTS = [i for i, x in enumerate(_TEMPLATE) if x != "-"]


def decode_uuid(compressed: str) -> str:
    """Cocos 的压缩 uuid（22 字符）解压成标准 36 字符 uuid。"""
    main = compressed.split("@")[0]
    if len(main) != 22:
        return main
    out = list(_TEMPLATE)
    out[0], out[1] = main[0], main[1]
    j = 2
    for i in range(2, 22, 2):
        lhs, rhs = _VALUES[ord(main[i])], _VALUES[ord(main[i + 1])]
        out[_SLOTS[j]] = _HEX[lhs >> 2]
        j += 1
        out[_SLOTS[j]] = _HEX[((lhs & 3) << 2) | (rhs >> 4)]
        j += 1
        out[_SLOTS[j]] = _HEX[rhs & 0xF]
        j += 1
    return "".join(out)


class Asset:
    __slots__ = ("bundle", "index", "path", "uuid", "hash", "rel", "kind")

    def __init__(self, bundle, index, path, uuid, hash_, rel, kind="?"):
        self.bundle, self.index, self.path, self.uuid, self.hash, self.rel, self.kind = (
            bundle, index, path, uuid, hash_, rel, kind,
        )

    def __repr__(self):
        return f"<{self.bundle} {self.path} {self.uuid}.{self.hash}>"


class Bundle:
    def __init__(self, cfg_path: pathlib.Path):
        self.cfg_path = cfg_path
        self.root = cfg_path.parent
        d = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        self.name = d.get("name") or self.root.name
        self.types = d.get("types", [])
        self.import_base = d.get("importBase", "import")
        uuids = d.get("uuids", [])
        paths = d.get("paths", {})
        ver = d.get("versions", {}).get("import", [])
        self.version = dict(zip(ver[0::2], ver[1::2]))
        self.assets: list[Asset] = []
        self.by_path: dict[str, Asset] = {}
        self.by_uuid: dict[str, Asset] = {}
        for key, val in paths.items():
            i = int(key)
            logical = val[0]
            full = decode_uuid(uuids[i]) if i < len(uuids) else ""
            h = self.version.get(i, "")
            rel = f"{self.import_base}/{full[:2]}/{full}.{h}.json"
            a = Asset(self.name, i, logical, full, h, rel, self.type_of(val[2] if len(val) > 2 else 0))
            self.assets.append(a)
            self.by_path[logical] = a
            self.by_uuid[full] = a

    def type_of(self, index: int) -> str:
        # paths[i][2] 是 1 基的类型下标
        t = self.types[index - 1] if 0 < index <= len(self.types) else None
        return t or "?"


def find_configs(root: pathlib.Path) -> list[pathlib.Path]:
    """挑出真正的 bundle config：顶层同时有 uuids / paths / versions。"""
    found = []
    for p in sorted(root.rglob("config*.json")):
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(d, dict) and {"uuids", "paths", "versions"} <= set(d):
            found.append(p)
    return found


def _config_rank(p: pathlib.Path) -> tuple:
    """同一个包有多份 config 时怎么排序：旁边有 import/ 的最优先（游戏真正加载的），
    然后资源多的优先（游戏更新后新下的那份通常更多），最后路径浅的优先。"""
    real = (p.parent / "import").is_dir()
    try:
        n = len(json.loads(p.read_text(encoding="utf-8-sig")).get("paths", {}))
    except Exception:
        n = 0
    return (real, n, -len(p.parts))


def load_bundles(root: pathlib.Path, only: str | None = None) -> list[Bundle]:
    """同一个包名可能有多份副本（下载到 Resources/ 的 + 包目录里的，或者游戏更新后
    新旧两份都还在），只留一份，并提示忽略了哪些。"""
    groups: dict[str, list[pathlib.Path]] = {}
    for p in find_configs(root):
        if only and p.name != pathlib.Path(only).name:
            continue
        try:
            name = json.loads(p.read_text(encoding="utf-8-sig")).get("name") or p.parent.name
        except Exception:
            name = p.parent.name
        groups.setdefault(name, []).append(p)
    out = []
    for name, paths in sorted(groups.items()):
        paths.sort(key=_config_rank, reverse=True)
        out.append(Bundle(paths[0]))
        for p in paths[1:]:
            print(f"[注意] {name} 有 {len(paths)} 份 config，用 {paths[0].name}，"
                  f"忽略 {p.name}（{_config_rank(p)[1]} 个资源）")
    return out


def pick(bundles: list[Bundle], name: str | None) -> list[Bundle]:
    if not name:
        return bundles
    hit = [b for b in bundles if b.name.lower() == name.lower() or b.root.name.lower() == name.lower()]
    if not hit:
        raise SystemExit(f"[错误] 没有名为 {name} 的包；现有：" + ", ".join(b.name for b in bundles))
    return hit


def cmd_info(args, bundles, root) -> None:
    print(f"仓库：{root}\n")
    for b in bundles:
        dist = collections.Counter(a.path.split("/")[0] for a in b.assets)
        print(f"{b.name}  ({b.cfg_path.relative_to(root).as_posix()})")
        print(f"   资源 {len(b.assets)} 个 | 类型 {b.types[:3]}{'…' if len(b.types) > 3 else ''} | 磁盘目录 {b.root.relative_to(root).as_posix()}/{b.import_base}/")
        print("   逻辑路径分布：" + "  ".join(f"{k} {v}" for k, v in dist.most_common()))
        print()


def cmd_map(args, bundles, root) -> None:
    if not (args.path or args.uuid or args.file):
        raise SystemExit("[错误] 需要 --path / --uuid / --file 之一")
    for b in bundles:
        a = None
        if args.path:
            a = b.by_path.get(args.path)
        elif args.uuid:
            a = b.by_uuid.get(decode_uuid(args.uuid) if len(args.uuid) != 36 else args.uuid)
            if a is None:
                a = next((x for x in b.assets if x.uuid.startswith(args.uuid.lower())), None)
        elif args.file:
            stem = pathlib.Path(args.file).name.split(".")[0]
            a = b.by_uuid.get(stem)
        if not a:
            continue
        disk = b.root / a.rel
        print(f"包           {a.bundle}")
        print(f"逻辑路径     {a.path}")
        print(f"索引         {a.index}")
        print(f"uuid         {a.uuid}")
        print(f"版本哈希     {a.hash}")
        print(f"资源类型     {a.kind}")
        print(f"磁盘文件     {disk.relative_to(root).as_posix()}  [{'存在' if disk.exists() else '不存在'}]")
        return
    raise SystemExit("[错误] 在已知的包里找不到这个资源")


def cmd_check(args, bundles, root) -> None:
    total = missing = 0
    on_disk: dict[str, set[str]] = {}
    for b in bundles:
        imp = b.root / b.import_base
        on_disk[b.name] = {f.name.split(".")[0] for f in imp.rglob("*.json")} if imp.is_dir() else set()
    for b in bundles:
        miss = [a for a in b.assets if not (b.root / a.rel).exists()]
        extra = on_disk[b.name] - {a.uuid for a in b.assets}
        total += len(b.assets)
        missing += len(miss)
        print(f"{b.name}: 资源 {len(b.assets)} | 磁盘上有 {len(on_disk[b.name])} 个文件 | 缺文件 {len(miss)} | 多出来的文件 {len(extra)}")
        for a in miss[:5]:
            print(f"   缺 {a.path}  ({a.uuid}.{a.hash})")
        for u in list(extra)[:5]:
            print(f"   多 {u}（config 里没有，可能是旧版本残留）")
    print(f"\n合计 {total} 个资源，缺 {missing} 个文件")


def cmd_export(args, bundles, root) -> None:
    out_root = pathlib.Path(args.out)
    if not out_root.is_absolute():
        out_root = root / out_root
    only = set(args.dir) if args.dir else None
    todo, skipped = [], 0
    for b in bundles:
        for a in b.assets:
            if only and a.path.split("/")[0] not in only:
                continue
            src = b.root / a.rel
            if not src.exists():
                skipped += 1
                continue
            todo.append((src, out_root / (a.path + ".json")))
    print(f"待导出 {len(todo)} 个，跳过（磁盘上没有）{skipped} 个")
    if not args.write:
        for s, d in todo[:5]:
            print(f"   {s.relative_to(root).as_posix()}  ->  {d}")
        print("（预览模式，加 --write 才会真的写）")
        return
    done = 0
    for s, d in todo:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        done += 1
    print(f"已导出 {done} 个到 {out_root}")


def cmd_missing(args, bundles, root) -> None:
    """拿 config 和在手的资源对比，列出缺哪些（并给出可下载的包内路径）。"""
    have_root = pathlib.Path(args.have)
    if not have_root.is_absolute():
        have_root = root / have_root
    if not have_root.is_dir():
        raise SystemExit(f"[错误] 找不到手上的资源目录 {have_root}")
    base = args.game_base.rstrip("/") + "/"

    present, scanned = set(), 0
    for p in sorted(have_root.rglob("*.json")):
        if p.name.endswith("_cn.json"):
            continue
        rel = p.relative_to(have_root)
        if len(rel.parts) < 2:                                   # 根目录下的文件（比如 config.json）
            continue
        if rel.parts[0] in ("assets", "Garden"):                 # 不是剧情目录
            continue
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                present.add(str(json.load(fh)[5][0][1]))
        except Exception:
            present.add(p.stem)
        scanned += 1

    rows = []
    for b in bundles:
        for a in b.assets:
            _, _, name = a.path.partition("/")
            if name in present:
                continue
            pkg = f"assets/{b.name}/{a.rel}"      # 相对游戏 r/<版本>/ 的路径
            rows.append((a.path, a.uuid, a.hash, pkg, base + pkg))

    total = sum(len(b.assets) for b in bundles)
    by_dir = collections.Counter(r[0].split("/")[0] for r in rows)
    print(f"config 共 {total} 个资源；手上扫到 {scanned} 个文件（去重 {len(present)} 个 id）；缺 {len(rows)} 个")
    for d in sorted(by_dir):
        print(f"   {d:10s} 缺 {by_dir[d]:4d}")

    if args.out:
        out = pathlib.Path(args.out)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write("# 逻辑路径\tuuid\t版本哈希\t包内路径\t下载地址\n")
            for r in rows:
                fh.write("\t".join(r) + "\n")
        print(f"\n已写出 {out}（{len(rows)} 行）")

    for r in rows[:args.show]:
        print("   " + r[0])
    if len(rows) > args.show:
        print(f"   … 其余 {len(rows) - args.show} 个")


def cmd_manifest(args, bundles, root) -> None:
    data = {}
    for b in bundles:
        data[b.name] = {
            a.path: {"uuid": a.uuid, "hash": a.hash, "file": a.rel, "index": a.index}
            for a in b.assets
        }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        p = pathlib.Path(args.output)
        if not p.is_absolute():
            p = root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        print(f"已写出 {p}（{sum(len(v) for v in data.values())} 条）")
    else:
        print(text[:2000])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Cocos bundle config 与资源文件的映射工具")
    parser.add_argument("--root", help="仓库根目录，默认当前目录向上找")
    parser.add_argument("--bundle", help="只处理指定包（AdvStory / main / …）")
    parser.add_argument("--config", help="只用这一份 config 文件（默认同名的包自动挑最新那份）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="列出所有包的资源分布")

    p = sub.add_parser("map", help="互查 逻辑路径 / uuid / 磁盘文件")
    p.add_argument("--path", help="逻辑路径，如 Card/story1110501")
    p.add_argument("--uuid", help="uuid（压缩或标准都行）")
    p.add_argument("--file", help="磁盘文件名")

    sub.add_parser("check", help="校验磁盘文件与 config 是否一致")

    p = sub.add_parser("export", help="按逻辑路径导出成 Card/Event/Main 形式")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--dir", action="append", help="只导出这些顶层目录，可重复；默认全部")
    p.add_argument("--write", action="store_true", help="真的写盘（默认为预览）")

    p = sub.add_parser("manifest", help="输出 逻辑路径 -> uuid 的清单 json")
    p.add_argument("-o", "--output", help="输出文件，不填则打印")

    p = sub.add_parser("missing", help="对比 config 和手上的资源，列出缺少的资源")
    p.add_argument("--have", default="Resources", help="手上的资源目录，默认 Resources")
    p.add_argument("--out", default="Translation/work/missing_advstory.tsv", help="清单输出路径")
    p.add_argument("--game-base", default="https://game.sweet-home-maid.com/r/7LCHDxB8msHV/",
                   help="游戏资源地址前缀，用来拼下载地址")
    p.add_argument("--show", type=int, default=15, help="打印多少条")

    args = parser.parse_args()
    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    bundles = pick(load_bundles(root, args.config), args.bundle)
    if not bundles:
        raise SystemExit(f"[错误] 在 {root} 下没找到 Cocos 的 config.<hash>.json")
    {"info": cmd_info, "map": cmd_map, "check": cmd_check, "export": cmd_export,
     "manifest": cmd_manifest, "missing": cmd_missing}[
        args.cmd
    ](args, bundles, root)


if __name__ == "__main__":
    main()
