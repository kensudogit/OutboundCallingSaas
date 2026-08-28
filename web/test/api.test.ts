import { beforeEach, describe, expect, test, vi } from 'vitest';
import { ApiError, api, blockedMessage } from '../lib/api';

/**
 * API クライアントと、関門で止まったときの案内。
 *
 * ★ blockedMessage が本題。全部を「発信できません」にすると、担当者は
 *   原因が分からず、管理者に聞き、管理者も分からない。理由コードごとに
 *   「次に何をすればよいか」が言えていることを固定する。
 */

let fetchMock: ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  // ★ Response は一度しか読めないので、呼び出しごとに作る
  fetchMock = vi.fn().mockImplementation(async () => jsonResponse({}));
  vi.stubGlobal('fetch', fetchMock);
});

function lastCall(): [string, RequestInit] {
  const call = fetchMock.mock.calls.at(-1);
  return [call?.[0] as string, (call?.[1] ?? {}) as RequestInit];
}

// ---------------------------------------------------------------- エンドポイント

describe('エンドポイント', () => {
  test('発信は contact_id だけを送る', async () => {
    await api.placeCall('c-1');

    const [url, init] = lastCall();
    expect(url).toBe('/api/calls');
    expect(init.method).toBe('POST');
    // ★ 電話番号を送らない。かける相手はサーバーが contact_id から引く。
    //   番号を body で受ける実装は、任意の番号に発信させられる
    expect(JSON.parse(init.body as string)).toEqual({ contact_id: 'c-1' });
  });

  test('結果登録は通話 ID ごとのパスを叩く', async () => {
    await api.registerDisposition('call-1', 'appointment', 'メモ');

    const [url, init] = lastCall();
    expect(url).toBe('/api/calls/call-1/disposition');
    expect(JSON.parse(init.body as string)).toEqual({
      disposition_code: 'appointment',
      note: 'メモ',
    });
  });

  test('キュー取得はリスト ID をエスケープして渡す', async () => {
    await api.nextContact('a b/c');
    expect(lastCall()[0]).toBe('/api/queue/next?list_id=a%20b%2Fc');
  });

  test('204 は本文なしとして扱う', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.skip('c-1')).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------- エラー

describe('エラーの取り出し', () => {
  test('関門の理由コードを取り出す', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { detail: { error: 'call_blocked', reason: 'dnc', detail: '架電拒否の登録があります' } },
        403,
      ),
    );

    const error = await api.placeCall('c-1').catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.code).toBe('dnc');
  });

  test('文字列の detail も拾う', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'contact_not_found' }, 404));
    const error = await api.placeCall('c-1').catch((e) => e);
    expect(error.code).toBe('contact_not_found');
  });

  // ★ 「通信が届いたか分からない」失敗。架電ではこの区別が最も重要
  test('通信自体が失敗したら network_error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const error = await api.placeCall('c-1').catch((e) => e);
    expect(error.status).toBe(0);
    expect(error.code).toBe('network_error');
  });
});

// ---------------------------------------------------------------- 案内文

describe('関門で止まったときの案内', () => {
  function blocked(reason: string, extra: Record<string, unknown> = {}): ApiError {
    return new ApiError(403, reason, { reason, ...extra }, reason);
  }

  test.each([
    ['dnc', '架電をお断り'],
    ['recently_called', '直前に発信済み'],
    ['total_limit', '上限に達しています'],
    ['no_reservation', '取り直してください'],
    ['invalid_number', '管理者に連絡'],
    ['contact_inactive', '架電対象外'],
    ['dialing_disabled', 'システム全体で発信を停止'],
  ])('%s は具体的な案内になる', (reason, expected) => {
    expect(blockedMessage(blocked(reason))).toContain(expected);
  });

  // ★ retry_after があると「いつなら再開できるか」を言える。
  //   これが無いと担当者は待つしかなく、管理者に問い合わせが飛ぶ
  test('時間帯外は再開できる時刻を伝える', () => {
    const message = blockedMessage(
      blocked('outside_hours', { retry_after: '2026-01-07T09:00:00+09:00' }),
    );
    expect(message).toContain('架電可能時間外');
    expect(message).toMatch(/9:00|09:00/);
  });

  test('retry_after が無くても案内は出る', () => {
    expect(blockedMessage(blocked('outside_hours'))).toContain('架電可能時間外');
  });

  test('当日上限も再開時刻を伝える', () => {
    const message = blockedMessage(
      blocked('daily_limit', { retry_after: '2026-01-07T09:00:00+09:00' }),
    );
    expect(message).toContain('本日の架電上限');
  });

  // ★ 発信されたか分からない失敗。「二重にかけない」を明示する
  test('発信の可否が不明なときは二重発信を避けるよう伝える', () => {
    const error = new ApiError(
      503,
      'provider_unavailable',
      { uncertain: true },
      'provider_unavailable',
    );
    const message = blockedMessage(error);
    expect(message).toContain('二重にかけない');
    expect(message).toContain('履歴');
  });

  test('提供側の障害（発信されていないと分かる場合）は再試行を促す', () => {
    const error = new ApiError(
      502,
      'provider_unavailable',
      { uncertain: false },
      'provider_unavailable',
    );
    expect(blockedMessage(error)).toContain('電話網');
  });

  test('未知のコードでも汎用の案内を返す', () => {
    expect(blockedMessage(blocked('something_new'))).toContain('発信できませんでした');
  });
});
