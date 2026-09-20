// ==UserScript==
// @name         SweetHomeMaid 消消乐汉化
// @namespace    sweethomemaid.translate
// @version      1.0
// @description  按 story id 把游戏加载的剧情替换成译文
// @author       SweetHomeMaidTranslate
// @match        https://game.sweet-home-maid.com/*
// @match        https://*.sweet-home-maid.com/*
// @match        https://sweet-home-maid.com/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      raw.githubusercontent.com
// @connect      cdn.jsdelivr.net
// ==/UserScript==

/* ------------------------------------------------------------------------
 * 游戏下载完资源后，从内容里读出它的 id：
 *   剧情    [5][0][1] = storyXXXXXXX
 *   主页气泡 [5][0][1] = MainGarden001Config
 * 再按固定规律拼出译文地址去取，取到就替换响应，取不到就原样放行。
 *
 * 译文放在仓库的 Translation/Resources/ 下，命名固定 <id>_cn.json，
 * 子目录跟游戏资源目录一致（story1… → Card/，storyEV… → Event/，MainGarden… → Garden/）。
 * 寝室剧情里的白屏闪烁特效(短闪两次再长闪一次)可以选择去除。默认不去除。想要去掉的话，在脚本菜单里打开选项"去除闪光弹"，
 * 再选移除全部闪光(默认选项)/ 移除短闪 / 移除长闪。
 * ------------------------------------------------------------------------ */

