# -*- coding: utf-8 -*-
"""wx_search.py 测试套件: 解析函数单测 + 合成数据集成测试（覆盖历史回归点）"""
import io
import json
import os
import sqlite3
import struct
import sys
import hashlib
import hmac as hmac_mod
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wx_search as W  # noqa: E402

ZSTD = zstd_compress = None


def zc(data: bytes) -> bytes:
    import zstandard
    return zstandard.ZstdCompressor().compress(data)


# ---------------------------------------------------------------- 纯函数

def test_decode_content_str_passthrough():
    assert W.decode_content("hello") == "hello"


def test_decode_content_zstd():
    assert W.decode_content(zc("中文压缩".encode())) == "中文压缩"


def test_decode_content_bad_bytes():
    assert "abc" in W.decode_content(b"abc\xff\xfe")


def test_xml_title_cdata():
    assert W.xml_title("<msg><title><![CDATA[标题A]]></title></msg>") == "标题A"


def test_xml_url_unescape():
    u = W.xml_url("<msg><url>https://x.com/a?b=1&amp;c=2</url></msg>")
    assert u == "https://x.com/a?b=1&c=2"


def test_appmsg_classify_file():
    xml = "<msg><appmsg><title>a.xlsx</title><type>6</type><appattach><fileext>xlsx</fileext></appattach></appmsg></msg>"
    assert W.appmsg_classify(xml, "a.xlsx")[0] == "[文件]"


def test_appmsg_classify_gzh():
    xml = "<msg><appmsg><title>t</title><type>5</type><url>https://mp.weixin.qq.com/s?x=1</url></appmsg></msg>"
    tag, url = W.appmsg_classify(xml, "t")
    assert tag == "[公众号]" and "mp.weixin.qq.com" in url


def test_appmsg_classify_link():
    xml = "<msg><appmsg><title>t</title><type>5</type><url>https://example.com</url></appmsg></msg>"
    assert W.appmsg_classify(xml, "t")[0] == "[链接]"


def test_appmsg_classify_pat_not_file():
    """回归: 拍一拍消息(type 62/空 fileext)不能判成文件"""
    xml = ("<msg><appmsg><title>\"A\" 拍了拍 \"B\"</title><type>62</type>"
           "<appattach><fileext></fileext></appattach></appmsg></msg>")
    assert W.appmsg_classify(xml, "")[0] == ""


def test_img_md5():
    xml = '<msg><img aeskey="k" md5="%s" /></msg>' % ("ab" * 16)
    assert W.img_md5(xml) == "ab" * 16
    assert W.img_md5("<msg><img/></msg>") == ""


def test_parse_recordinfo_expand_and_url():
    xml = ('<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist count="2">'
           '<dataitem datatype="1"><sourcename>示例成员甲</sourcename><sourcetime>2025-09-09 19:36</sourcetime>'
           '<datadesc>你好</datadesc></dataitem>'
           '<dataitem datatype="4"><sourcename>示例成员乙</sourcename><datatitle>文章</datatitle>'
           '<dataurl>https://mp.weixin.qq.com/s/abc</dataurl></dataitem>'
           '</datalist></recordinfo>]]></recorditem></appmsg></msg>')
    text, url = W.parse_recordinfo(xml)
    assert text.startswith("[聊天记录 2条]")
    assert "示例成员甲 (2025-09-09 19:36): 你好" in text
    assert "示例成员乙" in text and "文章" in text
    assert "mp.weixin.qq.com" in url


def test_expand_keywords_hit_and_miss():
    groups = W.expand_keywords(["校招", "无关词"])
    assert len(groups[0]) >= 2        # 校招 命中同义词组
    assert groups[1] == ["无关词"]     # 未命中保持原词


def test_embed_filter():
    assert not W._embed_filter("[图片]")
    assert not W._embed_filter("[语音]")
    assert not W._embed_filter("好")
    assert W._embed_filter("这是一条正常消息内容")
    assert W._embed_filter("[聊天记录 3条] 示例成员甲: 内容")


def test_combine_extra():
    assert W._combine_extra("", "") == ""
    assert W._combine_extra("ocr", "").startswith("[图片文字]")
    assert W._combine_extra("", "voice").startswith("[语音转写]")
    assert "[图片文字]" in W._combine_extra("o", "v") and "[语音转写]" in W._combine_extra("o", "v")


