#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sakura_translate.py —— 剧本对白抽取 / Sakura 翻译 / 原样回填

把 Cocos 剧本 JSON（Resources/assets/AdvStory/import/**.json）里 @ShowCastMessage
的正文抽出来，交给本地 Sakura 翻译，再把译文写回去。指令行（@）、资源参数行（$$）、
注释行（//）一律不参与翻译，也保证不被改动。

四步流程：
    extract    扫描剧本 JSON，抽出对白 -> work/segments.jsonl
    translate  调用 Sakura（OpenAI 兼容接口）-> work/translated.jsonl（可断点续跑）
    inject     回填译文 -> Translation/Resources/**.json
    check      质检报告（漏翻、假名残留、符号丢失、行数）

只依赖 Python 标准库（3.9+）。

示例：
    python Translation/tools/sakura_translate.py extract
    python Translation/tools/sakura_translate.py translate --limit 20
    python Translation/tools/sakura_translate.py check
    python Translation/tools/sakura_translate.py inject
"""

from __future__ import annotations

import argparse
import codecs
import concurrent.futures as cf
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# 默认配置（可被 Translation/config.json 覆盖）
# --------------------------------------------------------------------------

DEFAULTS = {
    "endpoint": "http://127.0.0.1:8080/v1/chat/completions",
    "endpoint_completion": "http://127.0.0.1:8080/completion",
    "health_url": "http://127.0.0.1:8080/v1/models",
    "mode": "chat",  # chat = /v1/chat/completions；raw = /completion 手拼模板
    "model": "sakura",
    "system_prompt": (
        "你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，"
        "并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，"
        "不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。"
    ),
    "temperature": 0.3,
    "top_p": 0.8,
    "top_k": 40,
    "repeat_penalty": 1.0,
    "max_tokens": 512,
    "timeout": 180,
    "workers": 4,
    "batch_lines": 6,
    "batch_chars": 300,
    "history_chars": 200,
    "retries": 3,
    "merge_split_ratio": 1.5,   # 两行合成一行时：译文比原文第一行长这么多倍就拆回两行
    "text_commands": ["@ShowCastMessage", "@ShowMessage", "@ShowSkitCastMessage"],
    "speaker_prefix": True,
    "speakers": {},
    "glossary": "Translation/glossary.txt",
    "input": "Resources/assets/AdvStory/import",
    "segments": "Translation/work/segments.jsonl",
    "translated": "Translation/work/translated.jsonl",
    "in_root": "Resources",
    "out_root": "Translation/Resources",
}

# 判断"还是日文"用的假名。故意不含「ー」(U+30FC) 和「・」(U+30FB)：
# 中文译文里也常拿它们表示拖长音（例如「好ー，就这样别动哦ー？」），算进去会误判。
KANA_RE = re.compile(r"[\u3041-\u309f\u30a1-\u30fa]")
# 译文里保留的注音，例如「白張（しらはり）」，括号里的假名不算没翻
BRACKET_RE = re.compile(r"[（(][^）)]*[）)]")


def looks_untranslated(text: str) -> bool:
    """这行是不是还没翻：看括号外的假名（注音不算）。"""
    return bool(KANA_RE.search(BRACKET_RE.sub("", text)))


ECHO_RE = re.compile(
    r"^\s*(历史翻译|参考以下术语表|根据以上术语表|\[History\]|\[Glossary\]|\[Input\]|"
    r"system|user|assistant|翻译结果|译文)\s*[:：]?\s*$"
)
CMD_RE = re.compile(r"^(@\w+),(.*)$")
# 模型回显的 名字「台词」 外壳：名字 1~12 字、不含引号
WRAP_RE = re.compile(r"^([^「」\n]{1,12})「(.*)」$", re.S)
OPEN_ONLY_RE = re.compile(r"^([^「」\n]{1,12})「(.*)$", re.S)
NOT_A_NAME = set("。！？!?，,、：:；;～~…—")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def find_root() -> Path:
    """从脚本位置往上找仓库根目录。

    优先认 .git；没有 .git 时才退回「同时有 Resources 和 Translation 的那层」，
    否则 Translation/Resources 这种目录会把自己误判成根目录。
    """
    here = Path(__file__).resolve()
    parents = [here.parent, *here.parents]
    for p in parents:
        if (p / ".git").exists():
            return p
    for p in parents:
        if (p / "Resources").is_dir() and (p / "Translation").is_dir():
            return p
    return Path.cwd()


def load_config(root: Path, path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = Path(path) if path else root / "Translation" / "config.json"
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    if cfg_path.exists():
        user = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


def sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def read_json_text(path: Path) -> tuple[str, bool]:
    """读 JSON 文本，返回 (文本, 是否有 BOM)。"""
    raw = path.read_bytes()
    return raw.decode("utf-8-sig"), raw.startswith(codecs.BOM_UTF8)


def detect_style(raw: str) -> dict:
    """记录原文件的排版风格，回填时照抄。

    raw 里真实的 \\r 只可能来自 JSON 自身的排版（字符串内的 CR/LF 会被转义成
    两个字符 \\r \\n），所以可以直接据此判断文件用 CRLF 还是 LF。
    """
    return {
        "pretty": bool(re.match(r"\[\s*\n", raw)),
        "crlf": "\r\n" in raw,
        "trailing_crlf": raw.endswith("\r\n"),
        "trailing_newline": raw.endswith("\n"),
    }


def dump_like(data, style: dict, has_bom: bool) -> bytes:
    if style.get("pretty"):
        s = json.dumps(data, ensure_ascii=False, indent=4)
    else:
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if style.get("crlf"):
        s = s.replace("\n", "\r\n")
    if style.get("trailing_crlf"):
        s += "\r\n"
    elif style.get("trailing_newline"):
        s += "\n"
    return s.encode("utf-8-sig" if has_bom else "utf-8")


def get_at(obj, path):
    for i in path:
        obj = obj[i]
    return obj


def set_at(obj, path, value):
    for i in path[:-1]:
        obj = obj[i]
    obj[path[-1]] = value


def find_text_path(data, needles=("@ShowCastMessage", "@ShowMessage", "@ShowSkitCastMessage")) -> list | None:
    """在 JSON 里定位剧本正文所在的那个字符串（返回索引路径）。

    游戏里有三套对话指令：@ShowCastMessage（最常用）、@ShowMessage、
    @ShowSkitCastMessage（演出里的对话）。有的文件只用其中一种，所以按"含有任意一种"来找。
    """
    if isinstance(needles, str):
        needles = (needles,)
    best = None
    stack = [(data, [])]
    while stack:
        cur, path = stack.pop()
        if isinstance(cur, str):
            if any(n in cur for n in needles) and (best is None or len(cur) > len(best[1])):
                best = (path, cur)
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                stack.append((v, path + [i]))
        elif isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((v, path + [k]))
    return best[0] if best else None


def split_lines(text: str) -> tuple[list[str], str]:
    sep = "\r\n" if "\r\n" in text else "\n"
    return text.split(sep), sep


# --------------------------------------------------------------------------
# 剧本解析
# --------------------------------------------------------------------------


def scan_blocks(lines: list[str], text_commands: list[str]) -> list[dict]:
    """抽出所有「会显示给玩家的文本块」。

    块的构成：@ShowCastMessage,<说话人> 之后、遇到空行/@指令/注释之前的连续行。
    其中 $$ 开头的行是语音、立绘等资源参数，不算正文。

    空行的特殊处理：有的文件写成 `$$Voice/...` + 空行 + 正文，空行只是把参数和正文隔开，
    所以当空行后面还有正文时，把空行也收进块里（原样保留，不参与翻译），继续往下收正文。
    """
    blocks = []
    i = 0
    while i < len(lines):
        m = CMD_RE.match(lines[i].strip())
        if m and m.group(1) in text_commands:
            speaker = m.group(2)
            region = []
            j = i + 1
            while j < len(lines):
                ts = lines[j].strip()
                if ts.startswith("@") or ts.startswith("//"):
                    break
                if ts == "":
                    k = j
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    nxt = lines[k].strip() if k < len(lines) else ""
                    if nxt == "" or nxt.startswith(("@", "//", "$$")):
                        break                     # 后面没有正文了，空行就是块结束
                    for b in range(j, k):         # 空行只是分隔，收进来原样保留
                        region.append({"kind": "blank", "text": lines[b]})
                    j = k
                    continue
                region.append(
                    {"kind": "param" if ts.startswith("$$") else "text", "text": lines[j]}
                )
                j += 1
            if region:
                blocks.append(
                    {
                        "cmd_line": i,
                        "cmd": m.group(1),
                        "speaker": speaker,
                        "region": region,
                    }
                )
            i = j
            continue
        i += 1
    return blocks


def scan_unhandled(lines: list[str], text_commands: list[str]) -> list[dict]:
    """安全网：找出「不是白名单指令、但后面跟着日文正文」的地方。"""
    out = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        m = CMD_RE.match(s)
        if m and m.group(1) not in text_commands:
            j = i + 1
            prose = []
            while j < len(lines):
                ts = lines[j].strip()
                if ts == "" or ts.startswith(("@", "//", "$$")):
                    break
                if KANA_RE.search(ts):
                    prose.append(ts)
                j += 1
            if prose:
                out.append({"cmd": m.group(1), "line": i, "sample": prose[:2]})
        i += 1
    return out


def collect_files(root: Path, target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*.json") if p.is_file())
    raise SystemExit(f"[错误] 输入路径不存在: {target}")


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------


def cmd_extract(args, cfg: dict, root: Path) -> None:
    target = Path(args.input) if args.input else root / cfg["input"]
    if not target.is_absolute():
        target = root / target
    out_path = Path(args.output) if args.output else root / cfg["segments"]
    if not out_path.is_absolute():
        out_path = root / out_path

    records, meta_files, skipped, warnings = [], {}, [], []
    for f in collect_files(root, target):
        raw_text, has_bom = read_json_text(f)
        style = detect_style(raw_text)
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            skipped.append((f, f"JSON 解析失败: {e}"))
            continue
        text_path = find_text_path(data)
        if text_path is None:
            skipped.append((f, "没找到 @ShowCastMessage，跳过"))
            continue

        text = get_at(data, text_path)
        lines, sep = split_lines(text)
        blocks = scan_blocks(lines, cfg["text_commands"])
        if not blocks:
            skipped.append((f, "没有可翻译的文本块，跳过"))
            continue

        rel = f.relative_to(root).as_posix() if f.is_relative_to(root) else f.name
        asset = ""
        try:
            asset = str(data[5][0][1])
        except Exception:
            pass

        # 回填用的元信息：路径 + 原始风格，保证输出文件除正文外逐字节不变
        roundtrip = dump_like(data, style, has_bom).decode("utf-8-sig") == raw_text
        meta_files[rel] = {
            "asset": asset,
            "text_path": text_path,
            "style": style,
            "has_bom": has_bom,
            "sep": "\\r\\n" if sep == "\r\n" else "\\n",
            "sha1": sha1_bytes(f.read_bytes()),
            "roundtrip_ok": roundtrip,
        }
        if not roundtrip:
            warnings.append(f"{rel}: 反序列化再序列化与原文不完全一致，回填前请留意")

        for n, b in enumerate(blocks):
            src = [r["text"] for r in b["region"] if r["kind"] == "text"]
            if not src:
                continue          # 只有 $$ 语音参数行、没有正文的块，没什么可翻
            records.append(
                {
                    "id": f"{rel}#{b['cmd_line']}",
                    "file": rel,
                    "asset": asset,
                    "block": n,
                    "cmd_line": b["cmd_line"],
                    "speaker": b["speaker"],
                    "region": b["region"],
                    "src": src,
                }
            )

        for u in scan_unhandled(lines, cfg["text_commands"]):
            warnings.append(
                f"{rel}:{u['line']} {u['cmd']} 后面跟着日文正文，但不在白名单里，可能漏翻 -> {u['sample']}"
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"_meta": {"files": meta_files}}, ensure_ascii=False) + "\n")
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_chars = sum(len("".join(r["src"])) for r in records)
    log(f"[extract] 写出 {len(records)} 个文本块 / {total_chars} 字符 -> {out_path}")
    if skipped:
        log(f"[extract] 跳过 {len(skipped)} 个文件：")
        for f, why in skipped:
            log(f"          - {f.name}: {why}")
    for w in warnings:
        log(f"[warn] {w}")


# --------------------------------------------------------------------------
# translate
# --------------------------------------------------------------------------


def load_jsonl(path: Path) -> tuple[dict, list[dict]]:
    meta, records = {}, []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "_meta" in obj:
                meta = obj["_meta"]
            else:
                records.append(obj)
    return meta, records


def load_glossary(cfg: dict, root: Path) -> tuple[str, dict]:
    """读取术语表；顺带把标注了「角色」的条目提成说话人译名。"""
    path = Path(cfg["glossary"])
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        return "", {}
    lines, speakers = [], {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
        m = re.match(r"^(.*?)->(.*?)(?:\s*#(.*))?$", s)
        if m:
            src, dst, info = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "")
            if "角色" in info:
                speakers[src] = dst
    speakers.update({k: v for k, v in cfg.get("speakers", {}).items() if v})
    return "\n".join(lines), speakers


def speaker_name(speaker: str, speakers: dict, cfg: dict) -> str:
    """把 @ShowCastMessage 的名字换成说话人译名。

    有的是 "花音B,花音" 这种「立绘差分,角色」两段式（还有 "エステファニア,ニア"），
    取认得出的那一段；都认不出就用最后一段。
    "彩葉＆ひまり子＆スカーレット" 这种多人同时说话，每段各自翻译再拼回去。
    """
    if not speaker:
        return ""
    if not cfg.get("speaker_prefix", True):
        return ""
    if speaker in speakers:
        return speakers[speaker]
    if "," in speaker:
        parts = [s.strip() for s in speaker.split(",")]
        for s in reversed(parts):
            if s in speakers:
                return speakers[s]
        return parts[-1]
    for sep in ("＆", "&"):
        if sep in speaker:
            return sep.join(speakers.get(p.strip(), p.strip()) for p in speaker.split(sep))
    return speaker


def build_user_prompt(cfg: dict, glossary: str, history: str, items: list[tuple[str, str]]) -> str:
    parts = []
    if history.strip():
        parts.append("历史翻译：" + history.strip())
    if cfg.get("glossary_enabled", True) and glossary.strip():
        parts.append("参考以下术语表（可为空，格式为src->dst #备注）：\n" + glossary.strip())
    parts.append("根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文：")
    parts.append("\n".join(f"{name}「{jp}」" if name else jp for name, jp in items))
    return "\n".join(parts)


def post_json(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def render_raw_prompt(system: str, user: str) -> str:
    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


def call_sakura(cfg: dict, user: str, strict_lines: int | None = None) -> str:
    system = cfg["system_prompt"]
    # 强约束必须放进 system：如果追加在用户输入之后，模型会把它当成待翻译的正文
    if strict_lines:
        system = (
            system
            + f"\n本次只输出译文本身，共 {strict_lines} 行，不要合并或拆分行，不要输出任何解释或提示。"
        )
    if cfg.get("mode") == "raw":
        payload = {
            "prompt": render_raw_prompt(system, user),
            "temperature": cfg["temperature"],
            "top_p": cfg["top_p"],
            "n_predict": cfg["max_tokens"],
            "stop": ["<|im_end|>"],
            "stream": False,
        }
        if cfg.get("top_k") is not None:
            payload["top_k"] = cfg["top_k"]
        if cfg.get("repeat_penalty") is not None:
            payload["repeat_penalty"] = cfg["repeat_penalty"]
        resp = post_json(cfg["endpoint_completion"], payload, cfg["timeout"])
        return resp.get("content", "")

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg["temperature"],
        "top_p": cfg["top_p"],
        "max_tokens": cfg["max_tokens"],
        "stream": False,
    }
    for k in ("top_k", "repeat_penalty", "presence_penalty", "frequency_penalty"):
        if cfg.get(k) is not None:
            payload[k] = cfg[k]
    resp = post_json(cfg["endpoint"], payload, cfg["timeout"])
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError(f"接口返回结构异常: {json.dumps(resp, ensure_ascii=False)[:300]}")


def clean_line(line: str, names: set[str], source: str) -> str:
    """去掉模型照着输入格式回显的说话人前缀，以及原文里没有的成对引号。

    输入是 名字「原文」 的形式，模型通常会把 名字「...」 整个复述回来，
    而且用的名字未必是我们给的那个，所以先按已知名字剥一次，
    剩下的交给 unwrap_dialogue_wrapper 按形态兜底。
    """
    s = line.strip()
    for n in sorted(names, key=len, reverse=True):
        if not n:
            continue
        if s.startswith(n + "「"):
            s = s[len(n) + 1 :].strip()
            break
        m = re.match(rf"^{re.escape(n)}\s*[:：]\s*(.+)$", s)
        if m:
            s = m.group(1).strip()
            break
    return unwrap_dialogue_wrapper(s, source)


def unwrap_dialogue_wrapper(text: str, source: str) -> str:
    """原文没有引号时，剥掉模型回显的 名字「...」 外壳。

    输入用的是 名字「原文」 格式，模型经常把外壳整个复述回来，而且它用的名字
    未必是我们给的那一个——实测出现过把 紅葉 写成术语表里的昵称「小红叶」，
    所以这里只按形态判断，不依赖名字白名单。
    """
    if not isinstance(text, str) or not text.strip():
        return text
    if "「" in source or "」" in source:
        return text                      # 原文自带引号，尊重原文，不动
    s = text.strip()

    m = WRAP_RE.match(s)
    if m and m.group(2).strip() and not (set(m.group(1)) & NOT_A_NAME):
        return m.group(2).strip()
    m = OPEN_ONLY_RE.match(s)            # 只开了没闭（被截断的回显）
    if m and m.group(2).strip() and not (set(m.group(1)) & NOT_A_NAME):
        return m.group(2).strip()
    if s.startswith("「") and s.endswith("」") and s.count("「") == s.count("」") == 1:
        return s[1:-1].strip()           # 整句被引号包住，没有名字前缀
    if s.count("」") > s.count("「") and s.endswith("」"):
        fixed = s[: -(s.count("」") - s.count("「"))].strip()
        if fixed:
            return fixed                 # 只剩孤零零的右引号
    return text


def split_in_half(s: str) -> tuple[str, str]:
    """尽量从标点处把一行拆成两行。"""
    mid = len(s) // 2
    best = None
    for i, ch in enumerate(s):
        if ch in "、。，！？…！?：；":
            d = abs(i - mid)
            if best is None or d < best[0]:
                best = (d, i)
    cut = best[1] + 1 if (best and best[0] <= max(4, len(s) // 4)) else mid
    a, b = s[:cut].strip(), s[cut:].strip()
    return (a, b) if a and b else (s, "")


def merge_pair(cn: str, src0: str, src1: str, ratio: float) -> list[tuple[str, int]]:
    """两行被译成一行时怎么塞回去：一般合成一行；明显比原文第一行长就拆成两行。

    游戏的消息框一行也能正常显示（实测过），所以默认接受合并；
    只有在合并后的译文比原文第一行长很多（ratio 倍以上）时才拆回两行，免得撑出框。
    """
    if len(cn) <= max(1, len(src0)) * ratio or len(cn) < 8:
        return [(cn, 2)]
    a, b = split_in_half(cn)
    if a and b:
        return [(a, 1), (b, 1)]
    return [(cn, 2)]


def parse_response(text: str, sources: list[str], names: set[str],
                   allow_merge: bool = False, split_ratio: float = 1.5):
    """把模型输出切成 (译文行, 覆盖几行原文) 的列表；对不上就返回 None 触发重试。

    allow_merge：只在一个批次里只有一个文本块时开——那时"两行被译成一行"能确定是哪两行。
    """
    got = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip().strip("\ufeff")
        if not s or s in ("<|im_end|>", "<|im_start|>"):
            continue
        if ECHO_RE.match(s):
            continue
        got.append(s)
    if len(got) > len(sources):
        # 模型把原文也抄回来了：去掉与原文完全相同的行
        pruned = [g for g in got if g not in (s.strip() for s in sources)]
        if len(pruned) == len(sources):
            got = pruned
    if len(got) == len(sources):
        return [(clean_line(g, names, src), 1) for g, src in zip(got, sources)]
    if allow_merge and len(sources) == 2 and len(got) == 1:
        cn = clean_line(got[0], names, sources[0])
        if cn:
            return merge_pair(cn, sources[0], sources[1], split_ratio)
    return None


def attempt_batch(cfg: dict, batch: list[dict], glossary: str, names: set[str], history: str):
    """按批次翻一轮（含重试）。成功返回 ([(译文行, 覆盖行数)], 尝试次数, "")。

    一个批次只有一个文本块时允许"两行合成一行"（能确定是哪两行），见 parse_response。
    """
    items = [(speaker_name(r["speaker"], cfg["speakers_map"], cfg), r["src"][k])
             for r in batch for k in range(len(r["src"]))]
    user = build_user_prompt(cfg, glossary, history, items)

    flat_sources = [jp for _, jp in items]
    allow_merge = len(batch) == 1 and len(flat_sources) == 2
    ratio = float(cfg.get("merge_split_ratio", 1.5))
    # 我们拼进输入里的说话人前缀，回显时要能原样剥掉（多人同说的名字可能很长）
    prefixes = {nm for nm, _ in items if nm}
    attempts, last_err = 0, ""
    for attempt in range(1, int(cfg["retries"]) + 1):
        attempts = attempt
        try:
            text = call_sakura(cfg, user, strict_lines=len(flat_sources) if attempt > 1 else None)
        except Exception as e:  # 网络 / 服务端错误
            last_err = f"请求失败: {e}"
            time.sleep(min(2 ** attempt, 10))
            continue
        parsed = parse_response(text, flat_sources, names | prefixes,
                                allow_merge=allow_merge, split_ratio=ratio)
        if parsed is None:
            last_err = "行数对不上: " + text.strip().replace("\n", " / ")[:200]
            continue
        return parsed, attempts, ""
    return None, attempts, last_err


def translate_batch(cfg: dict, batch: list[dict], glossary: str, names: set[str], history: str) -> dict:
    """翻译一个批次。整批对不上行数时，退化成逐块单独翻，尽量少丢内容。"""
    zh_flat, attempts, err = attempt_batch(cfg, batch, glossary, names, history)
    out = {"attempts": attempts, "error": "", "results": {}}
    if zh_flat is not None:
        cursor = 0
        for r in batch:
            need, texts, spans = len(r["src"]), [], []
            while need > 0 and cursor < len(zh_flat):
                t, sp = zh_flat[cursor]
                cursor += 1
                need -= sp
                texts.append(t)
                spans.append(sp)
            if need == 0 and texts:
                out["results"][r["id"]] = (texts, spans)
            else:
                out["error"] = "切分译文时对不上"
        return out

    out["error"] = err
    if len(batch) == 1:
        return out
    for r in batch:  # 兜底：逐块重试
        one, a_one, err_one = attempt_batch(cfg, [r], glossary, names, history)
        out["attempts"] += a_one
        if one:
            out["results"][r["id"]] = ([t for t, _ in one], [s for _, s in one])
        else:
            out["error"] = err_one
    return out


def make_batches(records: list[dict], cfg: dict) -> list[list[dict]]:
    batches, cur, cur_lines, cur_chars, cur_file = [], [], 0, 0, None
    for r in records:
        if not r["src"]:
            continue          # 只有 $$ 语音参数行、没有正文的块，没什么可翻
        n = len(r["src"])
        chars = len("".join(r["src"]))
        same_file = r["file"] == cur_file
        if cur and (
            not same_file
            or cur_lines + n > cfg["batch_lines"]
            or cur_chars + chars > cfg["batch_chars"]
        ):
            batches.append(cur)
            cur, cur_lines, cur_chars = [], 0, 0
        cur.append(r)
        cur_lines += n
        cur_chars += chars
        cur_file = r["file"]
    if cur:
        batches.append(cur)
    return batches


def cmd_translate(args, cfg: dict, root: Path) -> None:
    in_path = Path(args.input) if args.input else root / cfg["segments"]
    if not in_path.is_absolute():
        in_path = root / in_path
    out_path = Path(args.output) if args.output else root / cfg["translated"]
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta, records = load_jsonl(in_path)
    if not records:
        raise SystemExit(f"[错误] {in_path} 里没有可翻译记录，先跑 extract")

    # 译文文件自带 _meta，inject 就不必再依赖 segments.jsonl
    need_meta = not (out_path.exists() and out_path.stat().st_size > 0)
    meta_line = json.dumps({"_meta": meta}, ensure_ascii=False) + "\n" if need_meta else ""

    glossary, speakers = load_glossary(cfg, root)
    cfg["speakers_map"] = speakers
    names = set(speakers.values()) | set(speakers)
    if glossary:
        log(f"[translate] 术语表 {len(glossary.splitlines())} 条，说话人译名 {len(speakers)} 个")

    done = {}
    if out_path.exists() and not args.redo:
        _, old = load_jsonl(out_path)
        # dry-run 的结果不能当成"已完成"，否则正式翻译会被整片跳过
        want = "dry-run" if args.dry_run else "ok"
        for r in old:
            if r.get("status") == want:
                done[r["id"]] = r
        if done:
            log(f"[translate] 续跑：已有 {len(done)} 条结果，跳过")

    todo = [r for r in records if r["id"] not in done]
    if args.only:
        todo = [r for r in todo if args.only in r["asset"] or args.only in r["file"]]
    if args.limit:
        kept, counter = [], {}
        for r in todo:
            if counter.get(r["file"], 0) < args.limit:
                kept.append(r)
                counter[r["file"]] = counter.get(r["file"], 0) + 1
        todo = kept
    log(f"[translate] 待翻译 {len(todo)} 块（共 {len(records)} 块），并发 {cfg['workers']}")

    if not todo:
        log("[translate] 没有需要翻译的内容")
        return

    if args.dry_run:
        with out_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(meta_line)
            for r in todo:
                rec = {**r, "zh": list(r["src"]), "status": "dry-run", "attempts": 0, "error": ""}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log("[translate] dry-run 完成（译文 = 原文，仅用于验证流程）")
        return

    if not args.no_health:
        try:
            with urllib.request.urlopen(cfg["health_url"], timeout=10) as resp:
                resp.read()
            log(f"[translate] Sakura 服务在线：{cfg['health_url']}")
        except Exception as e:
            raise SystemExit(
                f"[错误] 连不上 Sakura 服务（{cfg['health_url']}）：{e}\n"
                f"       请先用 Sakura_Launcher_GUI / llama.cpp 启动模型，"
                f"或确认 config.json 里的地址。"
            )

    batches = make_batches(todo, cfg)
    workers = max(1, int(cfg["workers"]))
    history = ""
    ok = bad = 0
    t0 = time.time()
    with out_path.open("a", encoding="utf-8", newline="\n") as fh, cf.ThreadPoolExecutor(workers) as pool:
        fh.write(meta_line)
        for start in range(0, len(batches), workers):
            wave = batches[start : start + workers]
            results = list(pool.map(lambda b: translate_batch(cfg, b, glossary, names, history), wave))
            for batch, res in zip(wave, results):
                tail = ""
                for r in batch:
                    got = res["results"].get(r["id"])
                    texts, spans = got if got else ([], [])
                    rec = {
                        **r,
                        "zh": texts,
                        "status": "ok" if texts else "failed",
                        "attempts": res["attempts"],
                        "error": "" if texts else res["error"],
                    }
                    if texts and any(s != 1 for s in spans):
                        rec["zh_span"] = spans      # 有两行合成一行的情况才写
                    if texts:
                        ok += 1
                        tail = "\n".join(texts)
                    else:
                        bad += 1
                        log(f"[fail] {r['id']} {r['src'][:1]} -> {res['error'][:160]}")
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                if tail:
                    history = tail[-int(cfg["history_chars"]) :]
            done_n = ok + bad
            rate = done_n / max(time.time() - t0, 0.01)
            log(f"[translate] {done_n}/{len(todo)} 块  {rate:.2f} 块/秒  失败 {bad}")
    log(f"[translate] 完成：成功 {ok}，失败 {bad} -> {out_path}")


# --------------------------------------------------------------------------
# inject
# --------------------------------------------------------------------------


def apply_translation(lines: list[str], rec: dict) -> list[str]:
    """把译文写回文本行。

    zh 是译文行列表；zh_span[i] 表示第 i 行译文顶掉了原文几行（默认 1）。
    两行合成一行时 span=2：只写一次，原来两行里的第二行直接不写（游戏里一行也能正常显示）。
    """
    zh = rec.get("zh") or []
    if not zh:
        return lines
    span = [max(1, int(x)) for x in (rec.get("zh_span") or [1] * len(zh))]
    n_text = sum(1 for it in rec["region"] if it["kind"] == "text")
    if sum(span) != n_text:
        if len(zh) == n_text:
            span = [1] * n_text          # 老数据 / 普通情况
        else:
            return lines                 # 对不上就不动，宁可留原文

    owner = []
    for i, n in enumerate(span):
        owner.extend([i] * n)

    new_region, k = [], 0
    for item in rec["region"]:
        if item["kind"] != "text":
            new_region.append(item["text"])
            continue
        idx, k = k, k + 1
        out_i = owner[idx] if idx < len(owner) else None
        if out_i is None or out_i >= len(zh) or not zh[out_i]:
            new_region.append(item["text"])
            continue
        if idx == 0 or owner[idx - 1] != out_i:
            new_region.append(zh[out_i])   # 一组只写一次
    start = rec["cmd_line"] + 1
    lines[start : start + len(rec["region"])] = new_region
    return lines


def cmd_inject(args, cfg: dict, root: Path) -> None:
    in_path = Path(args.input) if args.input else root / cfg["translated"]
    if not in_path.is_absolute():
        in_path = root / in_path
    in_root = root / (args.in_root or cfg["in_root"])
    out_root = root / (args.out_root or cfg["out_root"])

    meta, records = load_jsonl(in_path)
    files_meta = meta.get("files", {})
    if not files_meta:
        # 旧版译文文件没带 _meta 时，退回 segments.jsonl
        seg = root / cfg["segments"]
        if seg.exists() and seg != in_path:
            files_meta = load_jsonl(seg)[0].get("files", {})
    if not records or not files_meta:
        raise SystemExit(f"[错误] {in_path} 缺少内容或 _meta，先跑 extract")

    by_file: dict[str, list[dict]] = {}
    seen_id: dict[str, int] = {}
    for r in records:
        # 同一个块被翻译多次时（dry-run 后又正式翻），以最后一条为准
        if r["id"] in seen_id:
            by_file[r["file"]][seen_id[r["id"]]] = r
        else:
            seen_id[r["id"]] = len(by_file.setdefault(r["file"], []))
            by_file[r["file"]].append(r)

    for rel, recs in by_file.items():
        src_path = root / rel
        if not src_path.exists():
            src_path = in_root / Path(rel).name
        if not src_path.exists():
            log(f"[warn] 找不到源文件 {rel}，跳过")
            continue
        raw_bytes = src_path.read_bytes()
        fm = files_meta.get(rel, {})
        if fm.get("sha1") and sha1_bytes(raw_bytes) != fm["sha1"]:
            log(f"[warn] {rel} 自 extract 后已改动，字体行号可能错位（--force 无视）")
            if not args.force:
                continue

        raw_text, has_bom = read_json_text(src_path)
        style = fm.get("style") or detect_style(raw_text)
        data = json.loads(raw_text)
        text_path = fm.get("text_path") or find_text_path(data)
        lines, sep = split_lines(get_at(data, text_path))
        before = list(lines)

        used = 0
        # 从后往前写：合并会让行数变少，从前往后写会让后面所有块的行号错位
        for r in sorted(recs, key=lambda x: x["cmd_line"], reverse=True):
            if r.get("zh"):
                apply_translation(lines, r)
                used += 1

        set_at(data, text_path, sep.join(lines))
        # 输出路径 = out_root + 源文件相对 in_root 的子路径，避免多套一层目录
        try:
            sub = src_path.relative_to(in_root)
        except ValueError:
            sub = Path(rel).name
        out_path = out_root / sub
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(dump_like(data, style, has_bom))

        # 自检：除译文行以外的每一行都必须与原文逐字相同
        diff_ok = len(lines) == len(before)
        if diff_ok:
            editable = set()
            for r in recs:
                for k, item in enumerate(r["region"]):
                    if item["kind"] == "text":
                        editable.add(r["cmd_line"] + 1 + k)
            diff_ok = all(before[i] == lines[i] for i in range(len(lines)) if i not in editable)
        flag = "OK" if diff_ok else "!! 指令行被改动，请检查"
        log(f"[inject] {rel}: 回填 {used}/{len(recs)} 块 -> {out_path}  [{flag}]")


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def cmd_check(args, cfg: dict, root: Path) -> None:
    in_path = Path(args.input) if args.input else root / cfg["translated"]
    if not in_path.is_absolute():
        in_path = root / in_path
    _, records = load_jsonl(in_path)
    if not records:
        raise SystemExit(f"[错误] {in_path} 没有内容")

    stats: dict[str, dict] = {}
    kana_hits, symbol_hits, wrap_hits = [], [], []
    for r in records:
        st = stats.setdefault(r["file"], {"blocks": 0, "ok": 0, "failed": 0, "chars_src": 0, "chars_zh": 0})
        st["blocks"] += 1
        st["chars_src"] += len("".join(r["src"]))
        status = r.get("status")
        if status == "ok":
            st["ok"] += 1
        else:
            st["failed"] += 1
        zh = r.get("zh") or []
        st["chars_zh"] += len("".join(zh))
        span = [max(1, int(x)) for x in (r.get("zh_span") or [1] * len(zh))]
        if sum(span) != len(r["src"]) or not zh:
            if status == "ok":  # 标了成功但行数对不上，也算失败
                st["failed"] += 1
                st["ok"] -= 1
            kana_hits.append((r["id"], "行数与原文不一致", r["src"]))
            continue
        src_i = 0
        for i, cn in enumerate(zh):
            jp = r["src"][src_i] if src_i < len(r["src"]) else ""
            src_i += max(1, int(span[i]))
            if looks_untranslated(cn):
                kana_hits.append((r["id"], "译文里还有假名", f"{jp} -> {cn}"))
            if unwrap_dialogue_wrapper(cn, jp) != cn:
                wrap_hits.append((r["id"], f"{jp} -> {cn}"))
            for sym in "♪～…！？":
                if jp.count(sym) and not cn.count(sym):
                    symbol_hits.append((r["id"], f"符号 {sym} 丢失", f"{jp} -> {cn}"))
                    break

    def pad(s: str, n: int) -> str:
        import unicodedata

        w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
        return s + " " * max(0, n - w)

    print(pad("文件", 54) + f"{'块':>5}{'完成':>6}{'失败':>6}{'原文字符':>9}{'译文字符':>9}")
    for rel, st in sorted(stats.items()):
        print(
            pad(rel, 54)
            + f"{st['blocks']:>5}{st['ok']:>6}{st['failed']:>6}{st['chars_src']:>9}{st['chars_zh']:>9}"
        )

    print(f"\n译文残留假名：{len(kana_hits)} 处")
    for i in kana_hits[: args.show]:
        print(f"   {i[0]}  {i[1]}\n      {i[2]}")
    print(f"\n符号疑似丢失：{len(symbol_hits)} 处")
    for i in symbol_hits[: args.show]:
        print(f"   {i[0]}  {i[1]}\n      {i[2]}")
    print(f"\n多余人名/引号外壳：{len(wrap_hits)} 处" + ("（跑 fix_dialogue_wrappers.py 可自动清掉）" if wrap_hits else ""))
    for i in wrap_hits[: args.show]:
        print(f"   {i[0]}\n      {i[1]}")
    if (kana_hits or symbol_hits) and args.show:
        print(f"\n（上面只显示前 {args.show} 条，完整数量见统计）")


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="剧本对白抽取 / Sakura 翻译 / 原样回填",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="配置文件路径（默认 Translation/config.json）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="抽出对白")
    p.add_argument("-i", "--input", help="剧本 JSON 文件或目录")
    p.add_argument("-o", "--output", help="输出 jsonl 路径")

    p = sub.add_parser("translate", help="调用 Sakura 翻译")
    p.add_argument("-i", "--input", help="segments.jsonl")
    p.add_argument("-o", "--output", help="translated.jsonl")
    p.add_argument("--dry-run", action="store_true", help="不调模型，仅验证流程")
    p.add_argument("--no-health", action="store_true", help="跳过在线检查")
    p.add_argument("--redo", action="store_true", help="忽略已有结果，全部重译")
    p.add_argument("--only", help="只用 asset/文件名包含该字符串的条目")
    p.add_argument("--limit", type=int, help="每个文件最多翻译 N 块（抽样对比用）")
    p.add_argument("--workers", type=int, help="并发数，覆盖配置")

    p = sub.add_parser("inject", help="把译文写回 JSON")
    p.add_argument("-i", "--input", help="translated.jsonl")
    p.add_argument("--in-root", help="源根目录，默认 Resources")
    p.add_argument("--out-root", help="输出根目录，默认 Translation/Resources")
    p.add_argument("--force", action="store_true", help="源文件已改动时仍然回填")

    p = sub.add_parser("check", help="质检报告")
    p.add_argument("-i", "--input", help="translated.jsonl")
    p.add_argument("--show", type=int, default=20, help="每类问题显示多少条")

    args = parser.parse_args()
    root = find_root()
    cfg = load_config(root, args.config)
    if getattr(args, "workers", None):
        cfg["workers"] = args.workers

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    {"extract": cmd_extract, "translate": cmd_translate, "inject": cmd_inject, "check": cmd_check}[
        args.cmd
    ](args, cfg, root)


if __name__ == "__main__":
    main()
