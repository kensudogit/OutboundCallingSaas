'use client';

/**
 * 通話中の文字起こしとサジェスト。
 *
 * ★ partial（暫定）と final（確定）で見た目を変える。書き換わることを
 *   見た目で伝えないと、担当者は「文字起こしが間違っている」と受け取る。
 *
 * ★ 自動スクロールは担当者が上にスクロールしていたら止める。過去の発言を
 *   読み返している最中に最下部へ飛ばされると読めない。
 *
 * ★ partial の更新は間引く。文字起こしが滑らかに出ることに価値はなく、
 *   遅れないことに価値がある。毎メッセージ再描画すると通話中に CPU を食う。
 */

import { useEffect, useRef, useState } from 'react';

type Segment = {
  key: string;
  track: 'inbound' | 'outbound';
  text: string;
  isFinal: boolean;
};

const PARTIAL_FLUSH_MS = 150;

export function TranscriptPane({ callId, active }: { callId: string | null; active: boolean }) {
  const [segments, setSegments] = useState<Segment[]>([]);
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const [status, setStatus] = useState<'idle' | 'live' | 'unavailable'>('idle');

  const listRef = useRef<HTMLDivElement>(null);
  const pending = useRef<Segment | null>(null);

  useEffect(() => {
    if (!callId || !active) {
      setStatus('idle');
      return;
    }

    setSegments([]);
    setSuggestion(null);

    let ws: WebSocket | null = null;
    let flushTimer: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    (async () => {
      const res = await fetch('/api/ws-ticket', { method: 'POST' });
      if (!res.ok || cancelled) {
        setStatus('unavailable');
        return;
      }
      const { url } = (await res.json()) as { url: string };
      if (cancelled) return;

      ws = new WebSocket(url.replace('{call_id}', callId));

      ws.onopen = () => setStatus('live');
      ws.onerror = () => setStatus('unavailable');
      // ★ 黙って止まると、担当者は「今日は誰も喋っていない」と誤解する
      ws.onclose = () => setStatus((s) => (s === 'live' ? 'unavailable' : s));

      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data as string) as
          | { type: 'transcript'; track: 'inbound' | 'outbound'; text: string; is_final: boolean; started_ms: number }
          | { type: 'suggestion'; text: string }
          | { type: 'ping' | 'stream_ended' | 'transcript_unavailable' };

        if (msg.type === 'suggestion') {
          setSuggestion(msg.text);
          return;
        }
        if (msg.type === 'transcript_unavailable') {
          setStatus('unavailable');
          return;
        }
        if (msg.type !== 'transcript') return;

        const segment: Segment = {
          key: `${msg.track}-${msg.started_ms}`,
          track: msg.track,
          text: msg.text,
          isFinal: msg.is_final,
        };

        if (msg.is_final) {
          // 確定は即座に反映し、同じキーの暫定を置き換える
          pending.current = null;
          setSegments((prev) => upsert(prev, segment));
        } else {
          // 暫定は溜めておき、タイマーでまとめて反映する
          pending.current = segment;
        }
      };

      flushTimer = setInterval(() => {
        if (pending.current) {
          const s = pending.current;
          pending.current = null;
          setSegments((prev) => upsert(prev, s));
        }
      }, PARTIAL_FLUSH_MS);
    })();

    return () => {
      cancelled = true;
      if (flushTimer) clearInterval(flushTimer);
      ws?.close();
    };
  }, [callId, active]);

  // 最下部にいるときだけ追従する
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom) el.scrollTop = el.scrollHeight;
  }, [segments]);

  return (
    <section className="transcript">
      <header>
        <h3>文字起こし</h3>
        {status === 'unavailable' && (
          <span className="warn" role="status">
            文字起こしが停止中です（通話は継続しています）
          </span>
        )}
      </header>

      <div className="segments" ref={listRef}>
        {segments.map((s) => (
          <p
            key={s.key}
            className={[
              s.track === 'inbound' ? 'them' : 'me',
              s.isFinal ? '' : 'partial',
            ].join(' ')}
          >
            <span className="who">{s.track === 'inbound' ? '相手' : '自分'}</span>
            {s.text}
          </p>
        ))}
      </div>

      {/* ★ サジェストは 1〜2 行。通話中に読める量には限りがある */}
      {suggestion && (
        <aside className="suggestion" role="status">
          💡 {suggestion}
        </aside>
      )}
    </section>
  );
}

function upsert(prev: Segment[], next: Segment): Segment[] {
  const index = prev.findIndex((s) => s.key === next.key);
  if (index < 0) return [...prev, next];
  const copy = [...prev];
  copy[index] = next;
  return copy;
}
