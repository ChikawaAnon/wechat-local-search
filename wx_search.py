# -*- coding: utf-8 -*-
"""
微信聊天记录检索导出工具（基于本机解密后的微信 4.x 数据库）

用法:
  python wx_search.py build                  # 构建/重建检索缓存（数据更新后重跑）
  python wx_search.py images                 # 解密图片消息对应的 .dat 到 data/img_decoded
  python wx_search.py ocr [N]                # OCR 识别图片文字入库（增量, N=并发, 默认6）
  python wx_search.py search                 # 交互式搜索
  python wx_search.py search -k 校招 -c 群A 群B -s 2026-07-01 -l 500
搜索增强:
  - 同义词扩展: synonyms.txt (一行一组, 空格分隔), --no-expand 关闭
  - DeepSeek 语义扩展: 项目根目录放 deepseek_key.txt 后, search 加 --llm 或交互中选择
  - 图片文字(OCR)一并参与关键词命中, Word 导出自动嵌入命中图片
"""
import argparse
import glob
import hashlib
import hmac as hmac_mod
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

import zstandard

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output")
SYN_FILE = os.path.join(BASE, "synonyms.txt")
DS_KEY_FILE = os.path.join(BASE, "deepseek_key.txt")
ZP_KEY_FILE = os.path.join(BASE, "zhipu_key.txt")
ROOT_DATA = os.path.join(BASE, "data")
CFG_FILE = os.path.join(BASE, "config.json")


def _discover_accounts(wechat_root=None):
    """扫描 xwechat_files 下含 db_storage 的账号目录"""
    import glob as _g
    if not wechat_root:
        cands = _g.glob(os.path.join(os.path.expanduser("~"), "xwechat_files")) + \
                _g.glob("C:\\xwechat_files") + _g.glob("D:\\xwechat_files")
        if not cands:
            return {}
        wechat_root = cands[0]
    accs = {}
    for d in _g.glob(os.path.join(wechat_root, "wxid_*")):
        if os.path.isdir(os.path.join(d, "db_storage")):
            accs[os.path.basename(d)] = {"db_dir": os.path.join(d, "db_storage")}
    return accs


def _load_config():
    """加载 config.json; 首次运行时从旧版布局迁移到多账号布局"""
    cfg = {"active": "", "accounts": {}, "wechat_root": ""}
    if os.path.exists(CFG_FILE):
        try:
            cfg.update(json.load(open(CFG_FILE, encoding="utf-8")))
        except Exception:
            pass
    if not cfg["accounts"]:
        discovered = _discover_accounts(cfg.get("wechat_root"))
        # 旧版: tools/wechat-decrypt/config.json 里的 db_dir 就是当前账号
        old_db_dir = ""
        old_cfg = os.path.join(BASE, "tools", "wechat-decrypt", "config.json")
        if os.path.exists(old_cfg):
            try:
                old_db_dir = json.load(open(old_cfg, encoding="utf-8"))["db_dir"]
            except Exception:
                pass
        if old_db_dir and os.path.basename(os.path.dirname(old_db_dir)) in discovered:
            active = os.path.basename(os.path.dirname(old_db_dir))
        elif discovered:
            active = sorted(discovered)[0]
        else:
            active = ""
        for name, info in discovered.items():
            info["self"] = re.match(r"^(wxid_.+?)_[0-9a-f]{4}$", name).group(1) \
                if re.match(r"^(wxid_.+?)_[0-9a-f]{4}$", name) else name
            info["label"] = "主号" if name == active else ""
            cfg["accounts"][name] = info
        cfg["active"] = active
        cfg["wechat_root"] = os.path.dirname(old_db_dir) if old_db_dir else ""
        _save_config(cfg)
        _migrate_v3(active)
    return cfg


def _save_config(cfg):
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _migrate_v3(active):
    """旧版数据布局 -> data/accounts/<active>/"""
    acc = os.path.join(ROOT_DATA, "accounts", active)
    moves = [
        (os.path.join(ROOT_DATA, "decrypted"), os.path.join(acc, "decrypted")),
        (os.path.join(ROOT_DATA, "search_cache.db"), os.path.join(acc, "search_cache.db")),
        (os.path.join(ROOT_DATA, "ocr.db"), os.path.join(acc, "ocr.db")),
        (os.path.join(ROOT_DATA, "img_decoded"), os.path.join(acc, "img_decoded")),
        (os.path.join(ROOT_DATA, "img_thumb"), os.path.join(acc, "img_thumb")),
        (os.path.join(ROOT_DATA, "vectors"), os.path.join(acc, "vectors")),
        (os.path.join(ROOT_DATA, "image_keys.json"), os.path.join(acc, "keys", "image_keys.json")),
    ]
    for src, dst in moves:
        try:
            if os.path.exists(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
                print(f"[migrate] {os.path.basename(src)} -> accounts/{active}/")
        except OSError as e:
            print(f"[migrate] 跳过 {os.path.basename(src)} (被占用, 稍后重跑会继续): {e}")
    # 数据库密钥从工具目录复制一份进账号 keys（原件保留, 手动 decrypt_db.py 仍可用）
    try:
        os.makedirs(os.path.join(acc, "keys"), exist_ok=True)
        for name in ("all_keys.json", "key_password.txt"):
            src = os.path.join(BASE, "tools", "wechat-decrypt", name)
            dst = os.path.join(acc, "keys", name)
            if os.path.exists(src) and not os.path.exists(dst):
                with open(src, "rb") as a, open(dst, "wb") as b:
                    b.write(a.read())
    except OSError:
        pass


_CFG = _load_config()
if not _CFG.get("active") or _CFG["active"] not in _CFG["accounts"]:
    print("[!] config.json 中没有可用账号; 先运行: python wx_search.py use")
    sys.exit(1)
_ACC = _CFG["active"]
_ACC_INFO = _CFG["accounts"][_ACC]
ACC_DIR = os.path.join(ROOT_DATA, "accounts", _ACC)
DECRYPTED = os.path.join(ACC_DIR, "decrypted")
CACHE_DB = os.path.join(ACC_DIR, "search_cache.db")
OCR_DB = os.path.join(ACC_DIR, "ocr.db")
VOICE_DB = os.path.join(ACC_DIR, "voice.db")
IMG_OUT = os.path.join(ACC_DIR, "img_decoded")
IMG_THUMB = os.path.join(ACC_DIR, "img_thumb")
KEYS_DIR = os.path.join(ACC_DIR, "keys")
SYNC_STATE = os.path.join(ACC_DIR, "sync_state.json")
VEC_DIR = os.path.join(ACC_DIR, "vectors")
VEC_FILE = os.path.join(VEC_DIR, "vectors.f16")
VEC_META = os.path.join(VEC_DIR, "meta.json")
VEC_DB = os.path.join(VEC_DIR, "vectors.db")
DB_DIR = _ACC_INFO["db_dir"]
WX_ROOT = _CFG.get("wechat_root") or os.path.dirname(DB_DIR.rstrip("\\/"))
ATTACH_DIR = os.path.join(WX_ROOT, "msg", "attach")
MSG_DBS = ["message\\message_0.db", "message\\message_1.db", "message\\message_2.db"]
SELF = _ACC_INFO.get("self", _ACC)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_dctx = zstandard.ZstdDecompressor()

TYPE_LABEL = {3: "[图片]", 34: "[语音]", 35: "[语音]", 42: "[名片]", 43: "[视频]",
              47: "[表情]", 48: "[位置]", 51: "", 10000: "", 10002: ""}
FWD_ITEM_LABEL = {2: "[图片]", 3: "[视频]", 4: "[链接]", 5: "[文件]", 6: "[位置]", 7: "[小程序]"}
DEFAULT_SYNONYMS = """校招 秋招 春招 校园招聘 招聘 内推 提前批 网申 投递 简历 offer 录用 三方 签约 笔试 面试 实习 暑期实习
快递 取件 包裹 快递站 取件码
开会 例会 会议 答辩 汇报"""


def decode_content(raw):
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    try:
        if bytes(raw[:4]) == ZSTD_MAGIC:
            raw = _dctx.decompressobj().decompress(bytes(raw))
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def xml_title(xml):
    m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml, re.S)
    return m.group(1).strip() if m else ""


def xml_url(xml):
    m = re.search(r"<url>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</url>", xml, re.S)
    if not m:
        return ""
    import html
    u = html.unescape(m.group(1).strip())
    return u if u.startswith("http") else ""


def appmsg_classify(xml, title):
    """appmsg 分类 -> (内容前缀标签, url)"""
    url = xml_url(xml)
    am_type = re.search(r"<type>(\d+)</type>", xml)
    if am_type and am_type.group(1) == "6" and title and title.lower() != "null":
        return "[文件]", ""
    if "mp.weixin.qq.com" in url:
        return "[公众号]", url
    if url:
        return "[链接]", url
    return "", ""


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def parse_recordinfo(xml):
    """合并转发消息 -> (展开子消息文本, 汇总链接)"""
    m = re.search(r"<recorditem><!\[CDATA\[(.*?)\]\]></recorditem>", xml, re.S)
    if not m:
        return "", ""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(m.group(1).strip())
    except ET.ParseError:
        return "", ""
    items = root.findall(".//dataitem")
    lines, urls = [], []
    for it in items:
        dtype = it.get("datatype", "")
        name = (it.findtext("sourcename") or "").strip()
        t = (it.findtext("sourcetime") or "").strip()
        body = (it.findtext("datadesc") or "").strip() or (it.findtext("datatitle") or "").strip()
        if not body:
            body = FWD_ITEM_LABEL.get(int(dtype) if str(dtype).isdigit() else 0, "[消息]")
        body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
        u = (it.findtext("dataurl") or "").strip()
        if u.startswith("http"):
            urls.append(u)
            body = f"{body}\n链接: {u}"
        lines.append(f"{name} ({t}): {body}" if name else body)
    if not lines:
        return "", ""
    return f"[聊天记录 {len(lines)}条] " + "\n".join(lines), " ".join(urls[:3])


