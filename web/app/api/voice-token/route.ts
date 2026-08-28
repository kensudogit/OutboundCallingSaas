/**
 * Twilio Voice のアクセストークンを取り次ぐ。
 *
 * ★ Voice トークンは「発信権限そのもの」。localStorage に保存しない。
 *   メモリに持ち、必要なときに取り直す。XSS で盗まれると他人の名義で
 *   電話がかけられる。
 *
 * ★ TwiML App SID と API Key Secret はここから先（サーバー側）にしか無い。
 */

import { cookies } from 'next/headers';

const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

export async function POST() {
  const token = (await cookies()).get('api_token')?.value;
  if (!token) return Response.json({ error: 'unauthorized' }, { status: 401 });

  const res = await fetch(`${API_BASE}/api/voice/token`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });

  return new Response(await res.text(), {
    status: res.status,
    headers: { 'Content-Type': 'application/json' },
  });
}
