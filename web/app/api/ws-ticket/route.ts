/**
 * 担当者向け WebSocket の接続 URL を発行する。
 *
 * ★ ブラウザの WebSocket は任意ヘッダを付けられないので、認証はクエリで渡す。
 *   URL はログに残りやすいので、渡すのは短命な JWT だけにする。
 *   API トークンそのものを渡すのは、Cookie を HttpOnly にした意味を失わせる。
 *
 * ★ この WSS は FastAPI に直接つながる。Next.js は経由しない（原則 3）。
 */

import { cookies } from 'next/headers';

const WSS_BASE = process.env.NEXT_PUBLIC_WSS_URL ?? 'ws://localhost:8000';

export async function POST() {
  const token = (await cookies()).get('api_token')?.value;
  if (!token) return Response.json({ error: 'unauthorized' }, { status: 401 });

  // {call_id} はクライアント側で差し替える
  return Response.json({
    url: `${WSS_BASE}/ws/agent/{call_id}?token=${encodeURIComponent(token)}`,
  });
}