def parse_appmsg(xml, mtype):
    """appmsg XML -> (content, url)"""
    title = xml_title(xml)
    tag, url = appmsg_classify(xml, title)
    # 引用消息补充被引用内容
    ref = re.search(r"<refermsg>.*?<displayname>(.*?)</displayname>.*?<content>(.*?)</content>",
                    xml, re.S)
    if ref:
        quoted = strip_tags(ref.group(2))[:200]
        title = f"{title} [引用 {ref.group(1)}: {quoted}]" if title else f"[引用 {ref.group(1)}: {quoted}]"
    if not title:
        return (TYPE_LABEL.get(mtype, "[消息]"), "")
    return ((tag + title) if tag else title, url)


def img_md5(xml):
    m = re.search(r'md5="([0-9a-f]{32})"', xml)
    return m.group(1) if m else ""


def hashlib_md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 缓存构建

def build_cache():
    os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)
    if os.path.exists(CACHE_DB):
        os.remove(CACHE_DB)
    out = sqlite3.connect(CACHE_DB)
    out.execute("CREATE TABLE msgs(talker TEXT, sender TEXT, ts INT, mtype INT, content TEXT, ref TEXT, url TEXT, svr_id INTEGER)")
    t0 = time.time()
    total = img_with_ref = fwd = 0
    for rel in MSG_DBS:
        path = os.path.join(DECRYPTED, rel)
        if not os.path.exists(path):
            continue
        con = sqlite3.connect(path)
        id2user = {r[0]: r[1] for r in con.execute("SELECT rowid, user_name FROM Name2Id")}
        md5map = {hashlib_md5(u): u for u in id2user.values() if u}
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        cols = {d[1] for d in con.execute(f"PRAGMA table_info('{tables[0]}')")} if tables else set()
        has_cc = "compress_content" in cols
        has_pk = "packed_info_data" in cols
        batch = []
        for t in tables:
            talker = md5map.get(t[4:])
            if not talker:
                continue
            sel_cols = ["real_sender_id", "create_time", "local_type", "message_content", "server_id"]
            if has_cc:
                sel_cols.append("compress_content")
            if has_pk:
                sel_cols.append("packed_info_data")
            for row in con.execute(f'SELECT {", ".join(sel_cols)} FROM "{t}"'):
                sid, ts, mtype, mc, svr_id = row[0], row[1], row[2], row[3], row[4]
                cc = row[5] if has_cc else None
                pk = row[6] if (has_cc and has_pk) else (row[5] if has_pk else None)
                sender_user = id2user.get(sid, "")
                content = decode_content(mc)
                if not content and cc is not None:
                    content = decode_content(cc)
                ref = ""
                url = ""
                s = content.strip()
                # 群聊: 内容前缀 "发送者id:\n" 是真实发送者（旧库 real_sender_id 语义不可靠），剥离并纠正归因
                if "chatroom" in talker:
                    mp = re.match(r"^([A-Za-z][0-9A-Za-z_\-.]{4,}):\r?\n", s)
                    if mp:
                        sender_user = mp.group(1)
                        s = s[mp.end():]
                content = s
                btype = mtype & 0xFFFFFFFF
                if btype == 3:
                    # 图片: md5 优先取 packed_info protobuf (\x22\x20 + 32位hex), 回退 XML
                    if pk:
                        m = re.search(rb"\x22\x20([0-9a-f]{32})", bytes(pk))
                        if m:
                            ref = m.group(1).decode()
                    if not ref:
                        ref = img_md5(s)
                    if ref:
                        img_with_ref += 1
                    content = "[图片]"
                elif s.startswith(("<?xml", "<msg")) or "<appmsg" in s[:200]:
                    if "<type>19</type>" in s:
                        expanded, furl = parse_recordinfo(s)
                        if expanded:
                            content, ref, fwd = expanded, "", fwd + 1
                            if furl:
                                url = furl
                        else:
                            content = xml_title(s) or "[聊天记录]"
                    elif re.search(r"<img[\s>]", s[:500]) and 'md5="' in s:
                        ref = img_md5(s)
                        content = TYPE_LABEL.get(3, "[图片]")
                        if ref:
                            img_with_ref += 1
                    else:
                        content, url = parse_appmsg(s, btype)
                if not content:
                    continue
                batch.append((talker, sender_user, int(ts or 0), int(mtype), content, ref, url,
                              int(svr_id) if svr_id else 0))
                if len(batch) >= 20000:
                    out.executemany("INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?)", batch)
                    total += len(batch); batch.clear()
        if batch:
            out.executemany("INSERT INTO msgs VALUES(?,?,?,?,?,?,?,?)", batch)
            total += len(batch); batch.clear()
        con.close()
        print(f"[build] {rel} 完成, 累计 {total} 条, {time.time()-t0:.0f}s")
    out.execute("CREATE INDEX idx_msgs_ts ON msgs(ts)")
    out.execute("CREATE INDEX idx_msgs_talker ON msgs(talker)")
    out.execute("CREATE INDEX idx_msgs_ref ON msgs(ref)")
    out.commit(); out.close()
    print(f"[build] 缓存完成: {total} 条, 合并转发 {fwd} 条, 图片含md5 {img_with_ref} 张, {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- 联系人与显示名

def _parse_room_ext(buf):
    """chat_room.ext_buffer (protobuf) -> [(成员wxid, 群昵称), ...]"""
    out = []
    if not buf:
        return out
    data = bytes(buf)

    def rvarint(d, i):
        r = sh = 0
        while i < len(d):
            b = d[i]; i += 1
            r |= (b & 0x7F) << sh; sh += 7
            if not (b & 0x80):
                break
        return r, i

    i = 0
    while i < len(data):
        tag, i = rvarint(data, i)
        field, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = rvarint(data, i)
            val = data[i:i + ln]; i += ln
            if field == 1:
                j = user = 0
                user = nick = ""
                while j < len(val):
                    t2, j = rvarint(val, j)
                    f2, w2 = t2 >> 3, t2 & 7
                    if w2 == 2:
                        l2, j = rvarint(val, j)
                        v2 = val[j:j + l2]; j += l2
                        if f2 == 1:
                            user = v2.decode("utf-8", "replace")
                        elif f2 == 2:
                            nick = v2.decode("utf-8", "replace")
                    elif w2 == 0:
                        _, j = rvarint(val, j)
                    else:
                        break
                if user:
                    out.append((user, nick))
        elif wt == 0:
            _, i = rvarint(data, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return out


class Contacts:
    def __init__(self):
        path = os.path.join(DECRYPTED, "contact", "contact.db")
        con = sqlite3.connect(path)
        self.info = {u: (rk or "", nk or "", al or "") for u, rk, nk, al in con.execute(
            "SELECT username, remark, nick_name, alias FROM contact")}
        self.room_nick = {}
        try:
            for room, buf in con.execute("SELECT username, ext_buffer FROM chat_room"):
                members = _parse_room_ext(buf)
                if members:
                    self.room_nick[room] = {u: n for u, n in members if n}
        except sqlite3.OperationalError:
            pass
        con.close()

    def display(self, username):
        if not username:
            return "系统"
        if username == SELF:
            return "我"
        rk, nk, al = self.info.get(username, ("", "", ""))
        return rk or nk or al or username

    def display_in(self, room, username):
        """群内显示: 群昵称优先于备注/昵称"""
        if username == SELF:
            return "我"
        nick = self.room_nick.get(room, {}).get(username)
        return nick or self.display(username)

    def match(self, keyword):
        kw = keyword.strip().lower()
        hits = []
        for u, (rk, nk, al) in self.info.items():
            if kw in u.lower() or kw in rk.lower() or kw in nk.lower() or kw in al.lower():
                hits.append(u)
        return sorted(set(hits))

    def match_room_member(self, keyword):
        """按群昵称模糊匹配, 返回 [(room, member_username), ...]"""
        kw = keyword.strip().lower()
        pairs = []
        for room, members in self.room_nick.items():
            for u, n in members.items():
                if kw in n.lower() or kw in u.lower():
                    pairs.append((room, u))
        return pairs


# ---------------------------------------------------------------- 智谱 / LLM 扩展

def _read_key(path):
    return open(path, encoding="utf-8").read().strip() if os.path.exists(path) else ""


def _zhipu_post(endpoint, payload, timeout=60):
    key = _read_key(ZP_KEY_FILE)
    if not key:
        raise RuntimeError("zhipu_key.txt 不存在")
    req = urllib.request.Request(
        f"https://open.bigmodel.cn/api/paas/v4/{endpoint}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def zhipu_chat(messages, model="glm-4-flash", timeout=40):
    d = _zhipu_post("chat/completions", {"model": model, "messages": messages,
                                         "temperature": 0.3}, timeout)
    return d["choices"][0]["message"]["content"]


def zhipu_embed(texts, dim=512, retries=8):
    """批量 embedding, 返回 (vecs, total_tokens); 429 视为限流无限等待, 其他错误有限重试"""
    attempt = 0
    while True:
        try:
            d = _zhipu_post("embeddings", {"model": "embedding-3", "input": texts,
                                           "dimensions": dim}, timeout=90)
            vecs = [None] * len(texts)
            for item in d["data"]:
                vecs[item["index"]] = item["embedding"]
            if any(v is None for v in vecs):
                raise RuntimeError("embedding 返回缺失")
            return vecs, d.get("usage", {}).get("total_tokens", 0)
        except Exception as e:
            if "429" in str(e) or "Too Many" in str(e):
                time.sleep(min(60 * (attempt + 1), 120))
                attempt += 1
                continue
            attempt += 1
            if attempt >= retries:
                raise
            time.sleep(2 * attempt)


def load_synonyms():
    groups = []
    text = open(SYN_FILE, encoding="utf-8").read() if os.path.exists(SYN_FILE) else DEFAULT_SYNONYMS
    for line in text.splitlines():
        ws = line.split()
        if len(ws) >= 2:
            groups.append(ws)
    return groups


def expand_keywords(keywords):
    """返回 groups: [[原词, 同义词...], ...] 每组 OR, 组间 AND"""
    syn = load_synonyms()
    groups = []
    for kw in keywords:
        g = [kw]
        low = kw.lower()
        for grp in syn:
            if low in [w.lower() for w in grp]:
                g = [w for w in grp]
                break
        groups.append(g)
    return groups


def llm_expand(keywords):
    """LLM 查询扩展: 优先智谱 glm-4-flash, 回退 DeepSeek; 无密钥/失败返回 []"""
    prompt = ("我在微信聊天记录里搜索关键词。请为这些词扩充语义相关的搜索词"
              "（同义词、近义表达、常见变体，中文为主），不要解释。"
              "输出格式: 词1,词2,词3。关键词: " + ",".join(keywords))
    if _read_key(ZP_KEY_FILE):
        try:
            text = zhipu_chat([{"role": "user", "content": prompt}])
            return [w for w in re.split(r"[,，、\s]+", text.strip()) if w][:20]
        except Exception as e:
            print(f"[!] 智谱扩展失败: {e}")
    if _read_key(DS_KEY_FILE):
        key = _read_key(DS_KEY_FILE)
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 300,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)
            text = data["choices"][0]["message"]["content"]
            return [w for w in re.split(r"[,，、\s]+", text.strip()) if w][:20]
        except Exception as e:
            print(f"[!] DeepSeek 扩展失败: {e}")
    return []


# ---------------------------------------------------------------- 搜索

def parse_date(s, end=False):
    s = s.strip()
    if not s:
        return None
    d = datetime.strptime(s, "%Y-%m-%d")
    return int(d.replace(hour=23, minute=59, second=59).timestamp()) if end else int(d.timestamp())


def _attach_side_dbs(con):
    """挂载 OCR/语音转写库, 返回 (has_ocr, has_voice)"""
    has_ocr = has_voice = False
    if os.path.exists(OCR_DB):
        try:
            oc = sqlite3.connect(OCR_DB)
            has_ocr = oc.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                 "AND name='ocr_text'").fetchone()[0] > 0
            oc.close()
        except Exception:
            has_ocr = False
    if os.path.exists(VOICE_DB):
        try:
            vc = sqlite3.connect(VOICE_DB)
            has_voice = vc.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                   "AND name='voice_text'").fetchone()[0] > 0
            vc.close()
        except Exception:
            has_voice = False
    if has_ocr:
        con.execute("ATTACH DATABASE ? AS odb", (OCR_DB,))
    if has_voice:
        con.execute("ATTACH DATABASE ? AS vdb", (VOICE_DB,))
    return has_ocr, has_voice


