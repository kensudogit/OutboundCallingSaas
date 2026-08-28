import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { TranscriptPane } from '../app/dial/TranscriptPane';

/**
 * 通話中の文字起こし表示。
 *
 * ★ ここで押さえたいのは 3 点。どれも「動いているように見えて役に立たない」
 *   状態になりやすい。
 *
 *   1. 暫定（partial）が確定（final）に書き換わり、行が増えないこと
 *   2. 止まったときに黙らないこと（黙ると「今日は誰も喋っていない」と誤解する）
 *   3. 担当者が読み返している最中に最下部へ飛ばさないこと
 */

class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  receive(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

function latest(): FakeSocket {
  const socket = FakeSocket.instances.at(-1);
  if (!socket) throw new Error('WebSocket が作られていません');
  return socket;
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      new Response(JSON.stringify({ url: 'ws://localhost:8000/ws/agent/{call_id}?token=t' }), {
        status: 200,
      }),
    ),
  );
});

async function mounted() {
  render(<TranscriptPane callId="call-1" active />);
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  latest().onopen?.();
  return latest();
}

describe('接続', () => {
  test('call_id を差し替えた URL に繋ぐ', async () => {
    const socket = await mounted();
    expect(socket.url).toBe('ws://localhost:8000/ws/agent/call-1?token=t');
  });

  test('通話中でなければ繋がない', () => {
    render(<TranscriptPane callId="call-1" active={false} />);
    expect(FakeSocket.instances).toHaveLength(0);
  });

  test('アンマウントで切断する', async () => {
    const { unmount } = render(<TranscriptPane callId="call-1" active />);
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
    const socket = latest();
    unmount();
    await waitFor(() => expect(socket.close).toHaveBeenCalled());
  });
});

describe('文字起こしの表示', () => {
  test('確定した発話が話者付きで出る', async () => {
    const socket = await mounted();

    socket.receive({
      type: 'transcript',
      track: 'inbound',
      text: 'はい、どういったご用件でしょうか',
      is_final: true,
      started_ms: 3200,
    });

    expect(await screen.findByText(/どういったご用件/)).toBeInTheDocument();
    expect(screen.getByText('相手')).toBeInTheDocument();
  });

  test('担当者の発話は自分として表示する', async () => {
    const socket = await mounted();
    socket.receive({
      type: 'transcript', track: 'outbound', text: 'お世話になっております',
      is_final: true, started_ms: 0,
    });
    expect(await screen.findByText('自分')).toBeInTheDocument();
  });

  // ★ 本題。暫定が確定に置き換わり、行が 2 つにならないこと
  test('暫定は確定で置き換わり、行が増えない', async () => {
    const socket = await mounted();

    socket.receive({
      type: 'transcript', track: 'inbound', text: 'はい、どういった',
      is_final: false, started_ms: 3200,
    });
    // 暫定は間引かれるので反映を待つ
    await screen.findByText(/はい、どういった/);

    socket.receive({
      type: 'transcript', track: 'inbound', text: 'はい、どういったご用件でしょうか',
      is_final: true, started_ms: 3200,
    });

    await screen.findByText(/ご用件でしょうか/);
    expect(screen.queryByText('はい、どういった')).not.toBeInTheDocument();
    expect(screen.getAllByText('相手')).toHaveLength(1);
  });

  // ★ 書き換わることを見た目で伝えないと、担当者は「誤字だ」と受け取る
  test('暫定は確定と見た目が違う', async () => {
    const socket = await mounted();
    socket.receive({
      type: 'transcript', track: 'inbound', text: '暫定のテキスト',
      is_final: false, started_ms: 100,
    });

    const line = await screen.findByText(/暫定のテキスト/);
    expect(line.closest('p')).toHaveClass('partial');
  });

  test('話者が違えば別の行になる', async () => {
    const socket = await mounted();
    socket.receive({
      type: 'transcript', track: 'inbound', text: 'あちら', is_final: true, started_ms: 0,
    });
    socket.receive({
      type: 'transcript', track: 'outbound', text: 'こちら', is_final: true, started_ms: 0,
    });

    await screen.findByText('あちら');
    expect(screen.getByText('こちら')).toBeInTheDocument();
  });
});

describe('サジェスト', () => {
  test('切り返し候補が表示される', async () => {
    const socket = await mounted();
    socket.receive({ type: 'suggestion', text: '「予算の時期」を確認してみましょう' });
    expect(await screen.findByText(/予算の時期/)).toBeInTheDocument();
  });
});

describe('止まったとき', () => {
  // ★ 黙って止まると、担当者は「今日は誰も喋っていない」と誤解する
  test('接続が切れたら停止中であることを伝える', async () => {
    const socket = await mounted();
    socket.onclose?.();

    expect(await screen.findByText(/文字起こしが停止中/)).toBeInTheDocument();
  });

  test('停止しても通話が続いていることを添える', async () => {
    const socket = await mounted();
    socket.onerror?.();

    expect(await screen.findByText(/通話は継続しています/)).toBeInTheDocument();
  });

  test('サーバーから利用不可を伝えられた場合も表示する', async () => {
    const socket = await mounted();
    socket.receive({ type: 'transcript_unavailable' });

    expect(await screen.findByText(/文字起こしが停止中/)).toBeInTheDocument();
  });

  test('チケットが取れなければ繋ぎに行かない', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 401 })));
    render(<TranscriptPane callId="call-1" active />);

    expect(await screen.findByText(/文字起こしが停止中/)).toBeInTheDocument();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  test('ping は画面に出さない', async () => {
    const socket = await mounted();
    socket.receive({ type: 'ping' });

    await waitFor(() => {
      expect(document.querySelectorAll('.segments p')).toHaveLength(0);
    });
  });
});
