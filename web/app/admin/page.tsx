import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { AdminClient } from './AdminClient';

/**
 * 管理画面。
 *
 * 権限チェックはサーバー側（current_manager）で行う。ここでの表示制御は
 * 導線を隠すだけで、これに頼らない。
 */
export default async function AdminPage() {
  const token = (await cookies()).get('api_token')?.value;
  if (!token) redirect('/login');

  return (
    <main className="app wide">
      <header className="app-header">
        <h1>管理</h1>
        <nav>
          <a href="/dial">架電画面へ</a>
        </nav>
      </header>
      <AdminClient />
    </main>
  );
}