def _combine_extra(ocr_text, voice_text):
    parts = []
    if ocr_text:
        parts.append("[图片文字] " + ocr_text)
    if voice_text:
        parts.append("[语音转写] " + voice_text)
    return "\n".join(parts)


def search(keyword="", contact="", sender="", start="", end="", limit=500,
           expand=True, use_llm=False, with_ocr=True, any_kw=False):
    con = sqlite3.connect(CACHE_DB)
    has_ocr, has_voice = _attach_side_dbs(con)
    with_ocr = with_ocr and has_ocr
    ocr_sel = "IFNULL(o.text,'')" if with_ocr else "''"
    voice_sel = "IFNULL(v.text,'')" if has_voice else "''"
    where, params = ["1=1"], []
    ct = None
    if contact:
        ct = Contacts()
        users, missed = set(), []
        for token in contact.split():
            hits = ct.match(token)
            if hits:
                users.update(hits)
            else:
                missed.append(token)
        if missed:
            print(f"[!] 这些聊天对象没有匹配到，已忽略: {'、'.join(missed)}")
        if not users:
            print("[!] 没有找到任何匹配的聊天对象")
            return []
        where.append(f"m.talker IN ({','.join('?' * len(users))})")
        params += sorted(users)
    if sender:
        ct = Contacts()
        users, missed, pairs = set(), [], []
        for token in sender.split():
            hits = ct.match(token)
            if hits:
                users.update(hits)
                continue
            gp = ct.match_room_member(token)
            if gp:
                for room, u in gp:
                    pairs.append((room, u))
            else:
                missed.append(token)
        if missed:
            print(f"[!] 这些发送者没有匹配到，已忽略: {'、'.join(missed)}")
        if not users and not pairs:
            print("[!] 没有找到任何匹配的发送者")
            return []
        conds = []
        if users:
            conds.append(f"m.sender IN ({','.join('?' * len(users))})")
            params += sorted(users)
        if pairs:
            marks = ",".join("?" * len(pairs))
            conds.append(f"(m.talker || '/' || m.sender) IN ({marks})")
            params += [f"{r}/{u}" for r, u in pairs]
        where.append("(" + " OR ".join(conds) + ")")

    kws = [k for k in (keyword or "").split() if k]
    if kws and use_llm:
        extra = llm_expand(kws)
        if extra:
            print(f"[llm] LLM 扩展: {'、'.join(extra)}")
            kws += [w for w in extra if w not in kws]
    if kws:
        groups = expand_keywords(kws) if expand else [[k] for k in kws]
        for g in groups:
            if expand and len(g) > 1:
                print(f"[syn] {g[0]} → {'、'.join(g)}")
        # 每组内 OR（原词+同义词）；组间默认 AND，--any 时改为 OR
        group_sql = []
        for g in groups:
            conds = []
            for w in g:
                cond = "m.content LIKE ?"
                params.append(f"%{w}%")
                if with_ocr:
                    cond += " OR IFNULL(o.text,'') LIKE ?"
                    params.append(f"%{w}%")
                if has_voice:
                    cond += " OR IFNULL(v.text,'') LIKE ?"
                    params.append(f"%{w}%")
                conds.append("(" + cond + ")")
            group_sql.append("(" + " OR ".join(conds) + ")")
        joiner = " OR " if any_kw else " AND "
        where.append("(" + joiner.join(group_sql) + ")")
    ts0, ts1 = parse_date(start), parse_date(end, end=True)
    if ts0 is not None:
        where.append("m.ts >= ?"); params.append(ts0)
    if ts1 is not None:
        where.append("m.ts <= ?"); params.append(ts1)
    join = "LEFT JOIN odb.ocr_text o ON o.ref = m.ref AND m.ref != ''" if with_ocr else ""
    if has_voice:
        join += " LEFT JOIN vdb.voice_text v ON v.svr_id = m.svr_id AND m.svr_id != 0"
    sql = (f"SELECT m.talker, m.sender, m.ts, m.mtype, m.content, m.ref, {ocr_sel}, m.url, {voice_sel} "
           f"FROM msgs m {join} WHERE {' AND '.join(where)} ORDER BY m.ts ASC LIMIT {int(limit)}")
    raw = con.execute(sql, params).fetchall()
    con.close()
    return [(t, s, ts, mt, c, ref, _combine_extra(o, v), u)
            for t, s, ts, mt, c, ref, o, u, v in raw]


def format_rows(rows, ct):
    """-> [(标题, [(时间戳, 行文本, ref, ocr_text, url)...]), ...]"""
    groups = {}
    for talker, sender, ts, mtype, content, ref, ocr_text, url in rows:
        label = ct.display(talker) + ("（群聊）" if "chatroom" in talker else "")
        who = ct.display_in(talker, sender) if sender else "系统"
        line = f"[{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}] {who}: {content}"
        groups.setdefault(label, []).append((ts, line, ref, ocr_text or "", url or "", content))
    return sorted(groups.items(), key=lambda kv: kv[1][0][0])


def find_local_file(title, ts):
    """[文件] 消息 -> 本地已下载文件路径（msg\\file\\年-月\\）"""
    name = title.strip()
    if name.startswith("[文件]"):
        name = name[len("[文件]"):].strip()
    if not name or "/" in name or "\\" in name:
        return ""
    month = datetime.fromtimestamp(ts).strftime("%Y-%m")
    base = os.path.join(WX_ROOT, "msg", "file", month)
    if not os.path.isdir(base):
        return ""
    for n in os.listdir(base):
        if n == name or name in n:
            return os.path.join(base, n)
    return ""


# ---------------------------------------------------------------- 图片解密

def load_image_keys():
    """优先取内存, 失败用备份文件; 返回 (aes_key_str, xor_key_int)"""
    key_file = os.path.join(KEYS_DIR, "image_keys.json")
    if os.path.exists(key_file):
        d = json.load(open(key_file))
        return d["aesKey"], d["xorKey"]
    try:
        sys.path.insert(0, os.path.join(BASE, "tools", "wechat-decrypt"))
        import wx_key
        res = json.loads(wx_key.get_image_key())
        acc = res["accounts"][0]["keys"][0]
        json.dump(acc, open(key_file, "w"))
        print(f"[keys] 图片密钥已获取并备份: aes={acc['aesKey'][:8]}... xor={acc['xorKey']}")
        return acc["aesKey"], acc["xorKey"]
    except Exception as e:
        print(f"[!] 获取图片密钥失败: {e}")
        sys.exit(1)


V2_MAGIC_FULL = b"\x07\x08V2\x08\x07"
V1_MAGIC_FULL = b"\x07\x08V1\x08\x07"
IMAGE_MAGIC = {"png": [0x89, 0x50, 0x4E, 0x47], "gif": [0x47, 0x49, 0x46, 0x38],
               "tif": [0x49, 0x49, 0x2A, 0x00], "webp": [0x52, 0x49, 0x46, 0x46], "jpg": [0xFF, 0xD8, 0xFF]}


def detect_image_format(head):
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:4] == b"\x89PNG":
        return "png"
    if head[:3] == b"GIF":
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return "bin"


def decrypt_dat(dat_path, out_path, aes_key, xor_key):
    from Crypto.Cipher import AES
    from Crypto.Util import Padding
    with open(dat_path, "rb") as f:
        data = f.read()
    if len(data) < 15:
        return None
    sig = data[:6]
    if sig == V2_MAGIC_FULL:
        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
        aligned = aes_size - ~(~aes_size % 16)
        off = 15
        if off + aligned > len(data):
            return None
        try:
            cipher = AES.new(aes_key.encode("ascii")[:16], AES.MODE_ECB)
            dec = Padding.unpad(cipher.decrypt(data[off:off + aligned]), AES.block_size)
        except (ValueError, KeyError):
            return None
        off += aligned
        raw_end = len(data) - xor_size
        dec = dec + (data[off:raw_end] if off < raw_end else b"")
        dec += bytes(b ^ xor_key for b in data[raw_end:])
    elif sig == V1_MAGIC_FULL:
        return _decrypt_v1(dat_path, out_path, xor_key)
    else:
        # 旧 XOR: 先按 magic 探测, 失败用账号 xorKey
        head = data[:16]
        key = None
        for magic in IMAGE_MAGIC.values():
            k = head[0] ^ magic[0]
            if all(head[i] ^ k == magic[i] for i in range(1, len(magic))):
                key = k; break
        if key is None:
            key = xor_key
        dec = bytes(b ^ key for b in data)
    fmt = detect_image_format(dec[:16])
    if fmt == "bin":
        return None
    with open(out_path + "." + fmt, "wb") as f:
        f.write(dec)
    return out_path + "." + fmt


