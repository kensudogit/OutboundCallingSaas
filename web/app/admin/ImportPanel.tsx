'use client';

/**
 * 取り込み画面。
 *
 * ★ 必ず「確認」を先に通す。数万件を入れる前に何件弾かれるかを見せないと、
 *   取り込んでから気付くことになり、部分的に入った状態の後始末が要る。
 *
 * ★ 弾かれた行は行番号と理由を出す。どこを直せばよいか分からないと、
 *   取り込みが永久に終わらない。
 */

import { useState } from 'react';
import { ApiError } from '../../lib/api';
import { type ImportResult, type RejectedRow, importErrorRows } from '../../lib/adminApi';

type Props = {
  title: string;
  description: string;
  placeholder: string;
  /** 取り込みを止める条件の説明。連絡先と DNC で方針が違う */
  policy: string;
  onImport: (text: string, dryRun: boolean) => Promise<ImportResult>;
  onDone?: () => void;
};

export function ImportPanel({ title, description, placeholder, policy, onImport, onDone }: Props) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [rejected, setRejected] = useState<RejectedRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  // ★ 確認を通していない限り取り込ませない
  const [checked, setChecked] = useState(false);

  async function run(dryRun: boolean) {
    if (busy || !text.trim()) return;
    setBusy(true);
    setError(null);
    setRejected([]);

    try {
      const outcome = await onImport(text, dryRun);
      setResult(outcome);
      setRejected(outcome.rejected ?? []);
      if (dryRun) {
        setChecked(true);
      } else {
        setText('');
        setChecked(false);
        onDone?.();
      }
    } catch (e) {
      if (e instanceof ApiError) {
        setRejected(importErrorRows(e));
        setError(
          e.code === 'invalid_rows'
            ? '不正な行があるため取り込みを中止しました。下の一覧を直してからやり直してください。'
            : e.status === 413
              ? '1 回に取り込める件数を超えています。ファイルを分けてください。'
              : '取り込めませんでした。',
        );
      } else {
        setError('取り込めませんでした。');
      }
      setChecked(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h3>{title}</h3>
      <p className="muted">{description}</p>
      <p className="policy">{policy}</p>

      <textarea
        aria-label={title}
        value={text}
        placeholder={placeholder}
        rows={8}
        onChange={(e) => {
          setText(e.target.value);
          // 内容が変わったら確認をやり直させる
          setChecked(false);
          setResult(null);
        }}
      />

      <div className="actions">
        <button type="button" className="secondary" onClick={() => run(true)} disabled={busy || !text.trim()}>
          確認（取り込まない）
        </button>
        <button type="button" onClick={() => run(false)} disabled={busy || !checked}>
          取り込む
        </button>
      </div>

      {!checked && text.trim() && !error && (
        <p className="muted small">先に「確認」を実行してください。</p>
      )}

      {error && <p className="error" role="alert">{error}</p>}

      {result && (
        <p className="notice" role="status">
          {result.status === 'dry_run' ? '確認結果: ' : '取り込みました: '}
          有効 {result.accepted} 件
          {result.rejected_total > 0 && ` / 不正 ${result.rejected_total} 件`}
          {result.inserted !== undefined && ` / 登録 ${result.inserted} 件`}
          {result.skipped_dnc ? ` / DNC のため除外 ${result.skipped_dnc} 件` : ''}
          {result.archived_contacts ? ` / リストから除外 ${result.archived_contacts} 件` : ''}
        </p>
      )}

      {rejected.length > 0 && (
        <div className="rejected">
          <h4>取り込めなかった行</h4>
          <table>
            <thead>
              <tr>
                <th>行</th>
                <th>値</th>
                <th>理由</th>
              </tr>
            </thead>
            <tbody>
              {rejected.map((row) => (
                <tr key={`${row.line}-${row.raw}`}>
                  <td>{row.line}</td>
                  <td>{row.raw}</td>
                  <td>{row.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
