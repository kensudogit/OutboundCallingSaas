/**
 * FastAPI への中継（BFF）。
 *
 * ★ Next.js が担うのは認証セッションの保持と REST の中継だけ。
 *   音声（WebRTC）とリアルタイムイベント（WSS）はここを通さない。
 *   Route Handler は短命な関数で常時接続の中継に向かず、デプロイのたびに
 *   全通話の文字起こしが切れる。
 *
 * ★ API トークンは HttpOnly Cookie に置き、ブラウザの JS からは触らせない。
 */

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';

const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

// 中継しないパス。ここに載っているものは専用のハンドラが処理する
const BLOCKED = new Set(['voice-token', 'ws-ticket', 'heartbeat']);

async function forward(req: NextRequest, segments: string[]): Promise<Response> {
  const head = segments[0];
  if (head === undefined || BLOCKED.has(head)) {
    return Response.json({ error: 'not_found' }, { status: 404 });
  }

  const token = (await cookies()).get('api_token')?.value;
  if (!token) return Response.json({ error: 'unauthorized' }, { status: 401 });

  const url = new URL(`/api/${segments.join('/')}`, API_BASE);
  url.search = req.nextUrl.search;

  const res = await fetch(url, {
    method: req.method,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': req.headers.get('content-type') ?? 'application/json',
    },
    body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text(),
    cache: 'no-store',
  });

  return new Response(res.body, {
    status: res.status,
    headers: { 'Content-Type': res.headers.get('content-type') ?? 'application/json' },
  });
}

type Ctx = { params: Promise<{ proxy: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).proxy);
}
export async function POST(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).proxy);
}
export async function PATCH(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).proxy);
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  return forward(req, (await ctx.params).proxy);
}
