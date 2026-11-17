"""资格自测：把「我这样能不能落户」变成一张表 + 一份判定。

为什么单独做一层，而不是再问一次大模型：
- 落户条件是可枚举的硬规则（居住证满 7 年、社保满 84 个月…），
  交给模型判断会引入不确定性，而且同样的输入两次可能给不同答案；
- 但结论必须可溯源，所以每条规则都绑定一个真实条款 id，
  判定完成后把条款原文取出来一起返回。

一句话：规则负责「算得准」，检索负责「说得清」。

规则来源（均已核对条款原文）：
- 居转户         clause 7371  第五条（申办条件）
- 引进人才落户    clause 7335  第五条（申办条件）
- 应届生落户      clause 7554  一、申请条件
- 居住证积分      clause 7462 第六条（基础指标及分值）+ 7469 第十二条（120 分）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# ── 判定结果的三种状态 ──
MET = "满足"
MISS = "不满足"
UNSURE = "无法判断"
SCORE = "计分"   # 仅用于积分明细：这是分数构成，不是通过/不通过


@dataclass
class Profile:
    """用户画像。全部来自表单，缺一项就少一条判据。"""

    degree: str = "其他"          # 博士 / 硕士 / 本科 / 大专 / 其他
    has_degree_cert: bool = False  # 是否拿到学位证（双证）
    grad_year: int | None = None
    is_shanghai_school: bool = False
    social_months: int = 0
    resident_years: float = 0.0
    title: str = "无"              # 无 / 初级 / 中级 / 副高 / 正高 / 技师 / 高级技师
    tax_paid: bool = True
    employer: str = "一般企业"      # 重点机构 / 事业单位 / 一般企业 / 暂无
    age: int = 25


@dataclass
class Check:
    label: str
    state: str
    detail: str = ""
    hard: bool = False   # True 表示「过期就永久不符合」（如应届身份），不能算「努力一下就行」


@dataclass
class Verdict:
    key: str
    name: str
    result: str                    # 符合 / 不符合 / 待确认
    checks: list[Check] = field(default_factory=list)
    clause_ids: list[int] = field(default_factory=list)
    note: str = ""
    extra: dict = field(default_factory=dict)


# ── 职称档位（用于居转户与积分） ──
_TITLE_RANK = {"无": 0, "初级": 1, "中级": 2, "技师": 2, "副高": 3,
               "高级技师": 3, "正高": 4, "高级": 4}

# 积分：学历分
_DEGREE_SCORE = {"大专": 50, "本科": 60, "硕士": 100, "博士": 110, "其他": 0}
# 积分：职称分
_TITLE_SCORE = {"无": 0, "初级": 0, "中级": 100, "技师": 100,
                "副高": 140, "高级技师": 140, "正高": 140, "高级": 140}


def _age_score(age: int) -> int:
    """年龄指标：最高 30 分；56–60 周岁积 5 分，每减少 1 岁加 2 分，封顶 30。"""
    if age >= 61:
        return 0
    if age >= 56:
        return 5
    return min(30, 5 + (56 - age) * 2)


def _degree_score(p: Profile) -> int:
    base = _DEGREE_SCORE.get(p.degree, 0)
    # 本科无学位只积 60，有学位积 90
    if p.degree == "本科":
        return 90 if p.has_degree_cert else 60
    return base


def evaluate(p: Profile) -> list[Verdict]:
    """返回四条路径的判定。"""
    return [_check_juzhuanhu(p), _check_talent(p), _check_fresh_grad(p), _check_points(p)]


# ────────────────────── 居转户 ──────────────────────
def _check_juzhuanhu(p: Profile) -> Verdict:
    ry = p.resident_years
    sm = p.social_months
    checks = [
        Check("持有《上海市居住证》满 7 年",
              MET if ry >= 7 else MISS,
              f"你填写 {ry:g} 年" + (f"，还差 {7 - ry:g} 年" if ry < 7 else "")),
        Check("持证期间参加本市职工社会保险满 7 年",
              MET if sm >= 84 else MISS,
              f"你填写 {sm} 个月" + (f"，还差 {84 - sm} 个月" if sm < 84 else "")),
        Check("被评聘为中级及以上职称，或具有技师以上职业资格",
              MET if _TITLE_RANK.get(p.title, 0) >= 2 else MISS,
              f"你填写「{p.title}」" + ("" if _TITLE_RANK.get(p.title, 0) >= 2 else "，需要中级职称或技师以上")),
        Check("持证期间依法在本市缴纳个人所得税",
              MET if p.tax_paid else MISS, "" if p.tax_paid else "需提供完税证明"),
    ]
    return _verdict("juzhuanhu", "居转户（居住证转常住户口）", checks, [7371],
                    note="四项须同时满足；符合激励条件（如社保基数达到一定倍数）可缩短年限，本工具未覆盖。")


# ────────────────────── 引进人才落户 ──────────────────────
def _check_talent(p: Profile) -> Verdict:
    # 第五条是「符合下列条件之一」，所以这组是「或」：
    # 满足任一即可，不能把未满足的分支显示成「缺什么」。
    talents = [
        ("博士研究生学历并取得相应学位",
         p.degree == "博士" and p.has_degree_cert,
         f"你填写的是「{p.degree}」" + ("（已取得学位）" if p.has_degree_cert else "（未取得学位）")),
        ("高级专业技术职务任职资格",
         _TITLE_RANK.get(p.title, 0) >= 3,
         f"你填写的是「{p.title}」"),
    ]
    approved = [label for label, ok, _ in talents if ok]
    any_talent = bool(approved)

    checked: list[Check] = []
    if any_talent:
        checked.append(Check(
            "满足以下任一资格：博士学历学位 / 高级职称",
            MET,
            f"你符合「{approved[0]}」"))
    else:
        checked.append(Check(
            "满足以下任一资格：博士学历学位 / 高级职称",
            MISS,
            "；".join(detail for _, _, detail in talents)))

    # 还有「省部级表彰」「人才培养计划」「重点机构紧缺急需人才」等路径，表单采集不到
    unsure_talent = (not any_talent) and p.employer == "重点机构"
    checked.append(Check(
        "或属于省部级表彰 / 人才培养计划 / 重点机构紧缺急需人才",
        UNSURE if unsure_talent else MISS,
        "这类资格取决于单位资质与个人荣誉，表单无法判断，需由单位人事确认" if unsure_talent
        else "你未选择重点机构，也没有博士学历或高级职称"))

    checked.append(Check("在沪工作稳定且依法参加社会保险",
                         MET if p.social_months > 0 else MISS,
                         f"社保 {p.social_months} 个月"))

    if any_talent:
        result = "符合" if p.social_months > 0 else "待确认"
    elif unsure_talent:
        result = "待确认"
    else:
        result = "不符合"

    return _verdict("talent", "引进人才落户", checked, [7335], result=result,
                    note="本路径由用人单位发起申请，个人不能自行申报；最终以人才工作部门审核为准。")


# ────────────────────── 应届生落户 ──────────────────────
def _check_fresh_grad(p: Profile) -> Verdict:
    this_year = date.today().year
    is_fresh = p.grad_year is not None and this_year - p.grad_year <= 1
    checks = [
        Check("属于应届普通高校毕业生（毕业当年或次年）",
              MET if is_fresh else MISS,
              f"你填写的毕业年份是 {p.grad_year}" if p.grad_year else "未填写毕业年份",
              hard=True),   # 一次性窗口：过了就永久不符合，不能算「努力一下就行」
        Check("非上海生源（上海生源本身无需办理落户）",
              MET if not p.is_shanghai_school else UNSURE,
              "上海高校毕业不等于上海生源，需以户籍为准" if p.is_shanghai_school else ""),
        Check("已落实符合条件的上海用人单位",
              MET if p.employer in ("重点机构", "事业单位", "一般企业") else MISS,
              f"你填写的是「{p.employer}」" + ("" if p.employer != "暂无" else "，需先签约上海单位")),
        # 这一项永远无法从表单判断，但必须显式列出来，
        # 否则「待确认」会变成一个没有解释的结论。
        Check("用人单位资质与当年评分标准",
              UNSURE,
              "取决于单位是否在当年受理名录内、以及评分是否达到标准分，需由单位人事确认"),
    ]
    # 三条都满足也不代表一定能办：还要看单位资质和当年打分，所以最高只给「待确认」
    result = "不符合" if any(c.state == MISS for c in checks) else "待确认"

    return _verdict("fresh", "应届毕业生落户", checks, [7554], result=result,
                    note="近年政策每年调整，且需用人单位一次性提交材料、由单位发起申请。"
                         "本工具仅按 2020 年通知的框架判断，具体以当年通知为准。")


# ────────────────────── 居住证积分 ──────────────────────
def _check_points(p: Profile) -> Verdict:
    age_s = _age_score(p.age)
    deg_s = _degree_score(p)
    title_s = _TITLE_SCORE.get(p.title, 0)
    # 条款原文：学历与职称「两项指标选择一项进行积分」，取较高者
    base_s = max(deg_s, title_s)
    social_s = int(p.social_months // 12) * 3
    total = age_s + base_s + social_s

    checks = [
        # 这三项是分数的组成部分，不是通过/不通过，用「计分」状态，避免误解
        Check("年龄指标", SCORE, f"{p.age} 岁 → {age_s} 分（上限 30 分）"),
        Check("学历与职称（二选一取高）", SCORE,
              f"学历 {deg_s} 分、职称 {title_s} 分 → 取 {base_s} 分"),
        Check("缴纳社保年限", SCORE,
              f"{p.social_months} 个月 → {social_s} 分（每满 1 年 3 分）"),
        Check("总积分达到标准分值 120 分",
              MET if total >= 120 else MISS,
              (f"当前估算 {total} 分，达标" if total >= 120
               else f"当前估算 {total} 分，还差 {120 - total} 分")),
    ]
    return _verdict("points", "居住证积分达标（120 分）", checks, [7462, 7469],
                    result="符合" if total >= 120 else "不符合",
                    extra={"score": total,
                           "breakdown": {"age": age_s, "base": base_s, "social": social_s}},
                    note="估算只含基础指标（年龄 / 学历 / 职称 / 社保年限）。"
                         "加分指标（创业人才、紧缺专业、表彰奖励等）与减分项未计入，实际以官方积分为准。")


# ────────────────────── 组装 ──────────────────────
def _verdict(key: str, name: str, checks: list[Check], clause_ids: list[int],
             result: str | None = None, note: str = "", extra: dict | None = None) -> Verdict:
    if result is None:
        states = [c.state for c in checks]
        if MISS in states:
            result = "不符合"
        elif UNSURE in states:
            result = "待确认"
        else:
            result = "符合"
    return Verdict(key=key, name=name, result=result, checks=checks,
                   clause_ids=clause_ids, note=note, extra=extra or {})


# ────────────────────── 总结 ──────────────────────
@dataclass
class Summary:
    """一句话回答「我到底能不能落户」。

    四条路径是「依据」，这个才是「答案」——用户填完表想看的是这句。
    """

    verdict: str                                    # 可以申请 / 可能有戏 / 暂时不符合
    headline: str                                   # 一句话结论
    detail: str = ""                                # 补充说明
    best_key: str | None = None
    best_name: str | None = None
    gaps: list[str] = field(default_factory=list)   # 还差什么
    alternatives: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    points_note: str = ""                           # 积分单独说明（它不是落户路径）


def _met(v: Verdict) -> int:
    return sum(1 for c in v.checks if c.state == MET)


def summarize(verdicts: list[Verdict]) -> Summary:
    # 只把真正的「落户路径」放进结论。
    # 居住证积分达标 ≠ 落户：积分 120 分用于子女教育、医保等公共服务，
    # 早期版本把它算成一条落户路径，导致「刚毕业没工作」也被判成"可以申请"，是错的。
    paths = [v for v in verdicts if v.key != "points"]
    points = next((v for v in verdicts if v.key == "points"), None)

    note = ""
    if points is not None:
        sc = points.extra.get("score")
        if sc is not None:
            note = (f"顺带一提：你的居住证积分估算 {sc} 分，"
                    + ("已达标——但积分用于子女教育等公共服务，不等于能落户。"
                       if sc >= 120 else f"距离达标线 120 分还差 {120 - sc} 分。"))

    eligible = [v for v in paths if v.result == "符合"]
    maybe = [v for v in paths if v.result == "待确认"]

    # ① 至少有一条落户路径走得通
    if eligible:
        best = max(eligible, key=_met)
        others = [v.name for v in eligible if v.key != best.key]
        detail = "可以直接准备材料了。"
        if others:
            detail = f"另外「{'」「'.join(others)}」也符合，可以按办理难度选一条。"
        return Summary(
            verdict="可以申请",
            headline=f"你符合「{best.name}」",
            detail=detail,
            best_key=best.key,
            best_name=best.name,
            alternatives=others,
            suggestions=[f"先看「{best.name}」的申办材料和受理窗口，再决定走哪条。"],
            points_note=note,
        )

    # ② 有路径但关键信息无法判断
    if maybe:
        best = max(maybe, key=_met)
        gaps = [f"{c.label} —— {c.detail}" for c in best.checks if c.state == UNSURE and c.detail]
        return Summary(
            verdict="可能有戏",
            headline=f"最可能的是「{best.name}」，但有信息需要确认",
            detail="下面这几项表单判断不了，需要你或单位人事补充确认：",
            best_key=best.key,
            best_name=best.name,
            gaps=gaps,
            suggestions=["建议直接问单位人事：本单位是否属于可申办落户的重点机构。"],
            points_note=note,
        )

    # ③ 一条都不满足 —— 不武断挑「最接近」，把三条的差距都摆出来。
    # 之前用「满足项占比」选最接近，会把已经永久过期的应届身份判成最接近，误导人。
    lines: list[str] = []
    for v in paths:
        miss = [c for c in v.checks if c.state == MISS]
        if not miss:
            continue
        hard = [c for c in miss if c.hard]
        if hard:
            lines.append(f"{v.name}：已不可能（{hard[0].label}）")
        else:
            lines.append(f"{v.name}：还差 {len(miss)} 项 —— {miss[0].label}")
    soft = [v for v in paths if all(c.state != MISS or not c.hard for c in v.checks)]
    best = soft[0] if soft else paths[0]

    return Summary(
        verdict="暂时不符合",
        headline="目前三条落户路径都还不满足",
        detail="各自的差距如下，看哪条补起来更快：",
        best_key=best.key,
        best_name=best.name,
        gaps=lines,
        suggestions=["补齐上面提到的条件后再回来测一次；"
                     "带「已不可能」的那条不用再考虑。"],
        points_note=note,
    )