def _decrypt_v1(dat_path, out_path, xor_key):
    from Crypto.Cipher import AES
    from Crypto.Util import Padding
    with open(dat_path, "rb") as f:
        data = f.read()
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    aligned = aes_size - ~(~aes_size % 16)
    try:
        cipher = AES.new(b"cfcd208495d565ef", AES.MODE_ECB)
        dec = Padding.unpad(cipher.decrypt(data[15:15 + aligned]), AES.block_size)
    except (ValueError, KeyError):
        return None
    raw_end = len(data) - xor_size
    dec = dec + (data[15 + aligned:raw_end] if 15 + aligned < raw_end else b"")
    dec += bytes(b ^ xor_key for b in data[raw_end:])
    fmt = detect_image_format(dec[:16])
    if fmt == "bin":
        return None
    with open(out_path + "." + fmt, "wb") as f:
        f.write(dec)
    return out_path + "." + fmt


def build_dat_index():
    """md5 -> [dat 路径...]（原图优先排序）"""
    idx = {}
    for p in glob.glob(os.path.join(ATTACH_DIR, "*", "*", "Img", "*.dat")):
        name = os.path.splitext(os.path.basename(p))[0]
        md5 = re.sub(r"_(t_)?[Wh]$", "", name)
        idx.setdefault(md5, []).append(p)
    for v in idx.values():
        v.sort(key=lambda p: ("_t_" in p, "_h" in p, p))
    return idx


def cmd_images():
    aes_key, xor_key = load_image_keys()
    con = sqlite3.connect(CACHE_DB)
    refs = [r[0] for r in con.execute("SELECT DISTINCT ref FROM msgs WHERE ref != ''")]
    con.close()
    print(f"[images] 需处理图片 md5 {len(refs)} 个")
    idx = build_dat_index()
    os.makedirs(IMG_OUT, exist_ok=True)
    existing = {fn.split(".")[0] for fn in os.listdir(IMG_OUT)}
    ok = miss = fail = 0
    t0 = time.time()
    for i, md5 in enumerate(refs):
        out_base = os.path.join(IMG_OUT, md5)
        if md5 in existing:
            ok += 1
            continue
        dats = idx.get(md5)
        if not dats:
            miss += 1
            continue
        done = None
        for dat in dats:  # 原图在前
            try:
                done = decrypt_dat(dat, out_base, aes_key, xor_key)
            except Exception:
                done = None
            if done:
                break
        if done:
            ok += 1
        else:
            fail += 1
        if (i + 1) % 2000 == 0:
            print(f"  [{i+1}/{len(refs)}] ok={ok} miss={miss} fail={fail} {time.time()-t0:.0f}s")
    print(f"[images] 完成: 成功 {ok}, 本地无文件 {miss}, 解密失败 {fail}, {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- OCR

def cmd_ocr(workers=6, max_n=0):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    con = sqlite3.connect(CACHE_DB)
    refs = [r[0] for r in con.execute("SELECT DISTINCT ref FROM msgs WHERE ref != ''")]
    con.close()
    if not os.path.exists(OCR_DB):
        sqlite3.connect(OCR_DB).close()
    done_con = sqlite3.connect(OCR_DB)
    done_con.execute("CREATE TABLE IF NOT EXISTS ocr_text(ref TEXT PRIMARY KEY, text TEXT)")
    done = {r[0] for r in done_con.execute("SELECT ref FROM ocr_text")}
    done_con.close()
    by_md5 = {}
    for fn in os.listdir(IMG_OUT):
        by_md5.setdefault(fn.split(".")[0], os.path.join(IMG_OUT, fn))
    todo = [r for r in refs if r not in done and r in by_md5]
    if max_n:
        todo = todo[:max_n]
    print(f"[ocr] 总图片 {len(refs)}, 已识别 {len(done)}, 待识别 {len(todo)}, 并发 {workers}")
    if not todo:
        return
    tasks = [(r, by_md5[r]) for r in todo]
    t0 = time.time()
    n_done = 0
    buf = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_ocr_one, t) for t in tasks]
        for f in as_completed(futs):
            buf.append(f.result())
            n_done += 1
            if len(buf) >= 300:
                c = sqlite3.connect(OCR_DB)
                c.executemany("INSERT OR REPLACE INTO ocr_text VALUES(?,?)", buf)
                c.commit(); c.close()
                buf.clear()
                print(f"  [{n_done}/{len(tasks)}] {time.time()-t0:.0f}s", flush=True)
    if buf:
        c = sqlite3.connect(OCR_DB)
        c.executemany("INSERT OR REPLACE INTO ocr_text VALUES(?,?)", buf)
        c.commit(); c.close()
    print(f"[ocr] 完成 {n_done} 张, {time.time()-t0:.0f}s")


_OCR = None


def _ocr_one(task):
    global _OCR
    ref, path = task
    try:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        result, _ = _OCR(path)
        text = "\n".join(item[1] for item in result) if result else ""
    except Exception:
        text = ""
    return ref, text


# ---------------------------------------------------------------- 向量语义检索

VEC_DB = os.path.join(VEC_DIR, "vectors.db")
EMBED_LABELS = {"[图片]", "[表情]", "[语音]", "[视频]", "[名片]", "[位置]", "[文件/链接]",
                "[消息]", "[文件]", "[链接]", ""}


def _embed_filter(content):
    c = content.strip()
    return (len(c) >= 2 and c not in EMBED_LABELS
            and not c.startswith(("[图片", "[表情", "[语音", "[视频", "[名片", "[位置")))


