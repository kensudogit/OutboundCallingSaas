/**
 * 管理画面の API クライアント。
 *
 * ★ 取り込みは必ず dry_run を先に通せる形にしてある。数万件を入れる前に
 *   「何件弾かれるか」を見せないと、取り込んでから気付くことになる。
 */

import { ApiError } from './api';

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
      typeof detail === 'object' && detail !== null && 'error' in detail
        ? String((detail as { error: string }).error)
        : typeof detail === 'string'
          ? detail
          : 'unknown_error';
    throw new ApiError(res.status, code, detail, code);
  }
  return body as T;
}

// ---------------------------------------------------------------- types

export type Settings = {
  company_name: string;
  calling_timezone: string;
  calling_hours_start: string;
  calling_hours_end: string;
  calling_weekdays: number[];
  exclude_holidays: boolean;
  max_attempts_per_day: number;
  max_attempts_total: number;
  auto_dial_delay_sec: number;
  recording_retention_days: number;
  ai_features_enabled: boolean;
};

export type Immutable = { label: string; value: string; reason: string };

export type ContactList = {
  id: string;
  name: string;
  is_active: boolean;
  active_contacts: number;
  exhausted_contacts: number;
  total_contacts: number;
};

export type RejectedRow = { line: number; raw: string; reason: string };

export type ImportResult = {
  status?: string;
  accepted: number;
  rejected: RejectedRow[];
  rejected_total: number;
  inserted?: number;
  skipped_dnc?: number;
  archived_contacts?: number;
};

export type DncEntry = {
  phone_e164: string;
  reason: string;
  source: string;
  created_at: string;
};

export type AuditEntry = {
  action: string;
  actor: string | null;
  target_type: string | null;
  detail: Record<string, unknown>;
  created_at: string;
};

// ---------------------------------------------------------------- endpoints

export const adminApi = {
  getSettings: () =>
    request<{ settings: Settings; immutable: Immutable[] }>('/api/admin/settings'),

  saveSettings: (settings: Settings) =>
    request<{ status: string }>('/api/admin/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),

  callingWindow: () =>
    request<{ now: string; can_call_now: boolean; next_open: string }>(
      '/api/admin/calling-window',
    ),

  lists: () => request<ContactList[]>('/api/admin/lists'),

  createList: (name: string) =>
    request<{ id: string; name: string }>('/api/admin/lists', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  importContacts: (listId: string, csv: string, dryRun: boolean) =>
    request<ImportResult>(`/api/admin/lists/${listId}/contacts`, {
      method: 'POST',
      body: JSON.stringify({ csv, dry_run: dryRun }),
    }),

  dnc: () => request<{ total: number; entries: DncEntry[] }>('/api/admin/dnc'),

  importDnc: (phones: string, dryRun: boolean) =>
    request<ImportResult>('/api/admin/dnc/import', {
      method: 'POST',
      body: JSON.stringify({ phones, dry_run: dryRun }),
    }),

  audit: () =>
    request<{ logs: AuditEntry[]; blocked_last_7d: { reason: string; count: number }[] }>(
      '/api/admin/audit',
    ),
};

/** 関門で止まった理由の表示名。監査画面で件数と一緒に出す */
export const BLOCK_REASON_LABELS: Record<string, string> = {
  dnc: '架電拒否の登録あり',
  outside_hours: '架電可能時間外',
  recently_called: '直前に発信済み',
  daily_limit: '当日の上限',
  total_limit: '通算の上限',
  no_reservation: '予約なし',
  invalid_number: '番号の形式が不正',
  contact_inactive: '対象外の連絡先',
  dialing_disabled: '発信を全体停止中',
  telephony_unconfigured: '電話基盤が未設定',
};

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  'settings.updated': 'テナント設定を変更',
  'list.created': 'リストを作成',
  'contacts.imported': '連絡先を取り込み',
  'dnc.imported': 'DNC を取り込み',
  'recording.listen': '録音を再生',
};

export function importErrorRows(error: ApiError): RejectedRow[] {
  const detail = error.detail as { rejected?: RejectedRow[] } | null;
  return detail?.rejected ?? [];
}
