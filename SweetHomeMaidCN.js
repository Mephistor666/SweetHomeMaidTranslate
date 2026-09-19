// ==UserScript==
// @name         SweetHomeMaid 消消乐汉化
// @namespace    sweethomemaid.translate
// @version      1.0
// @description  按 story id 把游戏加载的剧情和主页气泡换成译文
// @author       SweetHomeMaidTranslate
// @match        https://game.sweet-home-maid.com/*
// @match        https://*.sweet-home-maid.com/*
// @match        https://sweet-home-maid.com/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
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
 * ------------------------------------------------------------------------ */

(function () {
    'use strict';

    var CONFIG = {
        DEBUG: true,             // 控制台打 [汉化] 开头的日志
        ENABLED: true,           // 改成 false 就完全不接管
        TOAST: true,             // 每替换一篇，在画面右下角提示一下（同一篇只提示一次）
        VERSION: '1.0',

        /* 译文地址，前面的用不了自动试后面的 */
        BASES: [
            'https://raw.githubusercontent.com/Mephistor666/SweetHomeMaidTranslate/refs/heads/main/Translation/Resources/',
            'https://cdn.jsdelivr.net/gh/Mephistor666/SweetHomeMaidTranslate@main/Translation/Resources/'
        ],
        SUFFIX: '_cn.json',

        /* 只接管这些目录下的资源 */
        PREFIXES: ['assets/AdvStory/', 'assets/GardenMain001/', 'assets/GardenMain002/'],

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

    /* 依 responseType 把原文读成文本 */
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

    function hookXHR(xhr) {
        if (xhr.__smHooked) return;
        xhr.__smHooked = 1;

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
            var text = ok ? readText(xhr) : '';
            var id = text ? idOf(text) : '';
            var done = function (cn) {
                if (cn) {
                    applyResponse(xhr, cn);
                    log('替换 ' + id);
                    if (!toasted[id]) {
                        toasted[id] = 1;
                        toast('汉化已生效：' + id);
                    }
                } else if (id) {
                    log('没有译文，放行原文：' + id);
                }
                fire('rs', 'readystatechange');
                if (ok) fire('load', 'load');
                fire('end', 'loadend');
            };
            if (!ok || !id) { done(null); return; }
            loadTranslation(id).then(done, function () { done(null); });
        });
        native.call(xhr, 'load', function () { /* 统一在上面放行 */ });
        native.call(xhr, 'loadend', function () { /* 同上 */ });
    }

    var XHRProto = W.XMLHttpRequest && W.XMLHttpRequest.prototype;
    if (XHRProto && !XHRProto.__smPatched) {
        var origOpen = XHRProto.open;
        XHRProto.open = function (method, url) {
            try {
                if (CONFIG.ENABLED && isTarget(url)) hookXHR(this);
            } catch (e) {
                warn('拦截 XHR 出错：', e);
            }
            return origOpen.apply(this, arguments);
        };
        XHRProto.__smPatched = true;
    }

    log('v' + CONFIG.VERSION + ' 已注入：' + (XHRProto ? 'XHR hook OK' : 'XHR hook fail')
        + '；译文地址 ' + CONFIG.BASES[0]);
})();