def cmd_embed(dim=512, batch=32, workers=1, max_rows=0, delay=1.0):
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not _read_key(ZP_KEY_FILE):
        print("[!] zhipu_key.txt 不存在"); sys.exit(1)
    os.makedirs(VEC_DIR, exist_ok=True)
    vdb = sqlite3.connect(VEC_DB)
    vdb.execute("CREATE TABLE IF NOT EXISTS t2i(content TEXT PRIMARY KEY, idx INTEGER)")
    vdb.execute("CREATE TABLE IF NOT EXISTS r2i(rowid INTEGER PRIMARY KEY, idx INTEGER)")
    if vdb.execute("SELECT COUNT(*) FROM r2i").fetchone()[0] == 0:
        con = sqlite3.connect(CACHE_DB)
        t2i, r2i, nxt = {}, [], 0
        for rid, c in con.execute("SELECT rowid, content FROM msgs ORDER BY rowid"):
            if not _embed_filter(c):
                continue
            i = t2i.get(c)
            if i is None:
                i = nxt; t2i[c] = i; nxt += 1
            r2i.append((rid, i))
        con.close()
        vdb.executemany("INSERT OR IGNORE INTO t2i VALUES(?,?)", list(t2i.items()))
        vdb.executemany("INSERT OR REPLACE INTO r2i VALUES(?,?)", r2i)
        vdb.commit()
    n_total = vdb.execute("SELECT COUNT(*) FROM t2i").fetchone()[0]
    n_msg = vdb.execute("SELECT COUNT(*) FROM r2i").fetchone()[0]
    n_done = os.path.getsize(VEC_FILE) // (dim * 2) if os.path.exists(VEC_FILE) else 0
    todo = [r[0] for r in vdb.execute("SELECT idx FROM t2i WHERE idx >= ? ORDER BY idx", (n_done,))]
    if max_rows:
        todo = todo[:max_rows]
    print(f"[embed] 唯一文本 {n_total} / 消息 {n_msg}, 已嵌入 {n_done}, 待嵌入 {len(todo)}, "
          f"batch={batch} workers={workers} dim={dim}")
    if not todo:
        json.dump({"dim": dim, "n": n_done}, open(VEC_META, "w"))
        print("[embed] 无待处理项"); return
    t2c = dict(vdb.execute("SELECT idx, content FROM t2i"))
    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]

    def work(ch):
        # 512 字符截断: 超长消息是 token 成本大头, 对检索语义几乎无损
        vecs, tok = zhipu_embed([t2c[i][:512] for i in ch], dim)
        return ch, vecs, tok

    t0 = time.time()
    total_tokens = 0
    written = 0
    next_idx = n_done
    pending = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = []
        for ch in chunks:
            futs.append(pool.submit(work, ch))
            if workers > 1:
                time.sleep(delay)
        for fut in as_completed(futs):
            try:
                ch, vecs, tok = fut.result()
            except Exception as e:
                print(f"[!] 批次失败跳过: {e}", flush=True)
                continue
            pending[ch[0]] = (ch, vecs, tok)
            total_tokens += tok
            # 按顺序落盘
            while next_idx in pending:
                ch, vecs, _ = pending.pop(next_idx)
                arr = np.asarray(vecs, dtype=np.float16)
                with open(VEC_FILE, "ab") as f:
                    arr.tofile(f)
                written += len(ch)
                next_idx += len(ch)
            if written and (written // batch) % 25 == 0:
                print(f"  [{written}/{len(todo)} 唯一文本] tokens={total_tokens} "
                      f"费用≈{total_tokens/1e6*0.5:.2f}元 {time.time()-t0:.0f}s", flush=True)
    json.dump({"dim": dim, "n": next_idx}, open(VEC_META, "w"))
    print(f"[embed] 完成 {written} 条, tokens={total_tokens}, 费用≈{total_tokens/1e6*0.5:.2f}元, "
          f"{time.time()-t0:.0f}s")


def semantic_search(query, contact="", sender="", start="", end="", limit=50):
    import numpy as np
    if not os.path.exists(VEC_META):
        print("[!] 向量索引不存在, 先运行: python wx_search.py embed")
        return []
    meta = json.load(open(VEC_META))
    dim, n = meta["dim"], meta["n"]
    if n == 0:
        print("[!] 向量索引为空"); return []
    vdb = sqlite3.connect(VEC_DB)
    # 1) 候选消息过滤条件 -> rowid 集合
    con = sqlite3.connect(CACHE_DB)
    where, params = ["1=1"], []
    if contact:
        ct = Contacts(); users, missed = set(), []
        for token in contact.split():
            hits = ct.match(token)
            if hits: users.update(hits)
            else: missed.append(token)
        if missed:
            print(f"[!] 聊天对象未匹配已忽略: {'、'.join(missed)}")
        if not users:
            print("[!] 没有匹配的聊天对象"); return []
        where.append(f"talker IN ({','.join('?' * len(users))})"); params += sorted(users)
    if sender:
        ct = ct if contact else Contacts(); users, missed = set(), []
        for token in sender.split():
            hits = ct.match(token)
            if hits: users.update(hits)
            else: missed.append(token)
        if not users:
            print("[!] 没有匹配的发送者"); return []
        where.append(f"sender IN ({','.join('?' * len(users))})"); params += sorted(users)
    ts0, ts1 = parse_date(start), parse_date(end, end=True)
    if ts0 is not None:
        where.append("ts >= ?"); params.append(ts0)
    if ts1 is not None:
        where.append("ts <= ?"); params.append(ts1)
    filtered = bool(contact or sender or start or end)
    if filtered:
        cand_rowids = [r[0] for r in con.execute(
            f"SELECT rowid FROM msgs WHERE {' AND '.join(where)}", params)]
        if not cand_rowids:
            return []
        cand_idx = set()
        for i in range(0, len(cand_rowids), 500):
            part = cand_rowids[i:i + 500]
            cand_idx.update(r[0] for r in vdb.execute(
                f"SELECT idx FROM r2i WHERE rowid IN ({','.join('?' * len(part))})", part))
        cand_idx = sorted(cand_idx)
    else:
        cand_idx = None  # 全库
    # 2) 查询向量
    qvec = np.asarray(zhipu_embed([query], dim)[0][0], dtype=np.float32)
    qvec /= (np.linalg.norm(qvec) + 1e-9)
    # 3) 分块余弦 top-K
    mm = np.memmap(VEC_FILE, dtype=np.float16, mode="r", shape=(n, dim))
    top_k = max(int(limit), 50)
    best = []  # (sim, idx)
    idx_arr = np.asarray(cand_idx, dtype=np.int64) if cand_idx is not None else None
    total = len(idx_arr) if idx_arr is not None else n
    step = 65536
    for ofs in range(0, total, step):
        if idx_arr is not None:
            ids = idx_arr[ofs:ofs + step]
            block = np.asarray(mm[ids], dtype=np.float32)
        else:
            ids = np.arange(ofs, min(ofs + step, n))
            block = np.asarray(mm[ofs:min(ofs + step, n)], dtype=np.float32)
        sims = block @ qvec
        k = min(top_k, sims.shape[0])
        part = np.argpartition(-sims, k - 1)[:k]
        best += [(float(sims[j]), int(ids[j])) for j in part]
    best.sort(key=lambda x: -x[0])
    best = best[:top_k]
    if not best:
        return []
    # 4) 取详情
    idx2sim = {i: s for s, i in best}
    marks = ",".join("?" * len(idx2sim))
    rowid_sim = {}
    for i in range(0, len(idx2sim), 500):
        part = list(idx2sim)[i:i + 500]
        for rid, idx in vdb.execute(
                f"SELECT rowid, idx FROM r2i WHERE idx IN ({marks})", part):
            rowid_sim[rid] = idx2sim[idx]
    vdb.close()
    marks = ",".join("?" * len(rowid_sim))
    has_ocr, has_voice = _attach_side_dbs(con)
    ocr_sel = "IFNULL(o.text,'')" if has_ocr else "''"
    voice_sel = "IFNULL(vt.text,'')" if has_voice else "''"
    join = "LEFT JOIN odb.ocr_text o ON o.ref = m.ref AND m.ref != ''" if has_ocr else ""
    if has_voice:
        join += " LEFT JOIN vdb.voice_text vt ON vt.svr_id = m.svr_id AND m.svr_id != 0"
    rows = con.execute(
        f"SELECT m.talker, m.sender, m.ts, m.mtype, m.content, m.ref, {ocr_sel}, m.url, {voice_sel}, m.rowid "
        f"FROM msgs m {join} WHERE m.rowid IN ({marks})",
        list(rowid_sim)).fetchall()
    con.close()
    rows.sort(key=lambda r: -rowid_sim.get(r[9], 0))
    rows = rows[:limit]
    return [(t, s, ts, mt, c, ref, _combine_extra(o, v), u)
            for t, s, ts, mt, c, ref, o, u, v, _rid in rows]


# ---------------------------------------------------------------- 导出

def make_thumb(img_path, md5):
    """Word 嵌图用的小缩略图 (<=420px), 返回路径或 None"""
    out = os.path.join(IMG_THUMB, md5 + ".jpg")
    if os.path.exists(out):
        return out
    try:
        from PIL import Image
        im = Image.open(img_path)
        im = im.convert("RGB")
        im.thumbnail((420, 420))
        os.makedirs(IMG_THUMB, exist_ok=True)
        im.save(out, "JPEG", quality=75)
        return out
    except Exception:
        return None


def export_docx(rows, ct, args, out_path):
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def add_hyperlink(paragraph, link, text):
        r_id = paragraph.part.relate_to(
            link, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True)
        hl = OxmlElement("w:hyperlink")
        hl.set(qn("r:id"), r_id)
        run = OxmlElement("w:r")
        rPr = OxmlElement("w:rPr")
        color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); rPr.append(color)
        u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
        run.append(rPr)
        t = OxmlElement("w:t"); t.text = text; run.append(t)
        hl.append(run)
        paragraph._p.append(hl)

    def small_gray(p, text):
        rr = p.add_run(text)
        rr.font.size = Pt(9)
        rr.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc = Document()
    doc.add_heading("微信聊天记录检索汇总", level=0)
    cond = (f"关键词: {args.keyword or '（无）'} | 聊天对象: {args.contact or '（全部）'} | "
            f"发送者: {args.sender or '（全部）'} | 日期: {args.start or '不限'} ~ {args.end or '不限'}")
    if args.keyword and getattr(args, "any_kw", False):
        cond = cond.replace("关键词: ", "关键词(任一命中): ", 1)
    p = doc.add_paragraph()
    r = p.add_run(cond)
    r.font.size = Pt(10)
    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    doc.add_paragraph(f"共 {len(rows)} 条消息, 导出时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    kws = [k for k in (args.keyword or "").split() if k]

    def add_line(text):
        para = doc.add_paragraph()
        if not kws:
            para.add_run(text)
            return
        pos = 0
        low = text.lower()
        spans = sorted((low.find(k.lower()), len(k)) for k in kws if k.lower() in low)
        merged = []
        for s0, l0 in spans:
            if merged and s0 < merged[-1][0] + merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], s0 + l0 - merged[-1][0]))
            else:
                merged.append((s0, l0))
        for s0, l0 in merged:
            if s0 > pos:
                para.add_run(text[pos:s0])
            rr = para.add_run(text[s0:s0 + l0])
            rr.bold = True
            rr.font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)
            pos = s0 + l0
        if pos < len(text):
            para.add_run(text[pos:])

    for title, items in format_rows(rows, ct):
        doc.add_heading(title, level=2)
        for ts, line, ref, extra, url, content in items:
            add_line(line)
            if ref:
                fs = glob.glob(os.path.join(IMG_OUT, ref + ".*"))
                if fs:
                    thumb = make_thumb(fs[0], ref)
                    if thumb:
                        doc.add_picture(thumb, width=Inches(1.8))
            if extra:
                p = doc.add_paragraph()
                small_gray(p, extra[:600])
            if url:
                p = doc.add_paragraph()
                small_gray(p, "链接: ")
                add_hyperlink(p, url, url if len(url) <= 90 else url[:87] + "…")
            if content.startswith("[文件]"):
                lp = find_local_file(content, ts)
                p = doc.add_paragraph()
                if lp:
                    small_gray(p, f"本地文件: {lp}")
                else:
                    small_gray(p, "（文件未在本地下载，需在微信中点开下载后才能提取）")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc.save(out_path)
    return out_path


