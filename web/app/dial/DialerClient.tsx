'use client';

/**
 * 架電画面。
 *
 * ★ 通話中の担当者は画面をほとんど見られない。相手の話を聞きながら、
 *   視線を数回動かせるだけ。この制約から情報の優先順位が決まる。
 *
 * ★ 予約を取るのは「表示した瞬間」。発信ボタンを押した瞬間ではない。
 *   押したときに取ると、2 人が同じ相手を見ている状態が起きる。
 */

import { Call } from '@twilio/voice-sdk';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  api,
  blockedMessage,
  type NextUp,
  type QueueItem,
} from '../../lib/api';
import {
  MIC_MESSAGES,
  type MicStatus,
  ensureMicrophone,
  useTwilioDevice,
} from '../../lib/useTwilioDevice';
import { TranscriptPane } from './TranscriptPane';

type Phase = 'idle' | 'reserved' | 'dialing' | 'talking' | 'wrapup';

const DISPOSITIONS = [
  { code: 'appointment', label: 'アポ獲得' },
  { code: 'callback', label: '再架電希望' },
  { code: 'not_interested', label: '興味なし' },
  { code: 'no_authority', label: '決裁権なし' },
  { code: 'no_answer', label: '不応答' },
  { code: 'busy', label: '話中' },
  { code: 'machine', label: '留守番電話' },
  { code: 'wrong_number', label: '番号違い' },
  // ★ 拒否は選んだ時点で DNC に入る。取り消せないので確認を挟む
  { code: 'refused', label: '架電拒否（DNC登録）', confirm: true },
];

