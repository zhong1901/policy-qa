"""条款切分器：把一份文档切成语义完整的条款。

核心算法是「条号连续性」——不用标点猜边界，而是要求条款编号严格 1、2、3 递增。
原因：公文里 «本办法第五条规定» 这类引用会被标点规则误切，而真正的条款边界
必然满足序号递增，引用则会让序号跳变。

这个模块是从离线脚本 split_clauses_v2.py 里抽出来的纯函数版本（去掉了 CSV 读写），
算法一个字没改——因为库里 7690 条条款就是用它切出来的，改算法会让新旧条款不可比。
"""
from __future__ import annotations

import re

MAXLEN = 1000
MINLEN = 20
# 整篇当一条时的下限要更低。
# 「上海的高温津贴发几个月？」的答案只有 14 个字，但它是一句完整的回答；
# 用 20 字的门槛会把它丢掉，用户就问不到了。20 字是为「条款」定的——
# 条款里 20 字以下多半是被切断的碎片，而短问答不是。
MINLEN_WHOLE = 8
CN = "零一二三四五六七八九十百两〇"

RE_ART = re.compile(r"第([" + CN + r"]{1,6})条")
RE_CHAP = re.compile(r"第([" + CN + r"]{1,6})(章|节)")


def cn2int(s: str) -> int:
    s = s.replace("〇", "零").replace("两", "二")
    if not s:
        return -1
    if "百" in s:
        a, _, b = s.partition("百")
        h = cn2int(a) if a else 1
        rest = cn2int(b) if b else 0
        return h * 100 + rest
    if "十" in s:
        a, _, b = s.partition("十")
        t = (cn2int(a) if a else 1) * 10
        return t + (cn2int(b) if b else 0)
    if len(s) == 1:
        i = CN.find(s)
        return i if i >= 0 else -1
    return -1


def find_markers(text: str) -> list[tuple]:
    m = []
    for mt in RE_CHAP.finditer(text):
        m.append((mt.start(), mt.end(), "chap", f"第{mt.group(1)}{mt.group(2)}"))
    for mt in RE_ART.finditer(text):
        m.append((mt.start(), mt.end(), "art", cn2int(mt.group(1))))
    m.sort()
    return m


def build_articles(text: str) -> list[tuple[int, int, str, str]] | None:
    """按条号连续性切分。返回 [(segment, article_no, chapter, clause_text)]。"""
    marks = find_markers(text)
    art_marks = [x for x in marks if x[2] == "art"]
    if len(art_marks) < 2:
        return None

    accepted = []
    seg = 1
    expected = 1
    last_num = 0
    for pos, end, _, num in art_marks:
        if num < 1:
            continue
        if num == 1 and last_num >= 2:
            # 同一文件里出现第二部「办法」，重新开始编号
            seg += 1
            accepted.append((pos, end, 1, seg))
            expected, last_num = 2, 1
            continue
        if num == expected:
            accepted.append((pos, end, num, seg))
            last_num = num
            expected = num + 1
        # 其余视为正文里的引用，忽略

    if len(accepted) < 2:
        return None

    chap_pos = [(p, lab) for p, e, t, lab in marks if t == "chap"]

    out = []
    pre = text[:accepted[0][0]].strip()
    pre = re.sub(r"^.{0,60}?(办法|规定|细则|通知)\s*", "", pre).strip()
    if len(pre) > 40:
        out.append((1, 0, "", pre))
    for i, (pos, end, num, sg) in enumerate(accepted):
        nxt = accepted[i + 1][0] if i + 1 < len(accepted) else len(text)
        body = text[pos:nxt].strip()
        body = re.sub(r"^\s*第[" + CN + r"]{1,6}[章节][^。；\n]{0,20}", "", body)
        chap = ""
        for cp, lab in chap_pos:
            if cp < pos:
                chap = lab
            else:
                break
        out.append((sg, num, chap, body))
    return out


def split_enum(text: str) -> list[tuple] | None:
    """一、二、三 式：通知 / 政策问答。"""
    pat = re.compile(r"(?:(?<=[。；！？：）\n])|^) ?([一二三四五六七八九十]{1,3}、)")
    marks = [(m.start(), m.end(), m.group(1)) for m in pat.finditer(text)]
    if len(marks) < 2:
        return None
    out = []
    pre = text[:marks[0][0]].strip()
    if len(pre) > 40:
        out.append((1, 0, "", pre, "前言"))
    for i, (p, e, lab) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((1, i + 1, "", text[p:nxt].strip(), lab))
    return out


def split_long(t: str) -> list[str]:
    if len(t) <= MAXLEN:
        return [t]
    parts = re.split(r"(?<=[。；]) ?(?=[（(][一二三四五六七八九十]+[）)])", t)
    buf, out = "", []
    for p in parts:
        if not p.strip():
            continue
        if len(buf) + len(p) <= MAXLEN:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    fin = []
    for p in out:
        while len(p) > MAXLEN * 1.6:
            cut = p.rfind("。", 0, MAXLEN)
            cut = cut if cut > 200 else MAXLEN
            fin.append(p[:cut + 1])
            p = p[cut + 1:]
        if p.strip():
            fin.append(p)
    return fin or [t]


def normalize(text: str) -> str:
    t = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s+", " ", t).strip()


def split_document(body: str) -> tuple[str, list[dict]]:
    """把一篇文档切成语义完整的条款。

    返回 (mode, clauses)。mode 取值：
      article 按条切（最理想）
      enum    按「一、二、三、」切
      whole   整篇当一条（兜底）
    """
    body = normalize(body)
    arts = build_articles(body)
    out: list[dict] = []

    if arts:
        mode = "article"
        for sg, num, chap, txt in arts:
            txt = txt.strip()
            if len(txt) < MINLEN:
                continue
            if num == 0:
                no, path, lvl = "前言", "前言", "preamble"
            else:
                no = f"第{num}条"
                path = f"{chap}/{no}" if chap else no
                if sg > 1:
                    path = f"段{sg}/{path}"
                lvl = "article"
            for i, p in enumerate(split_long(txt), 1):
                out.append({
                    "segment": sg, "clause_no": no, "level": lvl,
                    "clause_path": path if i == 1 else f"{path}#{i}",
                    "content": p,
                })
        return mode, out

    ens = split_enum(body)
    if ens:
        mode = "enum"
        for sg, idx, chap, txt, lab in ens:
            txt = re.sub(r"^[一二三四五六七八九十]{1,3}、", "", txt).strip()
            if len(txt) < MINLEN:
                continue
            for i, p in enumerate(split_long(txt), 1):
                out.append({
                    "segment": 1, "clause_no": lab, "level": "enum",
                    "clause_path": lab if i == 1 else f"{lab}#{i}",
                    "content": p,
                })
        return mode, out

    mode = "whole"
    if len(body) >= MINLEN_WHOLE:
        for i, p in enumerate(split_long(body), 1):
            out.append({
                "segment": 1, "clause_no": "全文", "level": "whole",
                "clause_path": "全文" if i == 1 else f"全文#{i}",
                "content": p,
            })
    return mode, out
