"""请求/响应模型：所有接口的 Pydantic 定义，与 OpenAPI 文档一一对应。"""
from datetime import date

from pydantic import BaseModel, Field


class SearchItem(BaseModel):
    """/api/search 单条结果，字段直接对应数据库里的条款。"""

    clause_id: int = Field(..., description="条款主键 id")
    doc_title: str = Field(..., description="所属文档标题")
    clause_no: str | None = Field(None, description="条款号，如「第5条」，可能为空")
    content: str = Field(..., description="条款正文")
    score: float = Field(..., description="融合后的相关度分，越大越相关")
    url: str | None = Field(None, description="原文链接，可能为空")


class Citation(BaseModel):
    """一条引用：既给用户看，也用于前端跳转高亮。"""

    ref: str = Field(..., description="引用编号，如 C1")
    quote: str = Field(..., description="逐字摘录的原文片段")
    clause_id: int = Field(..., description="条款 id，前端据此跳转高亮")
    doc_title: str = Field(..., description="文档标题")
    clause_no: str | None = Field(None, description="条款号")
    effective_from: str | None = Field(None, description="生效日期")
    url: str | None = Field(None, description="原文链接")


class Material(BaseModel):
    """办理所需的一份材料。"""

    name: str
    note: str = ""


class AskRequest(BaseModel):
    """/api/ask 请求体。"""

    question: str = Field(..., min_length=1, max_length=200, description="用户问题")
    as_of: date | None = Field(
        None, description="按哪个时间点判定政策有效性，默认今天"
    )
    session_id: str | None = Field(
        None, max_length=64,
        description="会话 id。带上它才有短期记忆（多轮追问）与长期画像；不带就是单轮问答",
    )


class AskResponse(BaseModel):
    """/api/ask 响应体。refused=true 时除 conclusion 外其余字段为空。"""

    refused: bool = Field(False, description="是否拒答")
    conclusion: str = Field("", description="一句话结论")
    conditions: list[str] = Field(default_factory=list, description="适用条件")
    materials: list[Material] = Field(default_factory=list, description="所需材料")
    process: list[str] = Field(default_factory=list, description="办理步骤")
    deadline: str | None = Field(None, description="截止时间")
    citations: list[Citation] = Field(default_factory=list, description="引用条款")
    confidence: str = Field("low", description="high / medium / low")
    conflicts: list[str] = Field(default_factory=list, description="资料之间的冲突")
    as_of: date = Field(..., description="本次判定所依据的时间点")
    latency_ms: int = Field(0, description="服务端耗时（毫秒）")
    cached: bool = Field(False, description="是否命中答案缓存")
    note: str = Field("", description="补充说明（如资格判定的细节）")


# ────────────────── 资格自测 ──────────────────
class EligibilityRequest(BaseModel):
    """用户画像。字段与前端表单一一对应。"""

    degree: str = Field("本科", description="博士/硕士/本科/大专/其他")
    has_degree_cert: bool = Field(True, description="是否拿到学位证（双证）")
    grad_year: int | None = Field(None, description="毕业年份")
    is_shanghai_school: bool = Field(False, description="是否上海高校毕业")
    social_months: int = Field(0, ge=0, le=600, description="社保累计月数")
    resident_years: float = Field(0.0, ge=0, le=60, description="居住证持有年数")
    title: str = Field("无", description="无/初级/中级/技师/副高/正高/高级")
    tax_paid: bool = Field(True, description="是否依法缴纳个税")
    employer: str = Field("一般企业", description="重点机构/事业单位/一般企业/暂无")
    age: int = Field(25, ge=16, le=80)


class EligibilityCheck(BaseModel):
    label: str
    state: str
    detail: str = ""


class EligibilityCitation(BaseModel):
    clause_id: int
    doc_title: str
    clause_no: str | None = None
    doc_no: str | None = None
    quote: str
    url: str | None = None


class EligibilityMaterial(BaseModel):
    """一项申办材料。text 是条款原文，不改写。"""

    label: str = ""
    text: str


class EligibilityMaterials(BaseModel):
    """一条路径的完整材料清单，全部来自同一条枚举型条款。"""

    clause_id: int
    clause_no: str | None = None
    doc_title: str
    items: list[EligibilityMaterial] = Field(default_factory=list)


class EligibilityVerdict(BaseModel):
    key: str
    name: str
    result: str
    checks: list[EligibilityCheck] = Field(default_factory=list)
    citations: list[EligibilityCitation] = Field(default_factory=list)
    note: str = ""
    extra: dict = Field(default_factory=dict)
    # 申办材料清单。只对配置了材料条款的路径有值，其余为 None。
    materials: EligibilityMaterials | None = None


class EligibilitySummary(BaseModel):
    """一句话回答「我到底能不能落户」。放在结果最上面。"""

    verdict: str
    headline: str
    detail: str = ""
    best_key: str | None = None
    best_name: str | None = None
    gaps: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    points_note: str = ""


class EligibilityResponse(BaseModel):
    summary: EligibilitySummary
    verdicts: list[EligibilityVerdict]
    as_of: date
