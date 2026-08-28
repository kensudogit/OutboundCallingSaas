import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { DialerClient } from '../app/dial/DialerClient';
import { ApiError, api } from '../lib/api';

/**
 * 架電画面。
 *
 * ★ ここで壊れると、担当者は相手と話しながら対処することになる。
 *   目視では検出しにくい 3 点を押さえる。
 *
 *   1. StrictMode の二重マウントで Device が 2 つできない（本番では再現しない）
 *   2. 発信ボタンの二度押しで 2 回発信しない（速く押さないと再現しない）
 *   3. 関門で止まった理由が画面に出る（403 の中身で分岐する）
 */

// 文字起こしの WebSocket はここでは扱わない（TranscriptPane.test.tsx で見る）
vi.mock('../app/dial/TranscriptPane', () => ({
  TranscriptPane: () => <div data-testid="transcript" />,
}));

const deviceInstances: FakeDevice[] = [];

class FakeCall {
  private handlers = new Map<string, (...args: unknown[]) => void>();
  on(event: string, handler: (...args: unknown[]) => void) {
    this.handlers.set(event, handler);
  }
  emit(event: string) {
    this.handlers.get(event)?.();
  }
  disconnect = vi.fn(() => this.emit('disconnect'));
}

class FakeDevice {
  static lastCall: FakeCall | null = null;
  private handlers = new Map<string, (...args: unknown[]) => void>();

  constructor() {
    deviceInstances.push(this);
  }
  on(event: string, handler: (...args: unknown[]) => void) {
    this.handlers.set(event, handler);
  }
  emit(event: string, ...args: unknown[]) {
    this.handlers.get(event)?.(...args);
  }
  register = vi.fn(async () => {
    this.emit('registered');
  });
  connect = vi.fn(async () => {
    FakeDevice.lastCall = new FakeCall();
    return FakeDevice.lastCall;
  });
  destroy = vi.fn();
  updateToken = vi.fn();
}

vi.mock('@twilio/voice-sdk', () => ({
  Device: class {
    constructor() {
      return new FakeDevice() as unknown as object;
    }
  },
  Call: class {},
}));

const CONTACT = {
  contact_id: 'c-1',
  phone_e164: '+819000000001',
  company_name: '株式会社サンプル',
  person_name: '佐藤 一郎',
  reservation_expires_at: '2026-01-06T14:10:00+09:00',
  previous_calls: [],
};

beforeEach(() => {
  deviceInstances.length = 0;
  FakeDevice.lastCall = null;
  vi.spyOn(api, 'nextContact').mockResolvedValue(CONTACT);
  vi.spyOn(api, 'skip').mockResolvedValue(undefined);
  vi.spyOn(api, 'placeCall').mockResolvedValue({ call_id: 'call-1', status: 'QUEUED' });
  vi.spyOn(api, 'registerDisposition').mockResolvedValue({ status: 'ok', next: null });
});

/** Device の登録が終わるまで待つ */
async function renderReady() {
  const view = render(<DialerClient listId="list-1" />);
  await waitFor(() => expect(deviceInstances.length).toBeGreaterThan(0));
  return view;
}

async function showContact() {
  await userEvent.click(screen.getByRole('button', { name: '次の相手を表示' }));
  await screen.findByText('株式会社サンプル');
}

// ---------------------------------------------------------------- Device

