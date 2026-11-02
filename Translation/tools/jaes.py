#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jaes.py —— 解密 / 还原游戏接口里的 JAES 资源

游戏（Cocos Creator 3.x）有一部分资源和接口响应不是明文，而是用它自带的
EXSerializer 加密过的，开头 4 个字节是 "JAES"，结构是：

    "JAES" | IV(16 字节) | Base64( AES-256-CBC-PKCS7(JSON) )

密钥和 IV 写死在游戏主包 `assets/main/index.<版本>.js` 的 crypto-aes.ts 模块里
（游戏自己在客户端解密，所以密钥必然在包里）：

    key = 95364A843E75F7ED0A9D0A5E0BCBB3343FAF87AF40ABE0C21238D2ACEBA07E33
    iv  = B4461EE05E9A05528783A4BF13E5928D      # 写死的那份；实际用数据里带的那 16 字节

AES 是确定性加密（固定 key + 固定 IV，没有随机盐），所以改完明文再加密回去，
结构上和原文一模一样 —— 油猴脚本就是靠这一点把译文塞回接口响应。

只依赖 Python 标准库。

用法：
    python Translation/tools/jaes.py decrypt <输入> [输出.json]   # 支持原始二进制 / Base64 文本
    python Translation/tools/jaes.py encrypt <输入.json> [输出]   # 直接产出可回填的二进制
    python Translation/tools/jaes.py selftest                    # 自检（AES 官方测试向量）
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 游戏写死的密钥
# --------------------------------------------------------------------------
KEY_HEX = "95364A843E75F7ED0A9D0A5E0BCBB3343FAF87AF40ABE0C21238D2ACEBA07E33"
IV_HEX = "B4461EE05E9A05528783A4BF13E5928D"
TAG = b"JAES"


# --------------------------------------------------------------------------
# AES（纯 Python，仅用标准库）
# --------------------------------------------------------------------------
def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _build_tables():
    inv = [0] * 256
    for i in range(1, 256):
        for j in range(1, 256):
            if _gmul(i, j) == 1:
                inv[i] = j
                break
    sbox = [0] * 256
    for i in range(256):
        x = inv[i]
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        sbox[i] = s ^ 0x63
    rsbox = [0] * 256
    for i, v in enumerate(sbox):
        rsbox[v] = i
    return sbox, rsbox


SBOX, RSBOX = _build_tables()
RCON = [0x01]
for _ in range(13):
    RCON.append(_xtime(RCON[-1]))


class AES256:
    """只实现本游戏用到的 AES-256（14 轮）单块加解密。"""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256 需要 32 字节密钥")
        self.rounds = 14
        self.rk = self._expand(key)

    def _expand(self, key: bytes):
        nk, nr = 8, self.rounds
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        for i in range(nk, 4 * (nr + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]                       # RotWord
                t = [SBOX[b] for b in t]                # SubWord
                t[0] ^= RCON[i // nk - 1]
            elif i % nk == 4:
                t = [SBOX[b] for b in t]                # AES-256 多的那次 SubWord
            w.append([a ^ b for a, b in zip(w[i - nk], t)])
        return w

    def _add_round_key(self, s, rnd):
        for c in range(4):
            word = self.rk[rnd * 4 + c]
            for r in range(4):
                s[4 * c + r] ^= word[r]

    @staticmethod
    def _shift_rows(s, inv=False):
        for r in range(1, 4):
            row = [s[4 * c + r] for c in range(4)]
            row = row[-r:] + row[:-r] if inv else row[r:] + row[:r]
            for c in range(4):
                s[4 * c + r] = row[c]

    @staticmethod
    def _mix_columns(s, inv=False):
        for c in range(4):
            a = s[4 * c:4 * c + 4]
            if inv:
                s[4 * c + 0] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
                s[4 * c + 1] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
                s[4 * c + 2] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
                s[4 * c + 3] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)
            else:
                s[4 * c + 0] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
                s[4 * c + 1] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
                s[4 * c + 2] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
                s[4 * c + 3] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)

    def encrypt_block(self, block: bytes) -> bytes:
        s = list(block)
        self._add_round_key(s, 0)
        for rnd in range(1, self.rounds):
            s = [SBOX[b] for b in s]
            self._shift_rows(s)
            self._mix_columns(s)
            self._add_round_key(s, rnd)
        s = [SBOX[b] for b in s]
        self._shift_rows(s)
        self._add_round_key(s, self.rounds)
        return bytes(s)

    def decrypt_block(self, block: bytes) -> bytes:
        s = list(block)
        self._add_round_key(s, self.rounds)
        for rnd in range(self.rounds - 1, 0, -1):
            self._shift_rows(s, inv=True)
            s = [RSBOX[b] for b in s]
            self._add_round_key(s, rnd)
            self._mix_columns(s, inv=True)
        self._shift_rows(s, inv=True)
        s = [RSBOX[b] for b in s]
        self._add_round_key(s, 0)
        return bytes(s)


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _cbc_encrypt(aes: AES256, iv: bytes, data: bytes) -> bytes:
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        prev = aes.encrypt_block(_xor(data[i:i + 16], prev))
        out += prev
    return bytes(out)


