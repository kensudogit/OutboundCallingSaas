'use client';

/**
 * 管理画面。
 *
 * ★ 「変えられない設定」を理由付きで見せる。設定項目が無いことに気付かず
 *   探し回るより、なぜ無いかを書いておくほうが早い。
 *
 * ★ 移行手順として **DNC をリストより先に** 取り込ませる。順序を逆にすると
 *   その間の架電が全部違反になる。画面の並びと注意書きでそれを伝える。
 */

import { useCallback, useEffect, useState } from 'react';
import {
  AUDIT_ACTION_LABELS,
  BLOCK_REASON_LABELS,
  type AuditEntry,
  type ContactList,
  type DncEntry,
  type Immutable,
  type Settings,
  adminApi,
} from '../../lib/adminApi';
import { ImportPanel } from './ImportPanel';

const WEEKDAYS = [
  { value: 1, label: '月' },
  { value: 2, label: '火' },
  { value: 3, label: '水' },
  { value: 4, label: '木' },
  { value: 5, label: '金' },
  { value: 6, label: '土' },
  { value: 7, label: '日' },
];

type Tab = 'settings' | 'lists' | 'dnc' | 'audit';

export function AdminClient() {
  const [tab, setTab] = useState<Tab>('settings');

  return (
    <div className="admin">
      <nav className="tabs">
        {(
          [
            ['settings', '設定'],
            ['dnc', 'DNC（架電拒否）'],
            ['lists', 'リスト'],
            ['audit', '監査ログ'],
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={tab === key ? 'active' : ''}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === 'settings' && <SettingsTab />}
      {tab === 'dnc' && <DncTab />}
      {tab === 'lists' && <ListsTab />}
      {tab === 'audit' && <AuditTab />}
    </div>
  );
}

// ---------------------------------------------------------------- 設定

function SettingsTab() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [immutable, setImmutable] = useState<Immutable[]>([]);
  const [window, setWindow] = useState<{ can_call_now: boolean; next_open: string } | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const [config, current] = await Promise.all([
      adminApi.getSettings(),
      adminApi.callingWindow().catch(() => null),
    ]);
    setSettings(config.settings);
    setImmutable(config.immutable);
    setWindow(current);
  }, []);

  useEffect(() => {
    void load().catch(() => setError('設定を読み込めませんでした。'));
  }, [load]);

  if (!settings) return <p className="muted">読み込み中…</p>;

  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    setSettings((s) => (s ? { ...s, [key]: value } : s));
    setMessage(null);
  }

  async function save() {
    if (!settings || busy) return;
    setBusy(true);
    setError(null);
    try {
      await adminApi.saveSettings(settings);
      setMessage('保存しました。');
      await load();
    } catch {
      setError('保存できませんでした。入力内容をご確認ください。');
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {/* ★ 設定を変えた結果が今どうなるかを見せる。時間帯の設定ミスは
          「なぜか誰もかけられない」という形で表面化し、原因が分かりにくい */}
      {window && (
        <p className={window.can_call_now ? 'notice' : 'policy'} role="status">
          {window.can_call_now
            ? '現在の設定では、今この時間に架電できます。'
            : `現在の設定では、今は架電できません。次に架電できるのは ${new Date(
                window.next_open,
              ).toLocaleString('ja-JP')} です。`}
        </p>
      )}

      <section className="panel">
        <h3>架電ポリシー</h3>

        <label>
          事業者名（通話冒頭で名乗る名称）
          <input
            value={settings.company_name}
            onChange={(e) => update('company_name', e.target.value)}
          />
        </label>
        <p className="muted small">
          電話勧誘販売では、通話の冒頭で事業者名・担当者名・勧誘目的を伝える必要があります。
          空にはできません。
        </p>

        <div className="row">
          <label>
            架電開始
            <input
              type="time"
              value={settings.calling_hours_start}
              onChange={(e) => update('calling_hours_start', e.target.value)}
            />
          </label>
          <label>
            架電終了
            <input
              type="time"
              value={settings.calling_hours_end}
              onChange={(e) => update('calling_hours_end', e.target.value)}
            />
          </label>
          <label>
            タイムゾーン
            <input
              value={settings.calling_timezone}
              onChange={(e) => update('calling_timezone', e.target.value)}
            />
          </label>
        </div>

        <fieldset>
          <legend>架電する曜日</legend>
          {WEEKDAYS.map((day) => (
            <label key={day.value} className="inline">
              <input
                type="checkbox"
                checked={settings.calling_weekdays.includes(day.value)}
                onChange={(e) =>
                  update(
                    'calling_weekdays',
                    e.target.checked
                      ? [...settings.calling_weekdays, day.value].sort()
                      : settings.calling_weekdays.filter((d) => d !== day.value),
                  )
                }
              />
              {day.label}
            </label>
          ))}
        </fieldset>

        <label className="inline">
          <input
            type="checkbox"
            checked={settings.exclude_holidays}
            onChange={(e) => update('exclude_holidays', e.target.checked)}
          />
          祝日は架電しない
        </label>

        <div className="row">
          <label>
            同じ相手への 1 日の上限
            <input
              type="number"
              min={1}
              max={10}
              value={settings.max_attempts_per_day}
              onChange={(e) => update('max_attempts_per_day', Number(e.target.value))}
            />
          </label>
          <label>
            同じ相手への通算の上限
            <input
              type="number"
              min={1}
              max={50}
              value={settings.max_attempts_total}
              onChange={(e) => update('max_attempts_total', Number(e.target.value))}
            />
          </label>
          <label>
            自動発信までの間隔（秒）
            <input
              type="number"
              min={0}
              max={60}
              value={settings.auto_dial_delay_sec}
              onChange={(e) => update('auto_dial_delay_sec', Number(e.target.value))}
            />
          </label>
        </div>
        <p className="muted small">
          「つながるまでかける」は苦情を生み、苦情は DNC 登録につながってリストが痩せます。
        </p>
      </section>

      <section className="panel">
        <h3>録音と AI</h3>
        <label>
          録音の保存期間（日）
          <input
            type="number"
            min={1}
            max={3650}
            value={settings.recording_retention_days}
            onChange={(e) => update('recording_retention_days', Number(e.target.value))}
          />
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={settings.ai_features_enabled}
            onChange={(e) => update('ai_features_enabled', e.target.checked)}
          />
          文字起こし・要約を利用する
        </label>
        <p className="muted small">
          無効にしても通話・録音・結果登録は動きます。
        </p>
      </section>

      {/* ★ 「無い設定」を理由付きで見せる */}
      <section className="panel immutable">
        <h3>変更できない項目</h3>
        <dl>
          {immutable.map((item) => (
            <div key={item.label}>
              <dt>
                {item.label} — <strong>{item.value}</strong>
              </dt>
              <dd>{item.reason}</dd>
            </div>
          ))}
        </dl>
      </section>

      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="notice" role="status">{message}</p>}

      <div className="actions">
        <button type="button" onClick={save} disabled={busy}>
          保存
        </button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- DNC

function DncTab() {
  const [data, setData] = useState<{ total: number; entries: DncEntry[] } | null>(null);

  const load = useCallback(async () => {
    setData(await adminApi.dnc());
  }, []);

  useEffect(() => {
    void load().catch(() => setData({ total: 0, entries: [] }));
  }, [load]);

  return (
    <>
      <ImportPanel
        title="DNC（架電拒否）の取り込み"
        description="1 行に 1 件、電話番号を貼り付けてください。CSV の 1 列目でも読み取れます。"
        policy="★ 移行では、リストより先にこちらを取り込んでください。順序を逆にすると、その間の架電が拒否済みの相手に届きます。取り込んだ番号は、既存リストからも自動で対象外になります。"
        placeholder={'090-1234-5678\n03-1234-5678'}
        onImport={(text, dryRun) => adminApi.importDnc(text, dryRun)}
        onDone={load}
      />

      <section className="panel">
        <h3>登録済み（{data?.total ?? 0} 件）</h3>
        <p className="muted small">
          DNC は取り消せません。誤登録の訂正が必要な場合は、運用の申請フローで対応してください。
        </p>
        <table>
          <thead>
            <tr>
              <th>電話番号</th>
              <th>理由</th>
              <th>登録元</th>
              <th>登録日時</th>
            </tr>
          </thead>
          <tbody>
            {(data?.entries ?? []).map((entry) => (
              <tr key={entry.phone_e164}>
                <td>{entry.phone_e164}</td>
                <td>{entry.reason}</td>
                <td>{entry.source}</td>
                <td>{new Date(entry.created_at).toLocaleString('ja-JP')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

// ---------------------------------------------------------------- リスト

function ListsTab() {
  const [lists, setLists] = useState<ContactList[]>([]);
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const rows = await adminApi.lists();
    setLists(rows);
    setSelected((current) => current ?? rows[0]?.id ?? null);
  }, []);

  useEffect(() => {
    void load().catch(() => setError('リストを読み込めませんでした。'));
  }, [load]);

  async function create() {
    if (!name.trim()) return;
    try {
      const created = await adminApi.createList(name.trim());
      setName('');
      await load();
      setSelected(created.id);
    } catch {
      setError('リストを作成できませんでした。');
    }
  }

  return (
    <>
      {error && <p className="error" role="alert">{error}</p>}

      <section className="panel">
        <h3>リスト</h3>
        <div className="row">
          <label style={{ flex: 1 }}>
            新しいリスト名
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <button type="button" onClick={create} disabled={!name.trim()}>
            作成
          </button>
        </div>

        <table>
          <thead>
            <tr>
              <th>リスト</th>
              <th>架電可能</th>
              <th>上限到達</th>
              <th>合計</th>
            </tr>
          </thead>
          <tbody>
            {lists.map((list) => (
              <tr
                key={list.id}
                className={selected === list.id ? 'selected' : ''}
                onClick={() => setSelected(list.id)}
              >
                <td>{list.name}</td>
                <td>{list.active_contacts}</td>
                <td>{list.exhausted_contacts}</td>
                <td>{list.total_contacts}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {selected && (
        <ImportPanel
          key={selected}
          title="連絡先の取り込み"
          description="1 行目に見出しを置いた CSV を貼り付けてください（phone / 電話番号 の列が必要です）。"
          policy="★ 1 件でも不正な行があれば、1 件も取り込みません。部分的に入ると、再取り込みで重複するか、どこから再開するか分からなくなります。DNC に登録済みの番号は自動で除外されます。"
          placeholder={'電話番号,会社名,担当者\n090-1234-5678,株式会社サンプル,佐藤 一郎'}
          onImport={(text, dryRun) => adminApi.importContacts(selected, text, dryRun)}
          onDone={load}
        />
      )}
    </>
  );
}

// ---------------------------------------------------------------- 監査

function AuditTab() {
  const [data, setData] = useState<{
    logs: AuditEntry[];
    blocked_last_7d: { reason: string; count: number }[];
  } | null>(null);

  useEffect(() => {
    void adminApi
      .audit()
      .then(setData)
      .catch(() => setData({ logs: [], blocked_last_7d: [] }));
  }, []);

  return (
    <>
      {/* ★ 「関門が機能している証跡」。急増は設定ミスかリスト品質の劣化を示す */}
      <section className="panel">
        <h3>関門で止まった発信（直近 7 日）</h3>
        <p className="muted small">
          0 件が正常ではありません。DNC や時間帯で止まるのは想定どおりです。
          急に増えたときは、時間帯の設定かリストの品質を疑ってください。
        </p>
        {data?.blocked_last_7d.length ? (
          <table>
            <thead>
              <tr>
                <th>理由</th>
                <th>件数</th>
              </tr>
            </thead>
            <tbody>
              {data.blocked_last_7d.map((row) => (
                <tr key={row.reason}>
                  <td>{BLOCK_REASON_LABELS[row.reason] ?? row.reason}</td>
                  <td>{row.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">直近 7 日で関門に止められた発信はありません。</p>
        )}
      </section>

      <section className="panel">
        <h3>操作履歴</h3>
        <table>
          <thead>
            <tr>
              <th>日時</th>
              <th>操作</th>
              <th>実行者</th>
              <th>詳細</th>
            </tr>
          </thead>
          <tbody>
            {(data?.logs ?? []).map((log, index) => (
              <tr key={`${log.created_at}-${index}`}>
                <td>{new Date(log.created_at).toLocaleString('ja-JP')}</td>
                <td>{AUDIT_ACTION_LABELS[log.action] ?? log.action}</td>
                <td>{log.actor ?? '—'}</td>
                <td className="detail">{JSON.stringify(log.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