(function () {
    'use strict';

    var CONFIG = {
        DEBUG: true,             // 控制台打 [汉化] 开头的日志
        ENABLED: true,           // 改成 false 就完全不接管
        TOAST: true,             // 每替换一篇，在画面右下角提示一下（同一篇只提示一次）
        /* 闪光特效处理的状态：
           'keep'  = 保留所有闪光动画（默认）
           'none'  = 开关打开 + 移除全部闪光
           'short' = 开关打开 + 移除短闪
           'long'  = 开关打开 + 移除长闪 */
        FLASH_MODE: 'keep',
        VERSION: '1.0',

        /* 译文地址，前面的用不了自动试后面的 */
        BASES: [
            'https://raw.githubusercontent.com/Mephistor666/SweetHomeMaidTranslate/refs/heads/main/Translation/Resources/',
            'https://cdn.jsdelivr.net/gh/Mephistor666/SweetHomeMaidTranslate@main/Translation/Resources/'
        ],
        SUFFIX: '_cn.json',

        /* 只接管这些目录下的资源 */
        PREFIXES: ['assets/AdvStory/', 'assets/GardenMain001/', 'assets/GardenMain002/'],

        /* 剧情列表接口（标题在这里面），响应是加密的 JAES */
        TITLES: true,
        TITLE_FILE: 'titles_cn.json',
        TITLE_APIS: /\/scenario\/(card|event|main|mini-event)\/scenarios/i,
        /* 游戏主包 crypto-aes.ts 里写死的密钥 */
        JAES_KEY: '95364A843E75F7ED0A9D0A5E0BCBB3343FAF87AF40ABE0C21238D2ACEBA07E33',
        JAES_IV: 'B4461EE05E9A05528783A4BF13E5928D',

        /* id -> 译文所在子目录。 */
        DIR_RULES: [
            [/^storyEV/, ['Event/']],
            [/^storyBT/, ['Battle/']],
            [/^story0/, ['Main/']],
            [/^story1/, ['Card/']],
            [/^story2/, ['Main/', 'Card/']],
            [/^(default_|tutorial)/, ['Ordinary/']],
            [/^MainGarden\d+Config$/, ['Garden/']]
        ],

        FAIL_LIMIT: 3            // 地址连续不通FAIL_LIMIT次数后，就不再尝试翻译
    };

    var FLASH_MODES = { none: '移除全部闪光', short: '移除短闪', long: '移除长闪' };
    var flashMode = FLASH_MODES[CONFIG.FLASH_MODE] ? CONFIG.FLASH_MODE : 'keep';
    try {
        if (typeof GM_getValue === 'function') {
            var savedMode = GM_getValue('sm_flash_mode', '');
            if (savedMode === 'keep' || FLASH_MODES[savedMode]) flashMode = savedMode;
        }
    } catch (e) { }

    function flashLabel(mode) {
        return mode === 'keep' ? '保留闪光' : FLASH_MODES[mode];
    }

    function setFlashMode(mode) {
        if (mode !== 'keep' && !FLASH_MODES[mode]) return;
        flashMode = mode;
        try {
            if (typeof GM_setValue === 'function') GM_setValue('sm_flash_mode', mode);
        } catch (e) { }
        buildFlashMenu();
        var msg = '闪光弹处理：' + flashLabel(mode) + '（之后加载的剧情立即生效）';
        log(msg);
        toast(msg);
    }

    var menuIds = [];
    function buildFlashMenu() {
        if (typeof GM_registerMenuCommand !== 'function') return;
        if (typeof GM_unregisterMenuCommand === 'function') {
            menuIds.forEach(function (id) {
                try { GM_unregisterMenuCommand(id); } catch (e) { }
            });
            menuIds = [];
        } else if (menuIds.length) {
            return;
        }
        var on = flashMode !== 'keep';
        menuIds.push(GM_registerMenuCommand('去除闪光弹：' + (on ? '开' : '关'), function () {
            setFlashMode(on ? 'keep' : 'none');
        }));
        if (!on) return;
        Object.keys(FLASH_MODES).forEach(function (m) {
            menuIds.push(GM_registerMenuCommand('　· ' + FLASH_MODES[m] + (m === flashMode ? '（当前）' : ''),
                function () { setFlashMode(m); }));
        });
    }
    buildFlashMenu();

    function flashIsLong(line) {
        var p = line.split(",");
        if (p.length < 5) return false;
        var total = 0;
        for (var i = 2; i <= 4; i++) total += parseFloat(p[i]) || 0;
        return total >= 0.5;
    }

    var W = (typeof unsafeWindow !== 'undefined' && unsafeWindow) ? unsafeWindow : window;
    var cache = {};              // id -> Promise<string|null>
    var fails = 0;               // 连续「地址不通」次数

    function log() {
        if (!CONFIG.DEBUG) return;
        var a = [].slice.call(arguments);
        a.unshift('[汉化]');
        console.log.apply(console, a);
    }
    function warn() {
        var a = [].slice.call(arguments);
        a.unshift('[汉化]');
        console.warn.apply(console, a);
    }

    function joinUrl(prefix, file) {
        if (!prefix || !file) return null;
        return prefix.charAt(prefix.length - 1) === '/' ? prefix + file : prefix + '/' + file;
    }
    function isOwn(s) {
        for (var i = 0; i < CONFIG.BASES.length; i++) {
            if (CONFIG.BASES[i] && s.indexOf(CONFIG.BASES[i]) === 0) return true;
        }
        return false;
    }

    function isTarget(url) {
        var s = String(url || '');
        if (!s || s.slice(0, 5) === 'blob:' || s.slice(0, 5) === 'data:') return false;
        var low = s.toLowerCase();
        if (isOwn(s)) return false;
        if (/\/config[^\/]*\.json/.test(low)) return false;
        var cut = low.search(/[?#]/);
        if (cut >= 0) low = low.slice(0, cut);
        if (low.slice(-5) !== '.json') return false;
        for (var i = 0; i < CONFIG.PREFIXES.length; i++) {
            if (low.indexOf(String(CONFIG.PREFIXES[i]).toLowerCase()) >= 0) return true;
        }
        return false;
    }

    /* 剧情列表接口：/scenario/card/scenarios/108022、/scenario/main/scenarios/… 之类 */
    function isTitleApi(url) {
        return !!CONFIG.TITLES && CONFIG.TITLE_APIS.test(String(url || ''));
    }

    function idOf(text) {
        if (!text || text.length < 20) return '';
        try {
            var d = JSON.parse(text);
            if (d && d[5] && d[5][0] && typeof d[5][0][1] === 'string') return d[5][0][1];
        } catch (e) { /* 不是剧情 JSON */ }
        return '';
    }

    function dirsFor(id) {
        for (var i = 0; i < CONFIG.DIR_RULES.length; i++) {
            if (CONFIG.DIR_RULES[i][0].test(id)) return CONFIG.DIR_RULES[i][1];
        }
        return null;
    }

    function decodeBuffer(buf) {
        try {
            var Dec = W.TextDecoder || (typeof TextDecoder !== 'undefined' ? TextDecoder : null);
            return Dec ? new Dec('utf-8').decode(buf) : '';
        } catch (e) { return ''; }
    }

    function readText(xhr) {
        try {
            var t = '';
            try { t = xhr.responseType || ''; } catch (e) { t = ''; }
            if (t === '' || t === 'text') return xhr.responseText || '';
            if (t === 'json') return xhr.response ? JSON.stringify(xhr.response) : '';
            if (t === 'arraybuffer' && xhr.response) return decodeBuffer(xhr.response);
        } catch (e) {}
        return '';
    }

    function applyResponse(xhr, text) {
        function put(prop, val) {
            try { Object.defineProperty(xhr, prop, { value: val, configurable: true, writable: true }); }
            catch (e) { warn('响应替换失败：' + prop, e); }
        }
        var t = '';
        try { t = xhr.responseType || ''; } catch (e) { t = ''; }
        if (t === '' || t === 'text') { put('responseText', text); put('response', text); }
        else if (t === 'json') { try { put('response', JSON.parse(text)); } catch (e) { warn('译文不是合法 JSON'); } }
        else if (t === 'arraybuffer') {
            try {
                var Enc = W.TextEncoder || (typeof TextEncoder !== 'undefined' ? TextEncoder : null);
                if (Enc) put('response', new Enc().encode(text).buffer);
            } catch (e) { warn('译文转 arraybuffer 失败', e); }
        } else {
            warn('不认识的 responseType，跳过：' + t);
        }
    }

    function loadTranslation(id) {
        if (cache[id]) return cache[id];
        var p = (function () {
            var dirs = dirsFor(id);
            if (!dirs) {
                warn('没见过的 id 形态，跳过：' + id);
                return Promise.resolve(null);
            }
            var files = dirs.map(function (d) { return d + id + CONFIG.SUFFIX; });

            function tryBase(bi) {
                if (bi >= CONFIG.BASES.length) {
                    fails++;
                    if (fails >= CONFIG.FAIL_LIMIT) {
                        CONFIG.ENABLED = false;
                        warn('译文地址连续失败 ' + fails + ' 次，本次不再翻译（刷新页面可重来）');
                    }
                    return Promise.resolve(null);
                }
                var base = CONFIG.BASES[bi];
                return Promise.all(files.map(function (f) {
                    return getFile(joinUrl(base, f));
                })).then(function (rs) {
                    for (var i = 0; i < rs.length; i++) {
                        if (rs[i].status === 200) {
                            fails = 0;
                            return rs[i].text;
                        }
                    }

                    var netErr = rs.some(function (r) { return r.status === -1; });
                    return netErr ? tryBase(bi + 1) : null;
                });
            }
            return tryBase(0);
        })();
        cache[id] = p;
        return p;
    }

    function getFile(url) {
        return new Promise(function (resolve) {
            if (typeof GM_xmlhttpRequest !== 'function') {
                warn('没有 GM_xmlhttpRequest 权限，取不到译文');
                resolve({ status: -1, text: '' });
                return;
            }
            GM_xmlhttpRequest({
                method: 'GET',
                url: url,
                timeout: 20000,
                onload: function (r) { resolve({ status: r.status, text: r.responseText || '' }); },
                onerror: function () { resolve({ status: -1, text: '' }); },   // -1 = 网络层失败
                ontimeout: function () { resolve({ status: -1, text: '' }); }
            });
        });
    }

    function stripFlashScreen(text) {
        if (flashMode === 'keep') return null;              // 保留闪光：一个字都不动
        if (!text || text.indexOf("FlashScreen") < 0) return null;
        if (!/[ＲR][1１][8８]/.test(text)) return null;
        try {
            var d = JSON.parse(text);
            var t = d && d[5] && d[5][0] && d[5][0][2];
            if (typeof t !== "string") return null;
            var sep = t.indexOf("\r\n") >= 0 ? "\r\n" : "\n";
            var lines = t.split(sep);
            var out = [], removed = 0, kept = 0;
            for (var i = 0; i < lines.length; i++) {
                if (lines[i].indexOf("@FlashScreen") === 0) {
                    var isLong = flashIsLong(lines[i]);
                    var wantKeep = flashMode === 'short' ? isLong : (flashMode === 'long' ? !isLong : false);
                    if (wantKeep) { kept++; out.push(lines[i]); continue; }
                    if (i + 1 < lines.length && lines[i + 1].indexOf("@WaitMethod") === 0) i++;
                    removed++;
                    continue;
                }
                out.push(lines[i]);
            }
            if (!removed) return null;
            d[5][0][2] = out.join(sep);
            log(FLASH_MODES[flashMode] + '：去掉 @FlashScreen × ' + removed
                + (kept ? '，保留 × ' + kept : ''));
            return JSON.stringify(d);
        } catch (e) {
            warn('处理 @FlashScreen 出错：' + e);
            return null;
        }
    }

    /* ---------- 剧情列表的标题：JAES 接口响应 ----------
       游戏自己的EXSerializer格式：'JAES' + IV(16 字节) + Base64(AES-256-CBC(JSON))，
       密钥写死在游戏主包里、客户端自己解密，所以脚本也照着解、改完再照原样加密回去。 */
    var B64CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

    function hexToBytes(hex) {
        var out = new Uint8Array(hex.length >> 1);
        for (var i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
        return out;
    }
    function bytesToAscii(bytes) {          // 只用在 Base64、'JAES' 这种纯 ASCII 段上
        var s = '';
        for (var i = 0; i < bytes.length; i += 2048) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + 2048));
        }
        return s;
    }
    function b64ToBytes(str) {
        var clean = String(str).replace(/[^A-Za-z0-9+/=]/g, ''), out = [], buf = 0, bits = 0;
        for (var i = 0; i < clean.length; i++) {
            var c = clean.charAt(i);
            if (c === '=') break;
            var v = B64CHARS.indexOf(c);
            if (v < 0) continue;
            buf = (buf << 6) | v;
            bits += 6;
            if (bits >= 8) { bits -= 8; out.push((buf >> bits) & 0xFF); }
        }
        return new Uint8Array(out);
    }
    function bytesToB64(bytes) {
        var s = '';
        for (var i = 0; i < bytes.length; i += 3) {
            var b0 = bytes[i], b1 = bytes[i + 1], b2 = bytes[i + 2];
            s += B64CHARS.charAt(b0 >> 2);
            s += B64CHARS.charAt(((b0 & 3) << 4) | ((b1 || 0) >> 4));
            s += i + 1 < bytes.length ? B64CHARS.charAt(((b1 & 15) << 2) | ((b2 || 0) >> 6)) : '=';
            s += i + 2 < bytes.length ? B64CHARS.charAt(b2 & 63) : '=';
        }
        return s;
    }
    function utf8Encode(str) {
        var Enc = W.TextEncoder || (typeof TextEncoder !== 'undefined' ? TextEncoder : null);
        try { return Enc ? new Enc().encode(str) : null; } catch (e) { return null; }
    }
    function subtleCrypto() {
        var c = W.crypto || (typeof crypto !== 'undefined' ? crypto : null);
        return c && c.subtle ? c.subtle : null;
    }
    function aesKey(usage) {
        var s = subtleCrypto();
        return s ? s.importKey('raw', hexToBytes(CONFIG.JAES_KEY), { name: 'AES-CBC' }, false, usage) : null;
    }

    /* JAES 字节 -> 明文对象；不是 JAES 就返回 null（原样放行） */
    function jaesDecrypt(bytes) {
        var s = subtleCrypto();
        if (!s || !bytes || bytes.length < 21) return Promise.resolve(null);
        if (bytesToAscii(bytes.subarray(0, 4)) !== 'JAES') return Promise.resolve(null);
        var key = aesKey(['decrypt', 'encrypt']);
        if (!key) return Promise.resolve(null);
        var iv = bytes.subarray(4, 20);
        var ct = b64ToBytes(bytesToAscii(bytes.subarray(20)));
        return key.then(function (k) {
            return s.decrypt({ name: 'AES-CBC', iv: iv }, k, ct);
        }).then(function (buf) {
            var text = decodeBuffer(buf);
            return text ? JSON.parse(text) : null;
        }, function (e) {
            warn('解密接口响应失败：' + e);
            return null;
        });
    }

    function jaesEncrypt(obj) {
        var s = subtleCrypto(), key = aesKey(['encrypt', 'decrypt']);
        var data = utf8Encode(JSON.stringify(obj));
        if (!s || !key || !data) return Promise.resolve(null);
        var iv = hexToBytes(CONFIG.JAES_IV);
        var tag = utf8Encode('JAES');
        return key.then(function (k) {
            return s.encrypt({ name: 'AES-CBC', iv: iv }, k, data);
        }).then(function (buf) {
            var body = utf8Encode(bytesToB64(new Uint8Array(buf)));
            var out = new Uint8Array(tag.length + iv.length + body.length);
            out.set(tag, 0);
            out.set(iv, tag.length);
            out.set(body, tag.length + iv.length);
            return out;
        }, function (e) {
            warn('加密标题列表失败：' + e);
            return null;
        });
    }

    var titleCache = null;
    function loadTitles() {
        if (titleCache) return titleCache;
        titleCache = (function () {
            function tryBase(bi) {
                if (!CONFIG.TITLES || bi >= CONFIG.BASES.length) return Promise.resolve(null);
                return getFile(joinUrl(CONFIG.BASES[bi], CONFIG.TITLE_FILE)).then(function (r) {
                    if (!r || r.status !== 200) return (r && r.status === -1) ? tryBase(bi + 1) : null;
                    try {
                        var t = JSON.parse(r.text);
                        return (t && typeof t === 'object') ? t : null;
                    } catch (e) {
                        warn('标题表不是合法 JSON');
                        return null;
                    }
                });
            }
            return tryBase(0);
        })();
        return titleCache;
    }

    function storyIdOf(item) {
        var rid = item && item.scenario_rid;
        if (typeof rid === 'string' && rid) {
            var seg = rid.split('/');
            seg = seg[seg.length - 1];
            if (/^story[0-9A-Za-z_]+$/.test(seg)) return seg;
        }
        for (var k in item) {                                  // 兜底：card_scenario_id / main_scenario_id …
            if (/_scenario_id$/.test(k) && typeof item[k] === 'number') return 'story' + item[k];
        }
        return '';
    }

    /* 列表响应里所有带 scenario_title 的节点，按 story id 换成中文 */
    function patchTitles(obj, table) {
        var n = 0;
        (function walk(node) {
            if (!node || typeof node !== 'object') return;
            if (Object.prototype.toString.call(node) === '[object Array]') {
                for (var i = 0; i < node.length; i++) walk(node[i]);
                return;
            }
            if (typeof node.scenario_title === 'string') {
                var id = storyIdOf(node), cn = id ? table[id] : '';
                if (cn && cn !== node.scenario_title) {
                    node.scenario_title = cn;
                    n++;
                }
            }
            for (var k in node) {
                if (Object.prototype.hasOwnProperty.call(node, k)) walk(node[k]);
            }
        })(obj);
        return n;
    }

    function readBytes(xhr) {
        try {
            var t = xhr.responseType || '';
            if (t === 'arraybuffer' && xhr.response) return new Uint8Array(xhr.response);
            if (t === '' || t === 'text') {
                var s = xhr.responseText || '';
                if (s.slice(0, 4) === 'JAES') {                 // 万一不是 arraybuffer，也尽力还原字节
                    var out = new Uint8Array(s.length);
                    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xFF;
                    return out;
                }
            }
        } catch (e) { }
        return null;
    }
    function applyBytes(xhr, bytes) {
        try {
            var buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
            Object.defineProperty(xhr, 'response', { value: buf, configurable: true, writable: true });
        } catch (e) {
            warn('替换二进制响应失败：' + e);
        }
    }

    /* ---------- 替换成功时在画面角上提示一下（同一篇只提示一次） ---------- */
    var toasted = {};
    function toast(text) {
        if (!CONFIG.TOAST) return;
        function show() {
            try {
                var old = document.getElementById('sm-hanhua-tip');
                if (old && old.parentNode) old.parentNode.removeChild(old);
                var d = document.createElement('div');
                d.id = 'sm-hanhua-tip';
                d.textContent = text;
                d.style.cssText = [
                    'position:fixed', 'right:12px', 'bottom:12px', 'z-index:2147483647',
                    'font-size:12px', 'font-weight:200', 'color:#7CFC9B',
                    'pointer-events:none', 'font-family:system-ui,sans-serif', 'opacity:1',
                    'text-shadow:1px 0 1px rgba(0,0,0,.5)',
                    'transition:opacity 1.2s ease'
                ].join(';');
                (document.body || document.documentElement).appendChild(d);
                setTimeout(function () { d.style.opacity = '0'; }, 2500);
                setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); }, 4000);
            } catch (e) {
                warn('提示条没显示出来：' + e);
            }
        }
        if (document.body || document.documentElement) show();
        else document.addEventListener('DOMContentLoaded', show, { once: true });
    }

    function hookXHR(xhr, url) {
        if (xhr.__smHooked) return;
        xhr.__smHooked = 1;

        var isApi = isTitleApi(url);

        var page = { rs: [], load: [], end: [] };
        var native = xhr.addEventListener;

        [['onreadystatechange', 'rs'], ['onload', 'load'], ['onloadend', 'end']].forEach(function (pair) {
            var prop = pair[0], key = pair[1], prev = null;
            try { prev = xhr[prop]; } catch (e) { prev = null; }
            if (typeof prev === 'function') page[key].push(prev);
            try {
                Object.defineProperty(xhr, prop, {
                    configurable: true,
                    get: function () { return page[key].length ? page[key][page[key].length - 1] : null; },
                    set: function (fn) { page[key].length = 0; if (typeof fn === 'function') page[key].push(fn); }
                });
            } catch (e) { }
        });

        xhr.addEventListener = function (type, fn, opts) {
            var key = type === 'readystatechange' ? 'rs' : (type === 'load' ? 'load' : (type === 'loadend' ? 'end' : ''));
            if (key) { page[key].push(fn); return; }
            return native.call(xhr, type, fn, opts);
        };

        function fire(key, type) {
            page[key].slice().forEach(function (fn) {
                try { fn.call(xhr, { type: type, target: xhr, currentTarget: xhr }); }
                catch (e) { warn('页面回调出错：', e); }
            });
        }

        native.call(xhr, 'readystatechange', function () {
            if (xhr.readyState !== 4) { fire('rs', 'readystatechange'); return; }
            var ok = xhr.status >= 200 && xhr.status < 400;
            var finish = function () {
                fire('rs', 'readystatechange');
                if (ok) fire('load', 'load');
                fire('end', 'loadend');
            };

            if (isApi) {
                var raw = ok ? readBytes(xhr) : null;
                if (!raw) { finish(); return; }
                loadTitles().then(function (table) {
                    if (!table) { log('没有标题表，列表原样放行'); finish(); return; }
                    return jaesDecrypt(raw).then(function (obj) {
                        if (!obj) { finish(); return; }
                        var hit = patchTitles(obj, table);
                        if (!hit) { log('这个列表里没有译文标题'); finish(); return; }
                        return jaesEncrypt(obj).then(function (out) {
                            if (!out) { finish(); return; }
                            applyBytes(xhr, out);
                            log('替换剧情标题 × ' + hit);
                            if (!toasted.__titles) {
                                toasted.__titles = 1;
                                toast('汉化已生效：剧情标题 × ' + hit);
                            }
                            finish();
                        });
                    });
                }, finish);
                return;
            }

            var text = ok ? readText(xhr) : '';
            var id = text ? idOf(text) : '';
            var done = function (cn) {
                if (cn) {
                    applyResponse(xhr, stripFlashScreen(cn) || cn);
                    log('替换 ' + id);
                    if (!toasted[id]) {
                        toasted[id] = 1;
                        toast('汉化已生效：' + id);
                    }
                } else if (ok && text) {
                    var fixed = stripFlashScreen(text);      // 没有译文也照选项处理闪烁
                    if (fixed) {
                        applyResponse(xhr, fixed);
                        log('处理 @FlashScreen：' + id);
                    } else if (id) {
                        log('没有译文，放行原文：' + id);
                    }
                }
                finish();
            };
            if (!ok || !id) { done(null); return; }
            loadTranslation(id).then(done, function () { done(null); });
        });
        native.call(xhr, 'load', function () { });
        native.call(xhr, 'loadend', function () { });
    }

    var XHRProto = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
    if (XHRProto && !XHRProto.__smPatched) {
        var origOpen = XHRProto.open;
        XHRProto.open = function (method, url) {
            try {
                if (CONFIG.ENABLED && (isTarget(url) || isTitleApi(url))) hookXHR(this, url);
            } catch (e) {
                warn('拦截 XHR 出错：', e);
            }
            return origOpen.apply(this, arguments);
        };
        XHRProto.__smPatched = true;
    }

    log('v' + CONFIG.VERSION + ' 已注入：' + (XHRProto ? 'XHR hook OK' : 'XHR hook fail')
        + '；译文地址 ' + CONFIG.BASES[0] + '；闪光弹：' + flashLabel(flashMode)
        + '；剧情标题：' + (CONFIG.TITLES ? '开' : '关'));
})();
