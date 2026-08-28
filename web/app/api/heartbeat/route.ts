/**
 * 担当者の生存通知を中継する。
 *
 * 20 秒ごとに呼ばれる。途切れるとサーバー側で予約が解放され、他の担当者が
 * その連絡先を取れるようになる（帰宅した担当者の予約が翌朝までリストを
 * 塞ぐのを防ぐ）。
 */

import { cookies } from 'next/headers';

const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

export async function POST() {
  const token = (await cookies()).get('api_token')?.value;
  if (!token) return new Response(null, { status: 401 });

  const res = await fetch(`${API_BASE}/api/heartbeat`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  return new Response(null, { status: res.status });
}