describe('Twilio Device の扱い', () => {
  // ★ 本番ビルドでは再現しないので、ここで押さえないと開発中しか気付けない
  test('StrictMode で二重マウントされても Device は 1 つ', async () => {
    render(
      <StrictMode>
        <DialerClient listId="list-1" />
      </StrictMode>,
    );

    await waitFor(() => expect(deviceInstances.length).toBeGreaterThan(0));
    // 2 つ作られた場合でも、生き残るのは 1 つ（もう一方は destroy される）
    const alive = deviceInstances.filter((d) => !d.destroy.mock.calls.length);
    expect(alive).toHaveLength(1);
  });

  // ★ destroy を忘れるとマイクが掴まれたままになり、タブのインジケータが消えない
  test('アンマウントで destroy が呼ばれる', async () => {
    const { unmount } = await renderReady();
    unmount();
    await waitFor(() => expect(deviceInstances[0]!.destroy).toHaveBeenCalled());
  });

  // ★ 期限切れに気付けないと「発信できません」としか出ず、原因に辿り着けない
  test('トークンの期限が近づいたら取り直す', async () => {
    await renderReady();
    deviceInstances[0]!.emit('tokenWillExpire');
    await waitFor(() => expect(deviceInstances[0]!.updateToken).toHaveBeenCalled());
  });

  test('Device の初期化に失敗したら日本語で案内する', async () => {
    await renderReady();
    deviceInstances[0]!.emit('error', { message: 'AccessTokenInvalid' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('電話機能を初期化できませんでした');
    // ★ SDK の生の英語メッセージは画面に出さない
    expect(alert).not.toHaveTextContent('AccessTokenInvalid');
  });
});

// ---------------------------------------------------------------- マイク

describe('マイク権限', () => {
  test('拒否されていれば発信できないが、スキップはできる', async () => {
    (navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockRejectedValue(
      new DOMException('denied', 'NotAllowedError'),
    );

    await renderReady();
    await showContact();

    expect(await screen.findByText(/ブラウザのアドレスバーの設定/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '発信' })).toBeDisabled();
    // ★ 発信できなくても前に進めること。詰まると予約を握ったまま離脱する
    expect(screen.getByRole('button', { name: 'スキップ' })).toBeEnabled();
  });

  // ★ 「デバイスが無い」と「拒否された」で対処が違う。1 つにまとめると自力で直せない
  test('マイクが無い場合は接続を促す', async () => {
    (navigator.mediaDevices.getUserMedia as ReturnType<typeof vi.fn>).mockRejectedValue(
      new DOMException('none', 'NotFoundError'),
    );

    await renderReady();
    expect(await screen.findByText(/ヘッドセットを接続/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- 発信

describe('発信', () => {
  test('対象を表示してから発信できる', async () => {
    await renderReady();
    await showContact();

    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() => expect(api.placeCall).toHaveBeenCalledWith('c-1'));
    expect(deviceInstances[0]!.connect).toHaveBeenCalledWith({
      params: { call_id: 'call-1' },
    });
  });

  // ★ 連打しても 1 回。サーバー側の関門も受け止めるが、フロントでも止める
  test('発信ボタンを連打しても 1 回しか発信しない', async () => {
    await renderReady();
    await showContact();

    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());

    await Promise.all([userEvent.click(button), userEvent.click(button), userEvent.click(button)]);

    await waitFor(() => expect(api.placeCall).toHaveBeenCalledTimes(1));
  });

  test('スキップすると次の相手を取りに行く', async () => {
    await renderReady();
    await showContact();

    await userEvent.click(screen.getByRole('button', { name: 'スキップ' }));

    await waitFor(() => expect(api.skip).toHaveBeenCalledWith('c-1'));
    expect(api.nextContact).toHaveBeenCalledTimes(2);
  });

  test('キューが尽きたら理由を出す', async () => {
    vi.mocked(api.nextContact).mockResolvedValue(null);
    await renderReady();
    await userEvent.click(screen.getByRole('button', { name: '次の相手を表示' }));

    expect(await screen.findByText(/架電できる相手がいません/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- 関門

describe('関門で止まったとき', () => {
  test.each([
    ['dnc', '架電をお断り'],
    ['outside_hours', '架電可能時間外'],
    ['recently_called', '直前に発信済み'],
  ])('%s の理由が画面に出る', async (reason, expected) => {
    vi.mocked(api.placeCall).mockRejectedValue(
      new ApiError(403, reason, { reason }, reason),
    );

    await renderReady();
    await showContact();

    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    expect(await screen.findByText(new RegExp(expected))).toBeInTheDocument();
    // ★ 止まった後も同じ相手で再試行できる状態に戻ること
    await waitFor(() => expect(screen.getByRole('button', { name: '発信' })).toBeEnabled());
  });
});

// ---------------------------------------------------------------- 結果登録

describe('通話結果の登録', () => {
  async function reachWrapUp() {
    await renderReady();
    await showContact();
    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await waitFor(() => expect(FakeDevice.lastCall).not.toBeNull());
    FakeDevice.lastCall!.emit('accept');
    FakeDevice.lastCall!.emit('disconnect');
    return screen.findByText('通話結果');
  }

  test('通話が終わると結果を選べる', async () => {
    await reachWrapUp();
    await userEvent.click(screen.getByRole('button', { name: 'アポ獲得' }));
    await waitFor(() =>
      expect(api.registerDisposition).toHaveBeenCalledWith('call-1', 'appointment'),
    );
  });

  // ★ DNC 登録は取り消せない。確認を挟む
  test('架電拒否は確認してから登録する', async () => {
    await reachWrapUp();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);

    await userEvent.click(screen.getByRole('button', { name: /架電拒否/ }));

    expect(confirm).toHaveBeenCalled();
    expect(api.registerDisposition).not.toHaveBeenCalled();
  });

  test('確認して OK なら登録される', async () => {
    await reachWrapUp();
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await userEvent.click(screen.getByRole('button', { name: /架電拒否/ }));

    await waitFor(() =>
      expect(api.registerDisposition).toHaveBeenCalledWith('call-1', 'refused'),
    );
  });

  test('登録済みなら重複を伝える', async () => {
    await reachWrapUp();
    vi.mocked(api.registerDisposition).mockRejectedValue(
      new ApiError(409, 'already_dispositioned', null, 'already_dispositioned'),
    );

    await userEvent.click(screen.getByRole('button', { name: '不応答' }));

    expect(await screen.findByText(/登録済みです/)).toBeInTheDocument();
  });

  // ★ 結果登録の直後に鳴らすと担当者が息を継げない。中止できること
  test('プログレッシブの自動発信は中止できる', async () => {
    vi.mocked(api.registerDisposition).mockResolvedValue({
      status: 'ok',
      next: {
        contact_id: 'c-2',
        phone_e164: '+819000000002',
        company_name: 'テスト工業株式会社',
        person_name: null,
        delay_sec: 3,
      },
    });

    await reachWrapUp();
    await userEvent.click(screen.getByRole('button', { name: 'アポ獲得' }));

    const countdown = await screen.findByRole('status');
    expect(countdown).toHaveTextContent('テスト工業株式会社');

    await userEvent.click(within(countdown).getByRole('button', { name: '中止' }));

    expect(await screen.findByText(/自動発信を中止しました/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- 通話中

describe('通話中', () => {
  async function startTalking() {
    await renderReady();
    await showContact();
    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await waitFor(() => expect(FakeDevice.lastCall).not.toBeNull());
    FakeDevice.lastCall!.emit('accept');
    return FakeDevice.lastCall!;
  }

  test('応答すると通話終了ボタンが出る', async () => {
    await startTalking();
    expect(await screen.findByRole('button', { name: '通話を終了' })).toBeInTheDocument();
    // ★ 誤操作を避けるため、他の操作から離して置く（CSS で右端へ寄せている）
    expect(screen.getByRole('button', { name: '通話を終了' })).toHaveClass('hangup');
  });

  test('通話終了で切断され、結果登録に進む', async () => {
    const call = await startTalking();
    await userEvent.click(await screen.findByRole('button', { name: '通話を終了' }));

    expect(call.disconnect).toHaveBeenCalled();
    expect(await screen.findByText('通話結果')).toBeInTheDocument();
  });

  // ★ 結果登録は通話中から始められる。終わってから入力させると、
  //   次の発信までの間が延び、記憶も薄れる
  test('通話中でも結果を選べる', async () => {
    await startTalking();
    expect(await screen.findByText('通話結果')).toBeInTheDocument();
  });

  test('経過時間が表示される', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await startTalking();
      await vi.advanceTimersByTimeAsync(65_000);
      expect(await screen.findByText(/1:0\d/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  // ★ 途切れるとサーバー側で予約が解放される。ブラウザが落ちても
  //   リストが枯れないようにするための唯一の生存信号
  test('ハートビートを送り続ける', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await renderReady();
      const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
      await vi.advanceTimersByTimeAsync(45_000);
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls.length).toBeGreaterThan(before);
      expect(calls.some((c) => String(c[0]).includes('/api/heartbeat'))).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  test('不応答で通話が始まらなくても結果登録に進める', async () => {
    await renderReady();
    await showContact();
    const button = await screen.findByRole('button', { name: '発信' });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await waitFor(() => expect(FakeDevice.lastCall).not.toBeNull());

    // 相手が出ないまま切れた
    FakeDevice.lastCall!.emit('cancel');

    expect(await screen.findByText('通話結果')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '不応答' })).toBeEnabled();
  });
});
