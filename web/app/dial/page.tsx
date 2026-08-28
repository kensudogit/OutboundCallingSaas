import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { DialerClient } from './DialerClient';

/**
 * 架電画面。
 *
 * Server Component が担うのは認証チェックと初期データの取得だけ。
 * Device の状態・WebSocket・タイマーはすべてクライアント状態なので、
 * DialerClient に閉じる。ここを Server 側に寄せようとしても得がない。
 */

const API_BASE = process.env.API_BASE_URL ?? 'http://localhost:8000';

type ContactList = { id: string; name: string };

async function fetchLists(token: string): Promise<ContactList[]> {
  const res = await fetch(`${API_BASE}/api/lists`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  if (!res.ok) return [];
  return (await res.json()) as ContactList[];
}

export default async function DialPage({
  searchParams,
}: {
  searchParams: Promise<{ list?: string }>;
}) {
  const token = (await cookies()).get('api_token')?.value;
  if (!token) redirect('/login');

  const [lists, params] = await Promise.all([fetchLists(token), searchParams]);
  const listId = params.list ?? lists[0]?.id;

  if (!listId) {
    return (
      <main className="app">
        <h1>架電</h1>
        <p className="notice">架電リストがありません。管理者にリストの作成を依頼してください。</p>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="app-header">
        <h1>架電</h1>
        <nav>
          {lists.map((l) => (
            <a key={l.id} href={`/dial?list=${l.id}`} className={l.id === listId ? 'active' : ''}>
              {l.name}
            </a>
          ))}
        </nav>
      </header>
      <DialerClient listId={listId} />
    </main>
  );
}
