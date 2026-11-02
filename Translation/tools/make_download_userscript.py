#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_download_userscript.py —— 把缺失清单变成一次性下载用的油猴脚本

游戏更新后，先对比新 config 和手上的资源，拿到缺哪些：

    python Translation/tools/bundle_map.py --bundle AdvStory missing

它会写出 Translation/work/missing_advstory.tsv（列：逻辑路径 / uuid / 版本哈希 /
包内路径 / 下载地址）。本脚本把这份清单塞进 download_missing.user.js：
在浏览器里打开游戏，点一下菜单就把缺的资源抓下来打成 zip。

用法：
    python Translation/tools/make_download_userscript.py
    python Translation/tools/make_download_userscript.py --version 2026.09.28
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date


def build(tsv_path: pathlib.Path, out_path: pathlib.Path, version: str) -> tuple[int, int]:
    items = []
    for line in tsv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        items.append(cols[0] + "|" + cols[3])          # 逻辑路径 | 包内路径

    list_js = "\n".join("        " + repr(s).replace("'", '"') + "," for s in items)
    out = TEMPLATE.replace("__ITEMS__", list_js) \
                  .replace("__COUNT__", str(len(items))) \
                  .replace("__VERSION__", version)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8", newline="\n")
    return len(items), out_path.stat().st_size


