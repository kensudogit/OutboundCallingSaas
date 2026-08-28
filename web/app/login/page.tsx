import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';

/**
 * ログイン。
 *
 * ★ API トークンは HttpOnly Cookie に置く。ブラウザの JS からは触れない。
 *   架電 SaaS のトークンは「他社の顧客リストと通話録音への鍵」なので、
 *   localStorage に置くと XSS 一発で全部が出ていく。
 */

const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

async function login(formData: FormData) {
  'use server';

  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: String(formData.get('email') ?? ''),
      password: String(formData.get('password') ?? ''),
    }),
    cache: 'no-store',
  });

  if (!res.ok) redirect('/login?error=1');

  const { token } = (await res.json()) as { token: string };
  (await cookies()).set('api_token', token, {
    httpOnly: true,
    sameSite: 'lax',
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: 60 * 60 * 2,
  });
  redirect('/dial');
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <main className="app auth">
      <form action={login} className="login">
        <h1>ログイン</h1>
        {error && (
          // ユーザーの存在有無を応答で区別しない（サーバー側も同じ応答を返す）
          <p role="alert" className="error">
            メールアドレスまたはパスワードが正しくありません。
          </p>
        )}
        <label>
          メールアドレス
          <input name="email" type="email" autoComplete="username" required />
        </label>
        <label>
          パスワード
          <input name="password" type="password" autoComplete="current-password" required />
        </label>
        <button type="submit">ログイン</button>
      </form>
    </main>
  );
}
