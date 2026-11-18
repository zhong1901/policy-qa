"""应用配置：用 pydantic-settings 从 .env 读取并做类型校验。

设计取舍：
- 所有字段都给默认值，唯一例外是「数据库连接串」——没有它服务本身就没意义。
- API key 一律允许为空：没有 key 时服务照样能启动、/health 和 /api/search 的错误
  路径也能测，只在真正发起上游调用时才报错。这样调试顺序不会卡在「配置不齐起不来」上。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。字段名和 .env 里的 key 一一对应。"""

    # 数据库连接串（asyncpg 用）
    database_url: str = "postgresql://policy:policy@localhost:5432/policy"

    # ---- 生成侧大模型（可选：不配也能跑检索）----
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "deepseek-v4-pro"
    # DeepSeek V4 系列是推理模型：reasoning_tokens 会先吃掉 max_tokens，
    # 给得太小会拿到空 content（实测 60 个 token 全被 reasoning 用光）。
    # 结构化答案 + 思考过程，2000 是安全下限。
    llm_max_tokens: int = 4000
    # 推理模型首字延迟高，超时放到 90s；演示时如果太慢可换 deepseek-flash
    llm_timeout: float = 90.0

    # ---- 向量化模型（检索依赖；不配则 /api/search 返回 503）----
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    # bge-m3 是 1024 维。换模型必须同步改这里，并重建 embedding 列。
    embedding_dim: int = 1024

    # ---- 检索/生成参数 ----
    refusal_threshold: float = 0.30
    top_k_vector: int = 30
    top_k_keyword: int = 30
    top_k_final: int = 5
    citation_min_sim: float = 0.90

    # ---- 前端跨域（本地开发用；生产同源部署可留空）----
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ---- 访问口令（公网部署必填；本地留空则完全开放）----
    access_code: str | None = None

    # ---- 答案缓存 ----
    cache_enabled: bool = True
    cache_ttl_days: int = 7

    # 降级兜底：向量路不可用时，没有相似度可判，
    # 改用「关键词命中词数」作为证据强度，低于此值直接拒答。
    degraded_min_keyword_hits: int = 2

    # 生成温度。政策问答要的是可复现，不是文采，所以默认就压得很低。
    # 评测脚本会把它覆盖成 0，用来区分「真退化」和「模型随机抖动」。
    llm_temperature: float = 0.1

    # 统计页口令（和访问口令分开）。留空则统计页不开放。
    stats_key: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # 忽略 .env 里多出来的 key：部署用的 .env 还会带 SITE_ADDRESS、DB_PASSWORD 等无关变量，
        # 如果这里设成 forbid，一个多余变量就会让整个服务启动失败。
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 cors_origins 拆成列表，空字符串过滤掉。"""
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    用 lru_cache 而不是模块级 `settings = Settings()` 有两个好处：
    1. 推迟 .env 的读取时机——只有第一次真正调用时才读文件，`import app.config`
       在没有 .env 时也能成功，方便冒烟测试；
    2. 避免每次调用都重复读文件、重复做字段校验。
    """
    return Settings()
