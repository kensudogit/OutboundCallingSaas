/**
 * バックエンド API クライアント。
 *
 * ★ 発信の可否判断をここに書かない。関門はサーバー側の can_call() 1 箇所だけ。
 *   クライアントで先回りしてボタンを無効化するのは UX として構わないが、
 *   それは判断ではなく表示。403 が返ることを前提に組む。
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: unknown,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
    });
  } catch {
    throw new ApiError(0, 'network_error', null, '通信に失敗しました');
  }

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : {};

  if (!res.ok) {
    const detail = (body as { detail?: unknown }).detail;
    const code =
      typeof detail === 'object' && detail !== null && 'reason' in detail
        ? String((detail as { reason: string }).reason)
        : typeof detail === 'string'
          ? detail
          : 'unknown_error';
    throw new ApiError(res.status, code, detail, code);
  }
  return body as T;
}

// ---------------------------------------------------------------- types

export type QueueItem = {
  contact_id: string;
  phone_e164: string;
  company_name: string | null;
  person_name: string | null;
  reservation_expires_at: string;
  previous_calls: { started_at: string; raw_status: string; disposition: string | null; note: string | null }[];
};

export type PlacedCall = { call_id: string; status: string };

export type NextUp = {
  contact_id: string;
  phone_e164: string;
  company_name: string | null;
  person_name: string | null;
  delay_sec: number;
};

export type DispositionResult = {
  status: string;
  next: NextUp | null;
  queue_empty?: boolean;
};

export type MyStats = {
  attempts: number;
  connected: number;
  human_connected: number;
  appointments: number;
  refusals: number;
  avg_talk_sec: number;
  remaining_contacts: number;
};

// ---------------------------------------------------------------- endpoints

export const api = {
  login: (email: string, password: string) =>
    request<{ token: string; user: { id: string; email: string; displayName: string; role: string } }>(
      '/api/auth/login',
      { method: 'POST', body: JSON.stringify({ email, password }) },
    ),

  nextContact: (listId: string) =>
    request<QueueItem | null>(`/api/queue/next?list_id=${encodeURIComponent(listId)}`),

  skip: (contactId: string) =>
    request<void>('/api/queue/skip', {
      method: 'POST',
      body: JSON.stringify({ contact_id: contactId }),
    }),

  placeCall: (contactId: string) =>
    request<PlacedCall>('/api/calls', {
      method: 'POST',
      body: JSON.stringify({ contact_id: contactId }),
    }),

  registerDisposition: (callId: string, dispositionCode: string, note?: string) =>
    request<DispositionResult>(`/api/calls/${callId}/disposition`, {
      method: 'POST',
      body: JSON.stringify({ disposition_code: dispositionCode, note: note ?? null }),
    }),

  myStats: () => request<MyStats>('/api/stats/me'),
};

/**
 * 関門で止まったときの案内。
 *
 * ★ 全部を「発信できません」にすると、担当者は原因が分からず、管理者に聞き、
 *   管理者も分からない。理由コードごとに、次に何をすればよいかを書く。
 */
export function blockedMessage(error: ApiError): string {
  const detail = error.detail as { reason?: string; detail?: string; retry_after?: string } | null;
  const retryAt = detail?.retry_after
    ? new Date(detail.retry_after).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })
    : null;

  switch (error.code) {
    case 'dnc':
      return 'この相手は架電をお断りされています。発信できません。';
    case 'outside_hours':
      return retryAt
        ? `架電可能時間外です。${retryAt} 以降に再開できます。`
        : '架電可能時間外です。';
    case 'recently_called':
      return '直前に発信済みです。少し待ってからお試しください。';
    case 'daily_limit':
      return retryAt ? `本日の架電上限に達しています。${retryAt} 以降に可能です。` : '本日の架電上限に達しています。';
    case 'total_limit':
      return 'この相手への架電回数が上限に達しています。リストから除外されます。';
    case 'no_reservation':
      return '対象が確保されていません。「次の相手」を取り直してください。';
    case 'invalid_number':
      return '電話番号の形式が不正です。管理者に連絡してください。';
    case 'contact_inactive':
      return 'この相手は架電対象外になっています。';
    case 'dialing_disabled':
      return '現在システム全体で発信を停止しています。';
    case 'provider_unavailable':
      return (detail as unknown as { uncertain?: boolean })?.uncertain
        ? '発信できたか確認中です。二重にかけないよう、しばらく待ってから履歴をご確認ください。'
        : '電話網に接続できませんでした。しばらくしてからお試しください。';
    case 'network_error':
      return '通信が中断されました。発信状況を履歴でご確認ください。';
    default:
      return '発信できませんでした。しばらくしてからお試しください。';
  }
}
