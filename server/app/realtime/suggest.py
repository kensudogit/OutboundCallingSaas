"""LLM による切り返しサジェスト。

設計上の要点が 3 つある。

★ 短く出す。担当者は会話中で、画面を読む余裕は 1〜2 秒しかない。
  120 トークン程度、2〜3 行が上限。長い提案は読まれない。

★ 直近数ターンだけを渡す。通話が 20 分を超えると全文はトークンも遅延も膨らみ、
  しかも古い話題に引きずられて精度が落ちる。

★ 「言ってはいけないこと」をテナントのプロンプトに持たせる。サジェストは
  担当者が読み上げる前提なので、LLM の出力がそのまま顧客に伝わる。
  Web の生成文とはリスクの質が違う。
"""

from __future__ import annotations

from ..config import LLM_API_KEY, LLM_MAX_TOKENS, LLM_MODEL
from ..logger import logger

_SYSTEM = """あなたは電話営業の担当者を支援するアシスタントです。
相手の直近の発話に対して、担当者が次に言うとよいことを提案してください。

制約:
- 2 行以内。担当者は通話中で、長い文章を読む時間がありません
- 断定的な効能の保証、値引きの約束、根拠のない比較は書かない
- 相手が明確に断っている場合は、粘る提案ではなく丁寧に終える提案をする
"""


async def suggest_reply(call_id: str, utterance: str) -> str | None:
    """通話中のサジェストを 1 件返す。失敗しても通話には影響させない。"""
    if not LLM_API_KEY:
        return None

    context = await _load_context(call_id)

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=LLM_API_KEY)
        message = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=_SYSTEM + context.get("tenant_prompt", ""),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"直近のやり取り:\n{context['recent']}\n\n"
                        f"相手の直近の発話: {utterance}"
                    ),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warn("LLM の呼び出しに失敗しました", call_id=call_id, err=str(exc))
        return None

    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if text:
        await _record(call_id, utterance, text)
    return text or None


async def _load_context(call_id: str) -> dict[str, str]:
    """直近 6 ターンだけを取る。全文は渡さない。"""
    from ..db.engine import admin_tx

    async with admin_tx() as conn:
        rows = await conn.fetch(
            """
            select track, text from transcript_segments
             where call_id = $1 and source = 'realtime'
             order by started_ms desc
             limit 6
            """,
            call_id,
        )
    recent = "\n".join(
        f"{'相手' if r['track'] == 'inbound' else '担当者'}: {r['text']}" for r in reversed(rows)
    )
    return {"recent": recent, "tenant_prompt": ""}


async def _record(call_id: str, trigger: str, suggestion: str) -> None:
    """採否を後から測れるように残す。プロンプト改善の材料になる。"""
    from ..db.engine import admin_tx

    async with admin_tx() as conn:
        await conn.execute(
            """
            insert into call_suggestions (tenant_id, call_id, trigger_text, suggestion)
            select tenant_id, id, $2, $3 from calls where id = $1
            """,
            call_id,
            trigger,
            suggestion,
        )