export function DialerClient({ listId }: { listId: string }) {
  const { state: deviceState, error: deviceError, connect } = useTwilioDevice();
  const [mic, setMic] = useState<MicStatus>('unknown');

  const [phase, setPhase] = useState<Phase>('idle');
  const [contact, setContact] = useState<QueueItem | null>(null);
  const [callId, setCallId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [countdown, setCountdown] = useState<NextUp | null>(null);

  const callRef = useRef<Call | null>(null);
  const startedAt = useRef<number | null>(null);

  // ★ 架電画面に入った時点でマイク権限を取る。発信ボタンを押した瞬間に
  //   ダイアログが出ると、担当者がそれに気を取られている間に相手が出る
  useEffect(() => {
    void ensureMicrophone().then(setMic);
  }, []);

  // ハートビート。途切れるとサーバー側で予約が解放される
  useEffect(() => {
    const id = setInterval(() => {
      void fetch('/api/heartbeat', { method: 'POST' }).catch(() => {});
    }, 20_000);
    return () => clearInterval(id);
  }, []);

  // 通話時間の表示
  useEffect(() => {
    if (phase !== 'talking') return;
    const id = setInterval(() => {
      setElapsed(startedAt.current ? Math.floor((Date.now() - startedAt.current) / 1000) : 0);
    }, 1000);
    return () => clearInterval(id);
  }, [phase]);

  // ★ 離脱防止は「防げるかもしれない」程度のもの。本当の復帰はサーバー側の
  //   予約タイムアウトとハートビートが担う
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (phase === 'talking' || phase === 'dialing') e.preventDefault();
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [phase]);

  const loadNext = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      const next = await api.nextContact(listId);
      if (!next) {
        setContact(null);
        setPhase('idle');
        setMessage('このリストに架電できる相手がいません。');
        return;
      }
      setContact(next);
      setCallId(null);
      setPhase('reserved');
    } catch (e) {
      setMessage(e instanceof ApiError ? blockedMessage(e) : '対象を取得できませんでした。');
    } finally {
      setBusy(false);
    }
  }, [listId]);

  async function placeCall() {
    if (!contact || busy || phase !== 'reserved') return; // 二重送信を止める最後の砦
    setBusy(true);
    setMessage(null);
    setPhase('dialing');

    try {
      // ★ サーバーの関門を通る。403 が返ることを前提に組む
      const placed = await api.placeCall(contact.contact_id);
      setCallId(placed.call_id);

      const call = await connect({ call_id: placed.call_id });
      callRef.current = call;

      call.on('accept', () => {
        startedAt.current = Date.now();
        setPhase('talking');
      });
      call.on('disconnect', () => {
        callRef.current = null;
        setPhase('wrapup');
      });
      call.on('cancel', () => {
        callRef.current = null;
        setPhase('wrapup');
      });
    } catch (e) {
      setPhase('reserved');
      setMessage(e instanceof ApiError ? blockedMessage(e) : '発信できませんでした。');
    } finally {
      setBusy(false);
    }
  }

  function hangUp() {
    callRef.current?.disconnect();
  }

  async function skip() {
    if (!contact) return;
    await api.skip(contact.contact_id).catch(() => {});
    setContact(null);
    setPhase('idle');
    await loadNext();
  }

  async function registerDisposition(code: string) {
    if (!callId || busy) return;
    const chosen = DISPOSITIONS.find((d) => d.code === code);
    if (chosen?.confirm && !window.confirm('この相手を架電拒否として登録します。取り消せません。')) {
      return;
    }

    setBusy(true);
    try {
      const result = await api.registerDisposition(callId, code);
      setPhase('idle');
      setContact(null);
      setCallId(null);

      if (result.queue_empty) {
        setMessage('このリストの架電が完了しました。');
        return;
      }
      // ★ プログレッシブの自動発信にはカウントダウンとキャンセルを付ける。
      //   結果登録の直後に鳴らすと、担当者が席を立つタイミングがない
      if (result.next) setCountdown(result.next);
      else await loadNext();
    } catch (e) {
      setMessage(
        e instanceof ApiError && e.status === 409
          ? 'この通話の結果は登録済みです。'
          : '結果を登録できませんでした。',
      );
    } finally {
      setBusy(false);
    }
  }

  const micBlocked = mic === 'denied' || mic === 'no-device';
  const canDial = deviceState === 'ready' && !micBlocked && phase === 'reserved' && !busy;

  return (
    <div className="dialer">
      {micBlocked && <p role="alert" className="error">{MIC_MESSAGES[mic]}</p>}
      {deviceError && <p role="alert" className="error">{deviceError}</p>}
      {message && <p role="status" className="notice">{message}</p>}

      {countdown && (
        <AutoDialCountdown
          next={countdown}
          onFire={async () => {
            setCountdown(null);
            await loadNext();
          }}
          onCancel={() => {
            setCountdown(null);
            setMessage('自動発信を中止しました。');
          }}
        />
      )}

      {/* 常時見える帯。誰と何分話しているか、前回どうだったか */}
      <header className="target">
        <h2>{contact?.company_name ?? '対象なし'}</h2>
        <p className="person">{contact?.person_name ?? ''}</p>
        {phase === 'talking' && (
          <span className="elapsed">{formatDuration(elapsed)}</span>
        )}
        {contact?.previous_calls[0] && (
          <p className="history">
            前回: {new Date(contact.previous_calls[0].started_at).toLocaleDateString('ja-JP')}
            「{contact.previous_calls[0].disposition ?? contact.previous_calls[0].raw_status}」
          </p>
        )}
      </header>

      <div className="panes">
        <TranscriptPane callId={callId} active={phase === 'talking'} />
      </div>

      <div className="actions">
        {phase === 'idle' && (
          <button onClick={loadNext} disabled={busy}>次の相手を表示</button>
        )}
        {phase === 'reserved' && (
          <>
            <button onClick={skip} className="secondary" disabled={busy}>スキップ</button>
            <button onClick={placeCall} disabled={!canDial}>発信</button>
          </>
        )}
        {(phase === 'dialing' || phase === 'talking') && (
          // ★ 通話終了は他の操作から離して置く。誤って切ると相手にかけ直すことになる
          <button onClick={hangUp} className="danger hangup">通話を終了</button>
        )}
      </div>

      {/* ★ 結果登録は通話中から始められる。終わってから入力させると、
          次の発信までの間が延び、記憶も薄れる */}
      {(phase === 'talking' || phase === 'wrapup') && callId && (
        <section className="dispositions">
          <h3>通話結果</h3>
          <div className="grid">
            {DISPOSITIONS.map((d) => (
              <button key={d.code} onClick={() => registerDisposition(d.code)} disabled={busy}>
                {d.label}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function AutoDialCountdown({
  next,
  onFire,
  onCancel,
}: {
  next: NextUp;
  onFire: () => void;
  onCancel: () => void;
}) {
  const [remaining, setRemaining] = useState(next.delay_sec);

  useEffect(() => {
    if (remaining <= 0) {
      onFire();
      return;
    }
    const id = setTimeout(() => setRemaining((r) => r - 1), 1000);
    return () => clearTimeout(id);
  }, [remaining, onFire]);

  return (
    <div className="countdown" role="status">
      <span>
        {remaining} 秒後に「{next.company_name ?? next.phone_e164}」へ発信します
      </span>
      <button onClick={onCancel} className="secondary">中止</button>
    </div>
  );
}

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
