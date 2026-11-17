"""向量化：把 query 文本转成 1024 维向量，调 OpenAI 兼容的 embedding API。"""
import logging

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings

logger = logging.getLogger(__name__)

# 进程内复用一个 AsyncClient：复用 TCP/TLS 连接，省掉每次请求 50~100ms 的握手开销。
# 生命周期由 main.py 的 lifespan 关闭（close_http_client）。
_client: httpx.AsyncClient | None = None


class EmbeddingNotConfigured(RuntimeError):
    """没有配置 embedding key —— 属于部署配置问题，路由层应转成 503。"""


class EmbeddingFailed(RuntimeError):
    """上游 embedding 服务不可用（网络错误 / 5xx / 返回格式异常）。"""


def get_http_client() -> httpx.AsyncClient:
    """懒创建并复用全局 AsyncClient。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_http_client() -> None:
    """关闭全局 AsyncClient，应用退出时调用。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _is_retryable(exc: BaseException) -> bool:
    """只重试可能自愈的错误。

    4xx（除 429 限流）是请求本身有问题——key 错、模型名错、参数错，重试三次只是把
    同一个错误重复三遍，还多等几秒。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def _post_embedding(text: str, settings: Settings) -> list[float]:
    """真正的 HTTP 调用，被 tenacity 包着。参数校验不在这里做。"""
    url = f"{settings.embedding_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}
    payload = {"model": settings.embedding_model, "input": text}

    resp = await get_http_client().post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["embedding"]


async def embed_query(text: str, settings: Settings) -> list[float]:
    """把单条 query 转成 embedding 向量。

    参数：text 非空文本；settings 含 embedding 的 base_url/api_key/model。
    返回：float 列表（bge-m3 为 1024 维）。
    失败：抛 EmbeddingNotConfigured（没配 key）或 EmbeddingFailed（上游挂了），
         由路由层统一转 503。
    """
    if not text or not text.strip():
        raise ValueError("embedding 输入为空")

    if not settings.embedding_api_key:
        raise EmbeddingNotConfigured(
            "未配置 EMBEDDING_API_KEY，无法做向量检索；请在 .env 中填入后重启服务"
        )

    try:
        vector = await _post_embedding(text, settings)
    except httpx.HTTPStatusError as exc:
        # 把上游的状态码带进错误信息，否则日志里只剩「500」看不出是 key 错还是服务挂
        raise EmbeddingFailed(
            f"embedding 接口返回 {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise EmbeddingFailed(f"embedding 接口不可达: {exc}") from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise EmbeddingFailed(f"embedding 返回格式异常: {exc}") from exc

    # 维度校验前移：配错模型时在调用点就报清楚，而不是等到 SQL 里报
    # "expected 1024 dimensions, not 768" 这种指不到根因的错误
    if len(vector) != settings.embedding_dim:
        raise EmbeddingFailed(
            f"embedding 维度不符：期望 {settings.embedding_dim}，实际 {len(vector)}；"
            f"请检查 EMBEDDING_MODEL 是否与 embedding_dim 匹配"
        )

    logger.debug("embedding 完成，维度=%d", len(vector))
    return vector


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def _post_embedding_batch(texts: list[str], settings: Settings) -> list[list[float]]:
    url = f"{settings.embedding_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}
    payload = {"model": settings.embedding_model, "input": texts}
    resp = await get_http_client().post(url, headers=headers, json=payload, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()["data"]
    # 个别服务不保证顺序，按 index 排序后再取
    data = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


async def embed_texts(
    texts: list[str], settings: Settings, batch_size: int = 16
) -> list[list[float]]:
    """批量向量化。入库时用：一次请求带多条，比逐条快一个数量级。

    为什么要分批：bge-m3 单请求有长度/条数上限，一次塞几百条会被拒或超时。
    16 条一批是实测比较稳的值。
    """
    if not settings.embedding_api_key:
        raise EmbeddingNotConfigured("未配置 EMBEDDING_API_KEY，无法向量化")

    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = [t if t.strip() else "（空）" for t in texts[i:i + batch_size]]
        try:
            vecs = await _post_embedding_batch(chunk, settings)
        except httpx.HTTPStatusError as exc:
            raise EmbeddingFailed(
                f"embedding 接口返回 {exc.response.status_code}: "
                f"{exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingFailed(f"embedding 接口不可达: {exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise EmbeddingFailed(f"embedding 返回格式异常: {exc}") from exc

        if len(vecs) != len(chunk):
            raise EmbeddingFailed(f"embedding 返回条数不符：{len(vecs)} != {len(chunk)}")
        for v in vecs:
            if len(v) != settings.embedding_dim:
                raise EmbeddingFailed(
                    f"embedding 维度不符：期望 {settings.embedding_dim}，实际 {len(v)}")
        results.extend(vecs)
    return results