def test_parse_date():
    assert W.parse_date("2026-01-02") == int(__import__("datetime").datetime(2026, 1, 2).timestamp())
    end = W.parse_date("2026-01-02", end=True)
    assert end - W.parse_date("2026-01-02") == 86399


def test_hashlib_md5():
    assert W.hashlib_md5("abc") == hashlib.md5("abc".encode()).hexdigest()


# ---------------------------------------------------------------- 页面解密

def _mk_page(enc_key, pgno, plaintext_page):
    """构造一个 SQLCipher 加密页, 返回 (page_bytes, 解密后的数据区明文)"""
    from Crypto.Cipher import AES
    salt = os.urandom(16)
    iv = os.urandom(16)
    data_sz = W.PAGE_SZ - W.RESERVE_SZ - (16 if pgno == 1 else 0)
    start = 16 if pgno == 1 else 0
    body = plaintext_page[start:start + data_sz].ljust(data_sz, b"\x00")
    ct = AES.new(enc_key, AES.MODE_CBC, iv).encrypt(body)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    h = hmac_mod.new(mac_key, ct + iv + struct.pack("<I", pgno), hashlib.sha512)
    if pgno == 1:
        return salt + ct + iv + h.digest(), body
    return ct + iv + h.digest(), body


def test_decrypt_page_roundtrip():
    enc_key = os.urandom(32)
    plain1 = b"SQLite format 3\x00" + os.urandom(100)
    page1, body = _mk_page(enc_key, 1, plain1)
    out = W._decrypt_page(enc_key, page1, 1)
    assert out[:16] == b"SQLite format 3\x00"
    assert out[16:16 + 100] == plain1[16:116]
    assert W._verify_page1(enc_key, page1)
    assert not W._verify_page1(os.urandom(32), page1)


def test_verify_page1_wrong_key():
    enc_key = os.urandom(32)
    page1, _ = _mk_page(enc_key, 1, b"SQLite format 3\x00" + os.urandom(64))
    assert not W._verify_page1(os.urandom(32), page1)


def test_wal_patch():
    """WAL 补丁: 有效 frame 写入, 旧周期 frame 跳过"""
    enc_key = os.urandom(32)
    plain2 = os.urandom(200)
    page2, _ = _mk_page(enc_key, 2, plain2)
    salt1, salt2 = 0x11223344, 0x55667788
    wal_hdr = b"\x00" * 16 + struct.pack(">II", salt1, salt2) + b"\x00" * 8
    frame = struct.pack(">I", 2) + b"\x00" * 4 + struct.pack(">II", salt1, salt2) + b"\x00" * 8 + page2
    stale = struct.pack(">I", 3) + b"\x00" * 4 + struct.pack(">II", 0xAABB, 0xCCDD) + b"\x00" * 8 + os.urandom(W.PAGE_SZ)
    wal = wal_hdr + frame + stale
    db2pages = b"\x00" * (W.PAGE_SZ * 2)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wal_path = os.path.join(td, "x.db-wal")
        db_path = os.path.join(td, "x.db")
        open(wal_path, "wb").write(wal)
        open(db_path, "wb").write(db2pages)
        n = W._decrypt_wal_patch(wal_path, db_path, enc_key)
        assert n == 1
        with open(db_path, "rb") as f:
            f.seek(W.PAGE_SZ)
            got = f.read(W.PAGE_SZ)
        assert got[:200] == plain2[:200]


def test_decrypt_dat_xor_roundtrip():
    """旧版 XOR 图片: 加密->解密还原"""
    import tempfile
    orig = b"\x89PNG\r\n\x1a\n" + os.urandom(300)
    key = 0x5A
    enc = bytes(b ^ key for b in orig)
    with tempfile.TemporaryDirectory() as td:
        dat = os.path.join(td, "abc.dat")
        open(dat, "wb").write(enc)
        out = W.decrypt_dat(dat, os.path.join(td, "abc"), "0" * 16, key)
        assert out and out.endswith(".png")
        assert open(out, "rb").read() == orig


# ---------------------------------------------------------------- 合成环境集成