def _cbc_decrypt(aes: AES256, iv: bytes, data: bytes) -> bytes:
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += _xor(aes.decrypt_block(block), prev)
        prev = block
    return bytes(out)


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    n = data[-1] if data else 0
    if not 1 <= n <= 16:
        raise ValueError("PKCS7 填充不对，密钥或数据有问题")
    return data[:-n]


# --------------------------------------------------------------------------
# JAES 容器
# --------------------------------------------------------------------------
def looks_like_jaes(data: bytes) -> bool:
    return len(data) > 20 and data[:4] == TAG


def unwrap(raw: bytes) -> bytes:
    """把「原始二进制」或「整段 Base64 文本」都归一成 JAES 原始字节。"""
    if looks_like_jaes(raw):
        return raw
    text = raw.strip()
    if text[:4] == TAG:                                  # Base64 里不会出现 JAES 字面量
        return text
    try:
        decoded = base64.b64decode(text, validate=False)
    except Exception as exc:                             # pragma: no cover
        raise ValueError("既不是 JAES 二进制也不是 Base64 文本") from exc
    if not looks_like_jaes(decoded):
        raise ValueError("解出来的数据没有 JAES 头")
    return decoded


def decrypt(raw: bytes, key_hex: str = KEY_HEX) -> dict:
    """JAES 字节 -> 明文 dict"""
    buf = unwrap(raw)
    iv = buf[4:20]
    body = buf[20:]
    ct = base64.b64decode(body)
    if len(ct) % 16:
        raise ValueError("密文长度不是 16 的倍数")
    aes = AES256(bytes.fromhex(key_hex))
    text = _unpad(_cbc_decrypt(aes, iv, ct)).decode("utf-8")
    return json.loads(text)


def encrypt(obj, key_hex: str = KEY_HEX, iv_hex: str = IV_HEX) -> bytes:
    """明文 dict -> JAES 字节（可以直接当响应体回给游戏）"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # 游戏用的是 JSON.stringify，键顺序一致才能和原文逐字节一致；这里的 compact 写法等价
    return encrypt_raw(text, key_hex, iv_hex)


def encrypt_raw(text: bytes, key_hex: str = KEY_HEX, iv_hex: str = IV_HEX) -> bytes:
    iv = bytes.fromhex(iv_hex)
    aes = AES256(bytes.fromhex(key_hex))
    ct = _cbc_encrypt(aes, iv, _pad(text))
    return TAG + iv + base64.b64encode(ct)


# --------------------------------------------------------------------------
# 自检 / 命令行
# --------------------------------------------------------------------------
def selftest() -> int:
    # FIPS-197 附录 C.3 的 AES-256 测试向量
    key = bytes(range(32))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    want = "8ea2b7ca516745bfeafc49904b496089"
    aes = AES256(key)
    got = aes.encrypt_block(pt).hex()
    back = aes.decrypt_block(bytes.fromhex(want)).hex()
    print("AES-256 加密向量:", "OK" if got == want else f"NG {got}")
    print("AES-256 解密向量:", "OK" if back == pt.hex() else f"NG {back}")
    # 容器往返
    obj = {"scenario_list": [{"card_scenario_id": 1, "scenario_title": "テスト「あ」"}]}
    return 0 if got == want and back == pt.hex() and decrypt(encrypt(obj)) == obj else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JAES（AES-256-CBC）资源解密 / 还原")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("decrypt", help="解开成明文 JSON")
    d.add_argument("src")
    d.add_argument("dst", nargs="?")

    e = sub.add_parser("encrypt", help="把明文 JSON 打回 JAES 字节")
    e.add_argument("src")
    e.add_argument("dst", nargs="?")

    sub.add_parser("selftest", help="跑一遍 AES 测试向量")

    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    src = Path(args.src)
    raw = src.read_bytes()

    if args.cmd == "decrypt":
        obj = decrypt(raw)
        dst = Path(args.dst) if args.dst else src.with_suffix(src.suffix + ".json")
        dst.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{src} -> {dst}")
        return 0

    size = src.stat().st_size
    blob = encrypt(json.loads(src.read_text(encoding="utf-8")))
    dst = Path(args.dst) if args.dst else src.with_suffix(".bin")
    dst.write_bytes(blob)
    print(f"{src} ({size} 字节) -> {dst} ({len(blob)} 字节)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