TEMPLATE = r'''// ==UserScript==
// @name         SweetHomeMaid 缺失资源批量下载（一次性工具）
// @namespace    sweethomemaid.tools
// @version      __VERSION__
// @description  把 config 里缺的 __COUNT__ 个 AdvStory 资源抓下来打包成 zip，用完就可以删
// @match        https://game.sweet-home-maid.com/*
// @run-at       document-idle
// @grant        unsafeWindow
// @grant        GM_registerMenuCommand
// ==/UserScript==

/* ------------------------------------------------------------------------
 * 用法：
 *   1. 在浏览器里正常打开游戏（保持登录）；
 *   2. Tampermonkey 图标 → 本脚本 → 「下载缺失的资源」；
 *   3. 左下角会显示进度（点它可取消），跑完自动下载一个 zip；
 *   4. 把 zip 解压到 Resources/ 下（压缩包里的目录结构就是 Card/storyXXXX.json 这样）；
 *   5. 回来跑 python Translation/tools/bundle_map.py --bundle AdvStory missing
 *      确认「缺 0 个」，然后就可以删掉这个脚本了。
 * ------------------------------------------------------------------------ */

(function () {
    'use strict';

    /* 资源地址前缀：优先按当前页面地址里的 /r/<版本>/ 拼，认不出再退回写死的 */
    var BASE = (function () {
        try {
            var m = String(location.href).match(/^(https?:\/\/[^\/]+\/r\/[^\/]+\/)/);
            if (m) return m[1];
        } catch (e) { /* 忽略 */ }
        return 'https://game.sweet-home-maid.com/r/7LCHDxB8msHV/';
    })();
    var CONCURRENCY = 5;        // 同时下几个
    var RETRY = 2;              // 单个文件失败重试次数

    /* 逻辑路径 | 包内路径（来自 Translation/work/missing_advstory.tsv） */
    var LIST = [
__ITEMS__
    ].map(function (s) { return s.split('|'); });

    var stopped = false;
    var running = false;

    /* ---------- 左下角进度（点一下取消） ---------- */
    var box = null;
    function status(text) {
        console.log('[下载] ' + text);
        try {
            if (!box) {
                box = document.createElement('div');
                box.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:2147483647;' +
                    'font:600 14px system-ui,sans-serif;color:#7CFC9B;cursor:pointer;' +
                    'text-shadow:0 0 4px rgba(0,0,0,.9),0 1px 2px rgba(0,0,0,.9)';
                box.title = '点这里取消下载';
                box.addEventListener('click', function () {
                    if (running) { stopped = true; box.textContent = '正在取消…'; }
                });
                (document.body || document.documentElement).appendChild(box);
            }
            box.textContent = text;
        } catch (e) { /* 忽略 */ }
    }

    function delay(ms) {
        return new Promise(function (r) { setTimeout(r, ms); });
    }

    /* ---------- 抓一个文件 ---------- */
    function grab(item, attempt) {
        attempt = attempt || 0;
        var url = BASE + item[1];
        return fetch(url, { credentials: 'same-origin' }).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        }).then(function (text) {
            var d;
            try { d = JSON.parse(text); } catch (e) { throw new Error('不是 JSON'); }
            var id = (d && d[5] && d[5][0] && d[5][0][1]) || '';
            if (!id) throw new Error('结构不对');
            if (id !== item[0].split('/')[1]) throw new Error('id 对不上：' + id);
            return { name: item[0] + '.json', text: text };
        })['catch'](function (e) {
            if (attempt < RETRY) {
                return delay(400 * (attempt + 1)).then(function () { return grab(item, attempt + 1); });
            }
            console.warn('[下载] 失败：' + item[0] + '（' + e + '）');
            return null;
        });
    }

    /* ---------- 打 zip（store 方式，不带压缩） ---------- */
    var CRC_TABLE = null;
    function crc32(u8) {
        if (!CRC_TABLE) {
            CRC_TABLE = new Uint32Array(256);
            for (var n = 0; n < 256; n++) {
                var c = n;
                for (var k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
                CRC_TABLE[n] = c >>> 0;
            }
        }
        var crc = 0xFFFFFFFF;
        for (var i = 0; i < u8.length; i++) crc = CRC_TABLE[(crc ^ u8[i]) & 0xFF] ^ (crc >>> 8);
        return (crc ^ 0xFFFFFFFF) >>> 0;
    }

    function makeZip(entries) {
        var enc = new TextEncoder();
        var parts = [], central = [], offset = 0;
        entries.forEach(function (e) {
            var name = enc.encode(e.name);
            var crc = crc32(e.data);
            var head = new Uint8Array(30 + name.length);
            var dv = new DataView(head.buffer);
            dv.setUint32(0, 0x04034b50, true);
            dv.setUint16(4, 20, true);
            dv.setUint16(6, 0x0800, true);      // 文件名按 UTF-8
            dv.setUint16(8, 0, true);           // 不压缩
            dv.setUint16(12, 0x0021, true);     // 日期
            dv.setUint32(14, crc, true);
            dv.setUint32(18, e.data.length, true);
            dv.setUint32(22, e.data.length, true);
            dv.setUint16(26, name.length, true);
            head.set(name, 30);
            parts.push(head, e.data);
            central.push({ name: name, crc: crc, size: e.data.length, offset: offset });
            offset += head.length + e.data.length;
        });
        var cdStart = offset, cdSize = 0;
        central.forEach(function (c) {
            var h = new Uint8Array(46 + c.name.length);
            var dv = new DataView(h.buffer);
            dv.setUint32(0, 0x02014b50, true);
            dv.setUint16(4, 20, true);
            dv.setUint16(6, 20, true);
            dv.setUint16(8, 0x0800, true);
            dv.setUint16(14, 0x0021, true);
            dv.setUint32(16, c.crc, true);
            dv.setUint32(20, c.size, true);
            dv.setUint32(24, c.size, true);
            dv.setUint16(28, c.name.length, true);
            dv.setUint32(42, c.offset, true);
            h.set(c.name, 46);
            parts.push(h);
            cdSize += h.length;
        });
        var end = new Uint8Array(22);
        var edv = new DataView(end.buffer);
        edv.setUint32(0, 0x06054b50, true);
        edv.setUint16(8, central.length, true);
        edv.setUint16(10, central.length, true);
        edv.setUint32(12, cdSize, true);
        edv.setUint32(16, cdStart, true);
        parts.push(end);
        return new Blob(parts, { type: 'application/zip' });
    }

    /* ---------- 主流程 ---------- */
    function start() {
        if (running) { status('正在下载中…'); return; }
        running = true;
        stopped = false;
        var queue = LIST.slice();
        var done = 0, ok = 0, failed = [], got = [];
        status('开始下载 ' + queue.length + ' 个文件…');

        function worker() {
            if (stopped || !queue.length) return Promise.resolve();
            var item = queue.shift();
            return grab(item).then(function (res) {
                done++;
                if (res) { ok++; got.push(res); } else { failed.push(item[0]); }
                if (done % 10 === 0 || !queue.length) {
                    status('下载中 ' + done + '/' + LIST.length + '，失败 ' + failed.length + '（点此取消）');
                }
                return worker();
            });
        }

        var ws = [];
        for (var i = 0; i < CONCURRENCY; i++) ws.push(worker());
        Promise.all(ws).then(function () {
            running = false;
            if (!got.length) { status('一个都没下到，看控制台'); return; }
            var entries = got.map(function (r) {
                return { name: r.name, data: new TextEncoder().encode(r.text) };
            });
            var blob = makeZip(entries);
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'sweethomemaid-missing-' + new Date().toISOString().slice(0, 10) + '.zip';
            document.body.appendChild(a);
            a.click();
            setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 5000);
            status('完成：成功 ' + ok + '，失败 ' + failed.length + '（zip 已开始下载）' + (stopped ? '，中途取消' : ''));
            if (failed.length) {
                console.warn('[下载] 这些没下到（再点一次菜单会重新下一遍，已下到的会覆盖）：\n' + failed.join('\n'));
            }
        });
    }

    if (typeof GM_registerMenuCommand === 'function') {
        GM_registerMenuCommand('下载缺失的资源（__COUNT__ 个）', start);
    }
    unsafeWindow.__smDownload = { start: start, list: LIST };
    console.log('[下载] 就绪：' + LIST.length + ' 个待下载。菜单里点「下载缺失的资源」，或控制台执行 __smDownload.start()');
})();
'''


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="把 missing_advstory.tsv 变成下载用的油猴脚本")
    p.add_argument("--root", help="仓库根目录，默认自动定位")
    p.add_argument("--tsv", default="Translation/work/missing_advstory.tsv", help="缺失清单")
    p.add_argument("--out", default="Translation/tools/download_missing.user.js", help="输出的脚本")
    p.add_argument("--version", default=date.today().strftime("%Y.%m.%d"), help="脚本版本号")
    args = p.parse_args()

    root = pathlib.Path(args.root).resolve() if args.root else pathlib.Path(__file__).resolve().parents[2]
    tsv = (root / args.tsv).resolve()
    out = (root / args.out).resolve()
    if not tsv.exists():
        raise SystemExit(f"[错误] 找不到缺失清单 {tsv}，先跑 bundle_map.py --bundle AdvStory missing")

    n, size = build(tsv, out, args.version)
    print(f"生成 {out}")
    print(f"  条目 {n} 个，脚本 {size / 1024:.1f} KB，版本 {args.version}")
    if not n:
        print("  [提示] 清单是空的 —— 说明手上的资源已经齐了")


if __name__ == "__main__":
    main()