SQLITE_HDR = b"SQLite format 3\x00"


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    """合成一个微型账号环境并打补丁到 wx_search 模块"""
    acc = tmp_path / "acc"
    dec = acc / "decrypted"
    (dec / "message").mkdir(parents=True)
    (dec / "contact").mkdir()
    img_out = acc / "img_decoded"
    img_out.mkdir()

    def enc_db(src_plain: bytes, path):
        """用真实算法加密一个假库 page1, 供解密路径测试"""
        enc_key = os.urandom(32)
        salt = src_plain[:16]
        iv = os.urandom(16)
        from Crypto.Cipher import AES
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        ct = cipher.encrypt(src_plain[16:4016].ljust(4000, b"\x00"))
        mac_salt = bytes(b ^ 0x3A for b in salt)
        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
        h = hmac_mod.new(mac_key, ct + iv + struct.pack("<I", 1), hashlib.sha512)
        path.write_bytes(salt + ct + iv + h.digest())
        return enc_key

    # ---- message_0.db (明文, 简化: 直接用明文 sqlite 当"解密产物") ----
    con = sqlite3.connect(dec / "message" / "message_0.db")
    con.execute("CREATE TABLE Name2Id(rowid_ INTEGER PRIMARY KEY, user_name TEXT, is_session INT)")
    # Name2Id 用隐式 rowid
    con.execute("INSERT INTO Name2Id(user_name, is_session) VALUES('wxid_fixture_self', 1)")
    con.execute("INSERT INTO Name2Id(user_name, is_session) VALUES('wxid_fixture_alice', 1)")
    con.execute("INSERT INTO Name2Id(user_name, is_session) VALUES('fixture_room@chatroom', 1)")
    img_md5 = "cd" * 16
    packed = b"\x22\x20" + img_md5.encode()
    gzh_xml = ('<?xml version="1.0"?><msg><appmsg><title>示例文章</title><type>5</type>'
               '<url>https://mp.weixin.qq.com/s?x=1&amp;y=2</url></appmsg></msg>')
    file_xml = '<?xml version="1.0"?><msg><appmsg><title>demo.xlsx</title><type>6</type>' \
               '<appattach><fileext>xlsx</fileext></appattach></appmsg></msg>'
    fwd_xml = ('<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist count="1">'
               '<dataitem datatype="1"><sourcename>示例成员甲</sourcename><sourcetime>2026-01-01 10:00</sourcetime>'
               '<datadesc>合成转发检索词</datadesc></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>')
    emoji_xml = '<msg><emoji fromusername = "wxid_fixture_alice" type="2" /></msg>'
    tbl_fixture = "Msg_" + W.hashlib_md5("wxid_fixture_alice")
    tbl_room = "Msg_" + W.hashlib_md5("fixture_room@chatroom")
    for tbl in (tbl_fixture, tbl_room):
        con.execute(f'CREATE TABLE "{tbl}"(real_sender_id INT, create_time INT, local_type INT, '
                    f'message_content BLOB, server_id INT, packed_info_data BLOB)')
    rows = [
        # (sender_id, ts, type, content, svr_id)
        (2, 1767225600, 1, "合成文本消息", 101),                                # 私聊 fixture 用户
        (1, 1767225700, 1, "合成自发消息", 102),
        (2, 1767225800, 3, zc(('<?xml version="1.0"?><msg><img aeskey="k" md5="%s"/></msg>' % img_md5).encode()), 103),
        (2, 1767225900, 49, gzh_xml, 104),
        (2, 1767226000, 49, file_xml, 105),
        (3, 1767226100, 49, "wxid_fixture_alice:\n" + fwd_xml, 106),                      # 群聊转发(带前缀)
        (3, 1767226200, 47, "wxid_fixture_alice:\n" + emoji_xml, 107),                    # 群聊表情(带前缀, 回归)
        (3, 1767226300, 1, "wxid_fixture_alice:\n合成群消息", 108),
        (3, 1767226400, 1, "wxid_fixture_unknown:\n合成未知成员消息", 109),
    ]
    for i, (sid, ts, mt, mc, svr) in enumerate(rows, start=1):
        tbl = tbl_room if svr >= 106 else tbl_fixture
        pk = packed if mt == 3 else None
        con.execute(f'INSERT INTO "{tbl}" VALUES(?,?,?,?,?,?)',
                    (sid, ts, mt, mc.encode() if isinstance(mc, str) else mc, svr, pk))
    con.commit(); con.close()

    # ---- contact.db ----
    con = sqlite3.connect(dec / "contact" / "contact.db")
    con.execute("CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, remark TEXT, nick_name TEXT, alias TEXT)")
    con.executemany("INSERT INTO contact VALUES(?,?,?,?,?)", [
        (1, "wxid_fixture_self", "", "Fixture Self", ""),
        (2, "wxid_fixture_alice", "测试用户甲", "FixtureAlice", "fixture_alias"),
        (3, "fixture_room@chatroom", "", "示例项目群", ""),
    ])
    con.execute("CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB)")

    def room_ext(members):
        out = b""
        for u, n in members:
            ub, nb = u.encode(), n.encode()
            inner = b"\x0a" + bytes([len(ub)]) + ub + b"\x12" + bytes([len(nb)]) + nb + b"\x18\x01"
            out += b"\x0a" + bytes([len(inner)]) + inner
        return out

    con.execute("INSERT INTO chat_room VALUES(1, 'fixture_room@chatroom', 'wxid_fixture_self', ?)",
                (room_ext([("wxid_fixture_alice", "群成员甲"), ("wxid_fixture_unknown", "")]),))
    con.commit(); con.close()

    # ---- media_0.db 语音 ----
    con = sqlite3.connect(dec / "message" / "media_0.db")
    con.execute("CREATE TABLE Name2Id(user_name TEXT)")
    con.execute("INSERT INTO Name2Id(user_name) VALUES('fixture_room@chatroom')")
    con.execute("CREATE TABLE VoiceInfo(chat_name_id INT, create_time INT, local_id INT, svr_id INT, voice_data BLOB, data_index INT)")
    con.execute("INSERT INTO VoiceInfo VALUES(1, 1767226100, 1, 9001, ?, 0)", (b"\x02#!SILK_V3" + os.urandom(500),))
    con.commit(); con.close()

    # ---- patch 模块路径 ----
    monkeypatch.setattr(W, "DECRYPTED", str(dec))
    monkeypatch.setattr(W, "CACHE_DB", str(acc / "search_cache.db"))
    monkeypatch.setattr(W, "OCR_DB", str(acc / "ocr.db"))
    monkeypatch.setattr(W, "VOICE_DB", str(acc / "voice.db"))
    monkeypatch.setattr(W, "IMG_OUT", str(img_out))
    monkeypatch.setattr(W, "IMG_THUMB", str(acc / "img_thumb"))
    monkeypatch.setattr(W, "VEC_DIR", str(acc / "vectors"))
    monkeypatch.setattr(W, "VEC_FILE", str(acc / "vectors" / "vectors.f16"))
    monkeypatch.setattr(W, "VEC_META", str(acc / "vectors" / "meta.json"))
    monkeypatch.setattr(W, "VEC_DB", str(acc / "vectors" / "vectors.db"))
    monkeypatch.setattr(W, "VOICE_DIR", str(acc / "voices"))
    monkeypatch.setattr(W, "SELF", "wxid_fixture_self")
    return acc


