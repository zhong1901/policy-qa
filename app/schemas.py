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