def export_txt(rows, ct, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for title, items in format_rows(rows, ct):
            f.write(f"\n===== {title} =====\n")
            for ts, line, ref, extra, url, content in items:
                f.write(line + "\n")
                if extra:
                    f.write("  " + extra[:400].replace("\n", " | ") + "\n")
                if url:
                    f.write(f"  链接: {url}\n")
                if content.startswith("[文件]"):
                    lp = find_local_file(content, ts)
                    f.write(f"  本地文件: {lp}\n" if lp else "  （文件未在本地下载）\n")
    return out_path


def copy_clipboard(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8-sig")
    tmp.write(text); tmp.close()
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Get-Content -Raw -LiteralPath '{tmp.name}' | Set-Clipboard"], check=False)
    os.unlink(tmp.name)


# ---------------------------------------------------------------- 清理

CLEAN_ITEMS = [
    ("1", "检索缓存 search_cache.db（重建约15秒）", lambda: [CACHE_DB] if os.path.exists(CACHE_DB) else []),
    ("2", "Word 嵌图缩略图缓存 img_thumb（自动重建）", lambda: [IMG_THUMB] if os.path.isdir(IMG_THUMB) else []),
    ("3", "解密图片 img_decoded（重新解密约40分钟）", lambda: [IMG_OUT] if os.path.isdir(IMG_OUT) else []),
    ("4", "OCR 文本库 ocr.db（重新识别约1-2小时）", lambda: [OCR_DB] if os.path.exists(OCR_DB) else []),
    ("5", "语义向量索引 vectors（重新嵌入约1小时+费用）", lambda: [VEC_DIR] if os.path.isdir(VEC_DIR) else []),
    ("6", "解密数据库 data/decrypted（重新解密约3分钟）", lambda: [DECRYPTED] if os.path.isdir(DECRYPTED) else []),
    ("7", "导出文档 output（不可再生，慎重）", lambda: [OUT_DIR] if os.path.isdir(OUT_DIR) else []),
    ("8", "日志与 __pycache__（无代价）", lambda: glob.glob(os.path.join(BASE, "**", "__pycache__"), recursive=True)
     + glob.glob(os.path.join(BASE, "tools", "wechat-decrypt", "*.log"))),
    ("9", "语音音频+转写库（重新转写约45分钟）", lambda: [p for p in (VOICE_DIR, VOICE_DB) if os.path.exists(p)]),
]


def _path_size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cmd_clean(level="", yes=False, dry=False):
    if level:
        picks = [it for it in CLEAN_ITEMS if it[0] == level]
        if not picks:
            print(f"[!] 未知级别: {level}"); sys.exit(1)
    else:
        print("=" * 60)
        print("  可清理项（按重建代价排序, 括号为当前占用）")
        print("=" * 60)
        for num, desc, getter in CLEAN_ITEMS:
            sz = sum(_path_size(p) for p in getter())
            print(f"  [{num}] {desc}  <{sz/1048576:.1f} MB>")
        picks = []
        sel = input("输入要清理的编号(逗号分隔, 回车取消): ").strip()
        if not sel:
            return
        want = {s.strip() for s in sel.split(",") if s.strip()}
        picks = [it for it in CLEAN_ITEMS if it[0] in want]
    if not picks:
        return
    targets = [p for _, _, getter in picks for p in getter()]
    total = sum(_path_size(p) for p in targets)
    print("\n将删除:")
    for p in targets:
        print(f"  {p}")
    print(f"合计 {total/1048576:.1f} MB")
    if dry:
        print("[dry-run] 未实际删除"); return
    if not yes:
        if input("确认删除? (y/N): ").strip().lower() != "y":
            print("已取消"); return
    import shutil
    ok = fail = 0
    for p in targets:
        try:
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p)
            ok += 1
        except (OSError, PermissionError) as e:
            print(f"  [跳过] {p}: {e}")
            fail += 1
    print(f"[clean] 删除 {ok} 项" + (f", 跳过 {fail} 项(文件被占用)" if fail else ""))


# ---------------------------------------------------------------- 多账号 / 同步 / 语音 / 自检

def cmd_use():
    """列出/切换账号"""
    cfg = _load_config()
    accs = dict(cfg["accounts"])
    for name, info in _discover_accounts(cfg.get("wechat_root")).items():
        accs.setdefault(name, info)
    if not accs:
        print("[!] 未发现任何微信账号目录 (xwechat_files)"); return
    names = sorted(accs)
    print("可用账号:")
    for i, n in enumerate(names):
        mark = " <- 当前" if n == cfg["active"] else ""
        print(f"  [{i + 1}] {n} {accs[n].get('label', '')}{mark}")
    sel = input("切换到编号 (回车取消): ").strip()
    if not sel.isdigit() or not (1 <= int(sel) <= len(names)):
        return
    active = names[int(sel) - 1]
    cfg["active"] = active
    _save_config(cfg)
    tools_cfg = os.path.join(BASE, "tools", "wechat-decrypt", "config.json")
    try:
        tc = {"db_dir": accs[active]["db_dir"], "keys_file": "all_keys.json",
              "decrypted_dir": "..\\..\\data\\accounts\\%s\\decrypted" % active,
              "wechat_process": "Weixin.exe"}
        json.dump(tc, open(tools_cfg, "w", encoding="utf-8"), indent=4)
    except Exception:
        pass
    print(f"[+] 已切换到 {active}。重新运行 wx_search.py 后生效。")
    if not os.path.exists(os.path.join(ROOT_DATA, "accounts", active, "keys", "all_keys.json")):
        print("[!] 该账号尚未配置数据库密钥")


PAGE_SZ, RESERVE_SZ, SALT_SZ = 4096, 80, 16


def _decrypt_page(enc_key, page_data, pgno):
    """单页解密 -> 4096 字节标准 SQLite 页"""
    from Crypto.Cipher import AES
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    if pgno == 1:
        return b"SQLite format 3\x00" + cipher.decrypt(page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]) \
               + b"\x00" * RESERVE_SZ
    return cipher.decrypt(page_data[:PAGE_SZ - RESERVE_SZ]) + b"\x00" * RESERVE_SZ


def _verify_page1(enc_key, page1):
    mac_salt = bytes(b ^ 0x3A for b in page1[:SALT_SZ])
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    h = hmac_mod.new(mac_key, page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + 16] + struct.pack("<I", 1),
                     hashlib.sha512)
    return h.digest() == page1[PAGE_SZ - 64: PAGE_SZ]


def _decrypt_wal_patch(wal_path, out_path, enc_key):
    """解密 WAL 有效 frame 并 patch 到已解密 DB（微信运行中的新消息都在 WAL 里）"""
    if not os.path.exists(wal_path):
        return 0
    frame_sz = 24 + PAGE_SZ
    wal_size = os.path.getsize(wal_path)
    if wal_size <= 32:
        return 0
    patched = 0
    with open(wal_path, "rb") as wf, open(out_path, "r+b") as df:
        hdr = wf.read(32)
        salt1, salt2 = struct.unpack(">II", hdr[16:24])
        while wf.tell() + frame_sz <= wal_size:
            fh = wf.read(24)
            pgno = struct.unpack(">I", fh[0:4])[0]
            fs1, fs2 = struct.unpack(">II", fh[8:16])
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0 or pgno > 1000000 or fs1 != salt1 or fs2 != salt2:
                continue
            dec = _decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1
    return patched


def _decrypt_pages_worker(args):
    src, start_pg, end_pg, enc_key = args
    out = []
    with open(src, "rb") as f:
        f.seek((start_pg - 1) * PAGE_SZ)
        for pgno in range(start_pg, end_pg + 1):
            page = f.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                page += b"\x00" * (PAGE_SZ - len(page))
            out.append(_decrypt_page(enc_key, page, pgno))
    return start_pg, b"".join(out)


def _decrypt_db_parallel(src, out_path, enc_key, workers=6):
    """并行解密单个库（page1 HMAC 校验；src 是临时快照）"""
    from concurrent.futures import ProcessPoolExecutor
    size = os.path.getsize(src)
    npages = size // PAGE_SZ
    if npages == 0:
        return False
    with open(src, "rb") as f:
        page1 = f.read(PAGE_SZ)
    if not _verify_page1(enc_key, page1):
        return False
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.truncate(npages * PAGE_SZ)
    step = max(2000, npages // (workers * 4) + 1)
    bounds = []
    s = 1
    while s <= npages:
        e = min(s + step - 1, npages)
        bounds.append((src, s, e, enc_key))
        s = e + 1
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for start_pg, blob in pool.map(_decrypt_pages_worker, bounds):
            with open(out_path, "r+b") as f:
                f.seek((start_pg - 1) * PAGE_SZ)
                f.write(blob)
    return True


def _copy_file(src, dst):
    with open(src, "rb") as a, open(dst, "wb") as b:
        while True:
            chunk = a.read(1 << 22)
            if not chunk:
                break
            b.write(chunk)


def cmd_sync(quiet=False):
    """增量同步: 变化的库(含WAL)重解密 -> 重建缓存 -> 增量图片/OCR/向量"""
    log = (lambda *a: None) if quiet else print
    keys_file = os.path.join(KEYS_DIR, "all_keys.json")
    if not os.path.exists(keys_file):
        tools_keys = os.path.join(BASE, "tools", "wechat-decrypt", "all_keys.json")
        if os.path.exists(tools_keys):
            os.makedirs(KEYS_DIR, exist_ok=True)
            _copy_file(tools_keys, keys_file)
            log("[sync] 已从工具目录导入密钥")
        else:
            print("[!] 缺少数据库密钥，请先配置本地授权数据")
            sys.exit(1)
    keys = json.load(open(keys_file, encoding="utf-8"))
    state = json.load(open(SYNC_STATE, encoding="utf-8")) if os.path.exists(SYNC_STATE) else {}
    os.makedirs(DECRYPTED, exist_ok=True)
    changed = 0
    for rel, info in keys.items():
        src = os.path.join(DB_DIR, rel)
        if not os.path.exists(src):
            continue
        wal = src + "-wal"
        mt = [os.path.getmtime(src), os.path.getmtime(wal) if os.path.exists(wal) else 0]
        if state.get(rel) == mt:
            continue
        enc_key = bytes.fromhex(info["enc_key"])
        out_path = os.path.join(DECRYPTED, rel)
        tmp_db = out_path + ".tmp"
        _copy_file(src, tmp_db)
        if not _decrypt_db_parallel(tmp_db, out_path, enc_key, workers=6):
            os.remove(tmp_db)
            log(f"  [skip] {rel} 校验失败(可能正在写入), 下次重试")
            continue
        npatched = 0
        if os.path.exists(wal):
            tmp_wal = out_path + ".wal.tmp"
            _copy_file(wal, tmp_wal)
            npatched = _decrypt_wal_patch(tmp_wal, out_path, enc_key)
            os.remove(tmp_wal)
        os.remove(tmp_db)
        state[rel] = mt
        changed += 1
        log(f"  [sync] {rel}: 已更新" + (f", WAL补丁 {npatched} 页" if npatched else ""))
    json.dump(state, open(SYNC_STATE, "w"))
    json.dump({"last_sync": time.time(), "changed": changed},
              open(os.path.join(ACC_DIR, "last_sync.json"), "w"))
    log(f"[sync] {'%d 个库有变化' % changed if changed else '无变化'}")
    if changed:
        build_cache()
        cmd_images()
        cmd_ocr(4, max_n=3000)
        cmd_voices(workers=4, max_n=3000)
        if os.path.exists(VEC_META) and _read_key(ZP_KEY_FILE):
            try:
                cmd_embed()
                log("[sync] 向量索引增量完成")
            except Exception as e:
                log(f"[!] 向量增量失败(可手动 python wx_search.py embed): {e}")
    return changed


def cmd_watch(install=False, uninstall=False):
    """监视微信启动 -> 自动同步一次; --install 注册登录自启, --uninstall 移除"""
    if install or uninstall:
        tn = "WXChatSyncWatcher"
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        tr = f'"{pyw}" "{os.path.join(BASE, "wx_search.py")}" watch'
        if uninstall:
            subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], capture_output=True)
            print("[+] 已移除开机自动同步")
        else:
            r = subprocess.run(["schtasks", "/Create", "/TN", tn, "/SC", "ONLOGON",
                                "/TR", tr, "/F"], capture_output=True, text=True)
            print("[+] 已注册: 登录 Windows 后自动监视微信并同步" if r.returncode == 0
                  else f"[!] 注册失败: {r.stderr.strip()}")
        return
    import ctypes
    from ctypes import wintypes as wt

    def weixin_running():
        kernel32 = ctypes.WinDLL("kernel32")
        TH32CS_SNAPPROCESS = 0x2

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        entry = PE(); entry.dwSize = ctypes.sizeof(PE)
        found = False
        if kernel32.Process32First(snap, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.decode(errors="ignore").lower() == "weixin.exe":
                    found = True
                    break
                if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snap)
        return found

    print("[watch] 监视微信启动中 (Ctrl+C 退出)...", flush=True)
    was_running = weixin_running()
    last_sync_at = 0
    while True:
        time.sleep(15)
        running = weixin_running()
        if running and not was_running and time.time() - last_sync_at > 600:
            time.sleep(20)  # 等微信登录并打开数据库
            print(f"[watch] 检测到微信启动, 开始同步 {time.strftime('%H:%M')}", flush=True)
            try:
                cmd_sync(quiet=True)
                last_sync_at = time.time()
                print("[watch] 同步完成", flush=True)
            except SystemExit:
                pass
            except Exception as e:
                print(f"[watch] 同步失败: {e}", flush=True)
        was_running = running