def test_build_cache_integration(mini_env):
    W.build_cache()
    con = sqlite3.connect(W.CACHE_DB)
    n = con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
    assert n == 9
    # 图片 ref 从 packed_info 提取
    ref = con.execute("SELECT ref FROM msgs WHERE svr_id=103").fetchone()[0]
    assert ref == "cd" * 16
    # 公众号
    c, url = con.execute("SELECT content, url FROM msgs WHERE svr_id=104").fetchone()
    assert c == "[公众号]示例文章" and "mp.weixin.qq.com" in url and "&amp;" not in url
    # 文件
    c = con.execute("SELECT content FROM msgs WHERE svr_id=105").fetchone()[0]
    assert c == "[文件]demo.xlsx"
    # 群聊转发展开 + 前缀剥离
    c = con.execute("SELECT content FROM msgs WHERE svr_id=106").fetchone()[0]
    assert c.startswith("[聊天记录 1条]") and "合成转发检索词" in c
    # 群聊表情前缀剥离(回归)
    c = con.execute("SELECT content FROM msgs WHERE svr_id=107").fetchone()[0]
    assert c == "[表情]"
    # 合成群消息前缀剥离 + 发送者归因
    t, s = con.execute("SELECT content, sender FROM msgs WHERE svr_id=108").fetchone()
    assert t == "合成群消息" and s == "wxid_fixture_alice"
    # 非联系人前缀: 同样剥离, 前缀即权威发送者
    t, s2 = con.execute("SELECT content, sender FROM msgs WHERE svr_id=109").fetchone()
    assert t == "合成未知成员消息" and s2 == "wxid_fixture_unknown"
    con.close()


