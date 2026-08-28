import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';

export default async function Home() {
  const token = (await cookies()).get('api_token')?.value;
  redirect(token ? '/dial' : '/login');
}