def cmd_doctor():
    print("=" * 60)
    print("  doctor 自检")
    print("=" * 60)
    rows = []
    rows.append(("当前账号", f"{_ACC} ({_ACC_INFO.get('label', '')})"))
    rows.append(("微信数据目录", f"{'OK' if os.path.isdir(WX_ROOT) else '不存在!'} {WX_ROOT}"))
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    exe = glob.glob(os.path.join(local_appdata, "Tencent", "Weixin", "**", "Weixin.exe"), recursive=True) + \
        glob.glob(os.path.join(program_files, "Tencent", "Weixin", "**", "Weixin.exe"), recursive=True)
    rows.append(("微信版本", os.path.basename(os.path.dirname(exe[0])) if exe else "未找到(升级后可能需重新取密钥)"))
    rows.append(("数据库密钥", "OK" if os.path.exists(os.path.join(KEYS_DIR, "all_keys.json"))
                 else "缺失（请通过合法的本地数据准备流程配置）"))
    rows.append(("图片密钥", "OK" if os.path.exists(os.path.join(KEYS_DIR, "image_keys.json"))
                 else "缺失(运行 images 时自动提取)"))
    for mod in ("docx", "zstandard", "Crypto", "yara", "rapidocr_onnxruntime"):
        try:
            __import__(mod)
            rows.append((f"依赖 {mod}", "OK"))
        except ImportError:
            rows.append((f"依赖 {mod}", "缺失! pip install"))
    rows.append(("智谱语义检索", "OK" if _read_key(ZP_KEY_FILE) else "未配置 (zhipu_key.txt)"))
    try:
        import shutil as _sh
        free = _sh.disk_usage(BASE).free / 1073741824
        rows.append(("磁盘剩余", f"{free:.1f} GB" + (" (偏低!)" if free < 5 else "")))
    except Exception:
        pass
    for k, v in rows:
        print(f"  {k:　<10} {v}")
    print("\n  运行 python wx_search.py status 查看索引情况")


def cmd_status():
    print(f"账号: {_ACC} ({_ACC_INFO.get('label', '')})")
    lp = os.path.join(ACC_DIR, "last_sync.json")
    if os.path.exists(lp):
        last = json.load(open(lp))
        print("上次同步:", datetime.fromtimestamp(last["last_sync"]).strftime("%Y-%m-%d %H:%M:%S"),
              f"(变化 {last.get('changed', '?')} 库)")
    else:
        print("上次同步: 从未 (运行 python wx_search.py sync)")
    if os.path.exists(CACHE_DB):
        con = sqlite3.connect(CACHE_DB)
        n = con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        span = con.execute("SELECT MIN(ts), MAX(ts) FROM msgs").fetchone()
        n_img = con.execute("SELECT COUNT(*) FROM msgs WHERE ref != ''").fetchone()[0]
        n_fwd = con.execute("SELECT COUNT(*) FROM msgs WHERE content LIKE '[聊天记录%'").fetchone()[0]
        if span[0]:
            print(f"消息索引: {n} 条 ({datetime.fromtimestamp(span[0]).strftime('%Y-%m-%d')} ~ "
                  f"{datetime.fromtimestamp(span[1]).strftime('%Y-%m-%d')}), 图片 {n_img}, 合并转发 {n_fwd}")
        con.close()
    if os.path.exists(OCR_DB):
        oc = sqlite3.connect(OCR_DB)
        try:
            print("图片OCR:", oc.execute("SELECT COUNT(*) FROM ocr_text").fetchone()[0], "张")
        except sqlite3.OperationalError:
            pass
        oc.close()
    if os.path.exists(VOICE_DB):
        vc = sqlite3.connect(VOICE_DB)
        print("语音转写:", vc.execute("SELECT COUNT(*) FROM voice_text").fetchone()[0], "条")
        vc.close()
    if os.path.exists(VEC_META):
        print(f"向量索引: {json.load(open(VEC_META))['n']} 条")
    for name, path in [("解密库", DECRYPTED), ("解密图片", IMG_OUT), ("向量索引", VEC_DIR)]:
        if os.path.exists(path):
            print(f"{name}占用: {_path_size(path) / 1048576:.0f} MB")


# ---------------------------------------------------------------- 语音转写

VOICE_DIR = os.path.join(ACC_DIR, "voices")
ASR_DIR = os.path.join(BASE, "tools", "asr")
ASR_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                 "sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2")
ASR_MODEL_NAME = "sherpa-onnx-paraformer-zh-small-2024-03-09"


def _ensure_asr_model():
    """确保 paraformer-small 模型就位, 返回 (model.onnx, tokens.txt) 或 None"""
    base = os.path.join(ASR_DIR, ASR_MODEL_NAME)
    model = os.path.join(base, "model.int8.onnx")
    tokens = os.path.join(base, "tokens.txt")
    if os.path.exists(model) and os.path.exists(tokens):
        return model, tokens
    print("[asr] 首次使用, 下载语音识别模型 (~90MB)...")
    os.makedirs(ASR_DIR, exist_ok=True)
    archive = os.path.join(ASR_DIR, ASR_MODEL_NAME + ".tar.bz2")
    try:
        urllib.request.urlretrieve(ASR_MODEL_URL, archive)
        import tarfile
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(ASR_DIR)
        os.remove(archive)
    except Exception as e:
        print(f"[!] 模型下载失败: {e}\n    可手动下载解压到 {ASR_DIR}: {ASR_MODEL_URL}")
        return None
    return (model, tokens) if os.path.exists(model) and os.path.exists(tokens) else None


_VREC = None


def _voice_worker(task):
    svr_id, wav_path, model_dir = task
    try:
        global _VREC
        import wave
        import numpy as np
        if _VREC is None:
            import sherpa_onnx
            m = os.path.join(model_dir, "model.int8.onnx")
            tk = os.path.join(model_dir, "tokens.txt")
            _VREC = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=m, tokens=tk, num_threads=1, provider="cpu")
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        samples = samples.astype(np.float32) / 32768.0
        stream = _VREC.create_stream()
        stream.accept_waveform(sr, samples)
        _VREC.decode_stream(stream)
        return svr_id, stream.result.text.strip()
    except Exception:
        return svr_id, ""


def cmd_voices(workers=4, max_n=0, no_asr=False):
    """提取语音 -> SILK 解码 -> 离线 ASR -> voice.db (按 svr_id 关联消息)"""
    os.makedirs(VOICE_DIR, exist_ok=True)
    n = 0
    for rel in ["message\\media_0.db", "message\\media_1.db"]:
        p = os.path.join(DECRYPTED, rel)
        if not os.path.exists(p):
            continue
        con = sqlite3.connect(p)
        try:
            cur = con.execute("SELECT chat_name_id, svr_id, voice_data FROM VoiceInfo")
            for chat_name_id, svr_id, vd in cur:
                if not vd:
                    continue
                magic = bytes(vd)[1:10] if bytes(vd)[:1] == b"\x02" else bytes(vd)[:9]
                if magic != b"#!SILK_V3":
                    continue
                out = os.path.join(VOICE_DIR, f"{svr_id}.silk")
                if not os.path.exists(out):
                    with open(out, "wb") as f:
                        f.write(bytes(vd))
                    n += 1
        except sqlite3.OperationalError as e:
            print(f"[!] {rel}: {e}")
        con.close()
    print(f"[voices] 新提取语音 {n} 条")
    if no_asr or n == 0 and not glob.glob(os.path.join(VOICE_DIR, "*.silk")):
        return
    if n == 0 and not glob.glob(os.path.join(VOICE_DIR, "*.silk")):
        print("[voices] 没有本地语音"); return
    try:
        import pysilk  # noqa
    except ImportError:
        print("[!] 缺少 silk 解码依赖: pip install pysilk-mod"); return
    try:
        import sherpa_onnx  # noqa
    except ImportError:
        print("[!] 缺少语音识别依赖: pip install sherpa-onnx"); return
    model_dir = os.path.join(ASR_DIR, ASR_MODEL_NAME)
    if not _ensure_asr_model():
        return
    vdb = sqlite3.connect(VOICE_DB)
    vdb.execute("CREATE TABLE IF NOT EXISTS voice_text(svr_id INTEGER PRIMARY KEY, text TEXT)")
    done = {r[0] for r in vdb.execute("SELECT svr_id FROM voice_text")}
    vdb.close()
    silks = sorted(
        (os.path.join(VOICE_DIR, fn) for fn in os.listdir(VOICE_DIR) if fn.endswith(".silk")),
        key=os.path.getmtime)
    todo = [s for s in silks
            if int(os.path.splitext(os.path.basename(s))[0]) not in done]
    if max_n:
        todo = todo[:max_n]
    print(f"[voices] 待转写 {len(todo)} 条, workers={workers}")
    if not todo:
        return
    # SILK -> 16k wav (pysilk-mod 需要保留微信  前缀)
    import wave
    wavs = []
    t0 = time.time()
    for s in todo:
        sid = int(os.path.splitext(os.path.basename(s))[0])
        try:
            with open(s, "rb") as f:
                pcm = pysilk.decode(f.read(), sample_rate=16000)
            wav = os.path.join(VOICE_DIR, f"{sid}.wav")
            with wave.open(wav, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
                wf.writeframes(pcm)
            wavs.append((sid, wav))
        except Exception:
            continue
    print(f"[voices] 解码完成 {len(wavs)}/{len(todo)}, {time.time()-t0:.0f}s")
    from concurrent.futures import ProcessPoolExecutor, as_completed
    t0 = time.time()
    buf = []
    n_ok = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_voice_worker, (sid, wav, model_dir)) for sid, wav in wavs]
        for f in as_completed(futs):
            sid, text = f.result()
            buf.append((sid, text))
            n_ok += 1
            if len(buf) >= 100:
                vdb = sqlite3.connect(VOICE_DB)
                vdb.executemany("INSERT OR REPLACE INTO voice_text VALUES(?,?)", buf)
                vdb.commit(); vdb.close(); buf.clear()
                print(f"  [{n_ok}/{len(wavs)}] {time.time()-t0:.0f}s", flush=True)
    if buf:
        vdb = sqlite3.connect(VOICE_DB)
        vdb.executemany("INSERT OR REPLACE INTO voice_text VALUES(?,?)", buf)
        vdb.commit(); vdb.close()
    print(f"[voices] 转写完成 {n_ok} 条, {time.time()-t0:.0f}s")