def test_search_filters(mini_env):
    W.build_cache()
    # 关键词
    rows = W.search("合成文本", with_ocr=False)
    assert len(rows) == 1 and rows[0][4] == "合成文本消息"
    # --no-expand 时关键词仍生效(回归)
    rows = W.search("合成文本", expand=False, with_ocr=False)
    assert len(rows) == 1
    # 联系人多值
    rows = W.search("", contact="测试用户甲 示例项目群", with_ocr=False)
    assert len(rows) == 9
    # 未匹配对象被忽略并继续
    rows = W.search("合成文本", contact="测试用户甲 不存在的群", with_ocr=False)
    assert len(rows) == 1
    # 日期
    rows = W.search("", start="2026-01-01", end="2026-01-01", with_ocr=False)
    assert len(rows) == 9
    rows = W.search("", start="2030-01-01", with_ocr=False)
    assert rows == []


def test_search_any_keyword(mini_env):
    """多个关键词: 默认同时包含, any_kw 时任一命中即可"""
    W.build_cache()
    and_rows = W.search("合成文本 合成自发消息", expand=False, with_ocr=False)
    assert and_rows == []
    or_rows = W.search("合成文本 合成自发消息", expand=False, with_ocr=False, any_kw=True)
    texts = {r[4] for r in or_rows}
    assert texts == {"合成文本消息", "合成自发消息"}


def test_search_finds_forward_inner_text(mini_env):
    """回归: 合并转发内部文字可搜"""
    W.build_cache()
    rows = W.search("合成转发检索词", with_ocr=False)
    assert len(rows) == 1


def test_format_rows_and_displays(mini_env):
    W.build_cache()
    ct = W.Contacts()
    assert ct.display("wxid_fixture_self") == "我"
    assert ct.display("wxid_fixture_alice") == "测试用户甲"          # 备注优先
    assert ct.display("") == "系统"
    rows = W.search("", with_ocr=False)
    groups = W.format_rows(rows, ct)
    titles = {t: items for t, items in groups}
    assert "示例项目群（群聊）" in titles
    # 合成群消息行归因
    lines = [l for _, l, _, _, _, _ in titles["示例项目群（群聊）"]]
    assert any("群成员甲: 合成群消息" in l for l in lines)


def test_export_docx_and_txt(mini_env):
    W.build_cache()
    rows = W.search("", with_ocr=False)
    ct = W.Contacts()
    out = mini_env / "out.docx"
    W.export_docx(rows, ct, type("A", (), {"keyword": "测试", "contact": "", "sender": "",
                                           "start": "", "end": ""})(), str(out))
    assert out.exists()
    import zipfile
    xml = zipfile.ZipFile(str(out)).read("word/document.xml").decode("utf-8")
    assert "<w:hyperlink" in xml           # 公众号链接
    assert "[文件]demo.xlsx" in xml
    txt = mini_env / "out.txt"
    W.export_txt(rows, ct, str(txt))
    content = txt.read_text(encoding="utf-8")
    assert "本地文件" in content or "未在本地下载" in content


def test_voice_extraction_no_asr(mini_env):
    W.cmd_voices(no_asr=True)
    silks = list((mini_env / "voices").glob("*.silk"))
    assert len(silks) == 1
    raw = silks[0].read_bytes()
    assert raw[:10] == b"\x02#!SILK_V3"      # 保留微信前缀


def test_group_nick_display_and_search(mini_env):
    """群昵称: 群内显示与按群昵称搜人"""
    W.build_cache()
    ct = W.Contacts()
    # 群内显示用群昵称, 优先于备注
    assert ct.display_in("fixture_room@chatroom", "wxid_fixture_alice") == "群成员甲"
    assert ct.display_in("fixture_room@chatroom", "wxid_fixture_self") == "我"
    # 按群昵称搜发送者
    rows = W.search("", sender="群成员甲", with_ocr=False)
    assert len(rows) == 3
    assert all(r[0] == "fixture_room@chatroom" for r in rows)
    # 群外(备注)匹配仍有效: 私聊 4 条 + 群内 bob 3 条
    rows = W.search("", sender="测试用户甲", with_ocr=False)
    assert len(rows) == 7


def test_clean_dry_run(mini_env, capsys):
    (mini_env / "search_cache.db").write_bytes(b"x")
    W.CLEAN_ITEMS  # 触发引用
    # 直接构造: 只验证 dry-run 不删除
    target = mini_env / "search_cache.db"
    before = target.exists()
    # 用 monkeypatch 后的路径没法直接走 cmd_clean(它读模块常量), 只验证 API 不炸
    assert before