# ---------------------------------------------------------------- 入口

def run_search(a):
    if getattr(a, "semantic", ""):
        rows = semantic_search(a.semantic, a.contact, a.sender, a.start, a.end, a.limit)
        a = argparse.Namespace(**{**vars(a), "keyword": a.semantic})
    else:
        rows = search(a.keyword, a.contact, a.sender, a.start, a.end, a.limit,
                      expand=not a.no_expand, use_llm=a.llm,
                      any_kw=getattr(a, "any_kw", False))
    if not rows:
        print("没有匹配的记录。")
        return
    ct = Contacts()
    n_conv = len({r[0] for r in rows})
    n_img = sum(1 for r in rows if r[5])
    print(f"\n命中 {len(rows)} 条消息, 涉及 {n_conv} 个会话" + (f", 含图片 {n_img} 张" if n_img else "") + ":\n")
    for title, items in format_rows(rows, ct):
        print(f"  {title}: {len(items)} 条  ({items[0][1][1:17]} ~ {items[-1][1][1:17]})")
    print("\n--- 预览(前 15 条) ---")
    shown = 0
    for title, items in format_rows(rows, ct):
        print(f"\n◆ {title}")
        for ts, line, ref, extra, url, content in items:
            print("  " + (line if len(line) <= 120 else line[:120] + "…"))
            if extra:
                print("    " + extra[:100].replace("\n", " ⏎ ") + ("…" if len(extra) > 100 else ""))
            if url:
                print("    链接: " + (url[:90] + "…" if len(url) > 90 else url))
            if content.startswith("[文件]") and "[文件]" in line:
                lp = find_local_file(content, ts)
                print("    本地文件: " + lp if lp else "    （文件未在本地下载）")
            shown += 1
            if shown >= 15:
                break
        if shown >= 15:
            break

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = re.sub(r"[\\/:*?\"<>|\s]+", "_", a.keyword or (a.contact or "记录"))[:30]
    out_path = a.out or os.path.join(OUT_DIR, f"检索_{tag}_{stamp}.docx")
    if out_path.lower().endswith(".txt"):
        export_txt(rows, ct, out_path)
    else:
        export_docx(rows, ct, a, out_path)
    print(f"\n[+] 已导出: {out_path}")
    if not a.no_clip:
        text = "\n".join(f"◆ {t}\n" + "\n".join(
            l + (f"\n链接: {u}" if u else "") for _, l, _, _, u, _ in items)
            for t, items in format_rows(rows, ct))
        copy_clipboard(text)
        print("[+] 结果已复制到剪贴板, 可直接粘贴")


def interactive():
    print("=" * 56)
    print("  微信聊天记录检索 (回车 = 跳过该条件, q = 退出)")
    print("=" * 56)
    has_ds = os.path.exists(DS_KEY_FILE)
    while True:
        try:
            kw = input("关键词 (空格分隔=同时包含): ").strip()
            if kw.lower() == "q":
                break
            contact = input("聊天对象 (可多个, 空格分隔, 模糊): ").strip()
            sender = input("发送者 (可多个, 空格分隔, 可空): ").strip()
            start = input("开始日期 (YYYY-MM-DD, 可空): ").strip()
            end = input("结束日期 (YYYY-MM-DD, 可空): ").strip()
            lim = input("最大条数 [500]: ").strip()
            mode = "1"
            if has_ds and kw:
                mode = input("关键词扩展: [1]同义词词典 [2]LLM语义 [0]关闭 [1]: ").strip() or "1"
            m2 = "1"
            if os.path.exists(VEC_META) and kw:
                m2 = input("检索方式 [1]关键词 [2]语义向量 [1]: ").strip() or "1"
            any_mode = "1"
            if len(kw.split()) > 1 and m2 != "2":
                any_mode = input("关键词匹配: [1]同时包含 [2]任一命中 [1]: ").strip() or "1"
            a = argparse.Namespace(
                keyword=kw, contact=contact, sender=sender, start=start, end=end,
                limit=int(lim) if lim.isdigit() else 500, out=None, no_clip=False,
                no_expand=(mode == "0"), llm=(mode == "2"),
                semantic=(kw if m2 == "2" else ""),
                any_kw=(any_mode == "2"))
            if not any([kw, contact, sender, start, end]):
                print("[!] 至少填一个条件\n")
                continue
            run_search(a)
        except (KeyboardInterrupt, EOFError):
            break
        print("\n" + "-" * 56)


def main():
    ap = argparse.ArgumentParser(description="微信聊天记录检索导出工具")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("build", help="构建检索缓存")
    sub.add_parser("images", help="解密图片 .dat 文件")
    sp_ocr = sub.add_parser("ocr", help="图片 OCR（增量）")
    sp_ocr.add_argument("-w", "--workers", type=int, default=6)
    sp = sub.add_parser("search", help="搜索并导出")
    sp.add_argument("-k", "--keyword", default="")
    sp.add_argument("--semantic", default="", help="语义向量检索的查询句（需先 build 向量索引）")
    sp.add_argument("-c", "--contact", default="")
    sp.add_argument("--sender", default="")
    sp.add_argument("-s", "--start", default="")
    sp.add_argument("-e", "--end", default="")
    sp.add_argument("-l", "--limit", type=int, default=500)
    sp.add_argument("-o", "--out", default=None)
    sp.add_argument("--no-clip", action="store_true")
    sp.add_argument("--no-expand", action="store_true", help="关闭同义词扩展")
    sp.add_argument("--llm", action="store_true", help="用 LLM 扩展关键词（智谱/DeepSeek）")
    sp.add_argument("--any", dest="any_kw", action="store_true",
                    help="多个关键词任一命中即可（默认需同时包含）")
    sp_embed = sub.add_parser("embed", help="构建语义向量索引（智谱 embedding-3）")
    sp_embed.add_argument("--dim", type=int, default=512)
    sp_embed.add_argument("--batch", type=int, default=32)
    sp_embed.add_argument("--workers", type=int, default=1)
    sp_embed.add_argument("--delay", type=float, default=1.0)
    sp_embed.add_argument("--max", type=int, default=0, help="仅嵌入前 N 条（测试用）")
    sub.add_parser("use", help="切换账号")
    sp_sync = sub.add_parser("sync", help="增量同步（微信开着也能同步）")
    sp_sync.add_argument("--quiet", action="store_true")
    sp_watch = sub.add_parser("watch", help="监视微信启动自动同步")
    sp_watch.add_argument("--install", action="store_true", help="注册登录自启")
    sp_watch.add_argument("--uninstall", action="store_true", help="移除登录自启")
    sp_voices = sub.add_parser("voices", help="语音转写（提取+解码+离线ASR, 增量）")
    sp_voices.add_argument("-w", "--workers", type=int, default=4)
    sp_voices.add_argument("--max", type=int, default=0)
    sp_voices.add_argument("--no-asr", action="store_true", help="只提取音频不转写")
    sub.add_parser("doctor", help="环境自检")
    sub.add_parser("status", help="索引状态")
    sp_clean = sub.add_parser("clean", help="分级清理缓存/导出/临时文件")
    sp_clean.add_argument("--level", default="", help="1缓存 2缩略图 3解密图片 4OCR 5向量 6解密库 7导出 8日志")
    sp_clean.add_argument("--yes", action="store_true", help="跳过确认")
    sp_clean.add_argument("--dry-run", action="store_true", help="只列出, 不删除")
    args = ap.parse_args()
    if args.cmd == "build":
        build_cache()
    elif args.cmd == "images":
        cmd_images()
    elif args.cmd == "ocr":
        cmd_ocr(args.workers)
    elif args.cmd == "embed":
        cmd_embed(args.dim, args.batch, args.workers, args.max, args.delay)
    elif args.cmd == "clean":
        cmd_clean(args.level, args.yes, args.dry_run)
    elif args.cmd == "use":
        cmd_use()
    elif args.cmd == "sync":
        cmd_sync(args.quiet)
    elif args.cmd == "watch":
        cmd_watch(args.install, args.uninstall)
    elif args.cmd == "voices":
        cmd_voices(args.workers, args.max, args.no_asr)
    elif args.cmd == "doctor":
        cmd_doctor()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "search":
        if any([args.keyword, args.semantic, args.contact, args.sender, args.start, args.end]):
            run_search(args)
        else:
            interactive()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
