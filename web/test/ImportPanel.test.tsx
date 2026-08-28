import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, test, vi } from 'vitest';
import { ImportPanel } from '../app/admin/ImportPanel';
import { ApiError } from '../lib/api';
import type { ImportResult } from '../lib/adminApi';

/**
 * 取り込み画面。
 *
 * ★ ここで守りたいのは「確認を通さないと取り込めない」こと。
 *   数万件を入れる前に何件弾かれるかを見せないと、取り込んでから気付く
 *   ことになり、部分的に入った状態の後始末が要る。
 *
 * ★ 弾かれた行は行番号と理由が出ること。どこを直せばよいか分からないと
 *   取り込みが永久に終わらない。
 */

type ImportFn = (text: string, dryRun: boolean) => Promise<ImportResult>;

let onImport: ReturnType<typeof vi.fn<ImportFn>>;

function renderPanel(overrides: Partial<Parameters<typeof ImportPanel>[0]> = {}) {
  return render(
    <ImportPanel
      title="連絡先の取り込み"
      description="CSV を貼り付けてください"
      policy="1 件でも不正なら 1 件も取り込みません"
      placeholder="電話番号"
      onImport={onImport}
      {...overrides}
    />,
  );
}

beforeEach(() => {
  onImport = vi.fn<ImportFn>(async () => ({
    status: 'dry_run',
    accepted: 2,
    rejected: [],
    rejected_total: 0,
  }));
});

async function type(text: string) {
  await userEvent.type(screen.getByLabelText('連絡先の取り込み'), text);
}

// ---------------------------------------------------------------- 確認の強制

describe('確認を先に通させる', () => {
  test('入力前はどちらも押せない', () => {
    renderPanel();
    expect(screen.getByRole('button', { name: /確認/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: '取り込む' })).toBeDisabled();
  });

  // ★ 本題。確認を通していない限り取り込ませない
  test('確認を通すまで取り込みボタンは押せない', async () => {
    renderPanel();
    await type('09011112222');

    expect(screen.getByRole('button', { name: /確認/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: '取り込む' })).toBeDisabled();
    expect(screen.getByText(/先に「確認」を実行してください/)).toBeInTheDocument();
  });

  test('確認すると取り込めるようになる', async () => {
    renderPanel();
    await type('09011112222');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    expect(onImport).toHaveBeenCalledWith('09011112222', true);
  });

  // ★ 確認した内容と違うものを取り込ませない
  test('内容を変えたら確認をやり直させる', async () => {
    renderPanel();
    await type('09011112222');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );

    await type('\n09033334444');

    expect(screen.getByRole('button', { name: '取り込む' })).toBeDisabled();
  });

  test('取り込みは dry_run=false で呼ぶ', async () => {
    renderPanel();
    await type('09011112222');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );

    onImport.mockResolvedValue({
      status: 'ok', accepted: 1, rejected: [], rejected_total: 0, inserted: 1,
    });
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    await waitFor(() => expect(onImport).toHaveBeenLastCalledWith('09011112222', false));
  });
});

// ---------------------------------------------------------------- 結果表示

describe('結果の表示', () => {
  test('確認結果に有効件数と不正件数を出す', async () => {
    onImport.mockResolvedValue({
      status: 'dry_run',
      accepted: 8,
      rejected: [{ line: 3, raw: 'abc', reason: '解析できません: abc' }],
      rejected_total: 1,
    });

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));

    const status = await screen.findByRole('status');
    expect(status).toHaveTextContent('有効 8 件');
    expect(status).toHaveTextContent('不正 1 件');
  });

  // ★ どこを直せばよいか分からないと、取り込みが永久に終わらない
  test('弾かれた行は行番号と理由が出る', async () => {
    onImport.mockResolvedValue({
      status: 'dry_run',
      accepted: 1,
      rejected: [{ line: 3, raw: 'abc', reason: '解析できません: abc' }],
      rejected_total: 1,
    });

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));

    expect(await screen.findByText('解析できません: abc')).toBeInTheDocument();
    expect(screen.getByText('abc')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  test('DNC で除外された件数も出す', async () => {
    onImport.mockResolvedValue({
      status: 'ok', accepted: 3, rejected: [], rejected_total: 0,
      inserted: 2, skipped_dnc: 1,
    });

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    expect(await screen.findByText(/DNC のため除外 1 件/)).toBeInTheDocument();
  });

  test('取り込みが終わると入力欄が空になる', async () => {
    onImport.mockResolvedValue({
      status: 'ok', accepted: 1, rejected: [], rejected_total: 0, inserted: 1,
    });

    renderPanel();
    await type('09011112222');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    await waitFor(() => expect(screen.getByLabelText('連絡先の取り込み')).toHaveValue(''));
  });

  test('完了したら呼び出し側に知らせる', async () => {
    const onDone = vi.fn();
    onImport.mockResolvedValue({
      status: 'ok', accepted: 1, rejected: [], rejected_total: 0, inserted: 1,
    });

    renderPanel({ onDone });
    await type('09011112222');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });
});

// ---------------------------------------------------------------- 失敗

describe('取り込みに失敗したとき', () => {
  // ★ サーバー側が 422 で止めた場合も、行番号を出して直せるようにする
  test('不正な行があれば中止した旨と該当行を出す', async () => {
    onImport
      .mockResolvedValueOnce({
        status: 'dry_run', accepted: 1, rejected: [], rejected_total: 0,
      })
      .mockRejectedValueOnce(
        new ApiError(
          422,
          'invalid_rows',
          { rejected: [{ line: 5, raw: 'xyz', reason: '有効な番号ではありません' }] },
          'invalid_rows',
        ),
      );

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('取り込みを中止しました');
    expect(screen.getByText('有効な番号ではありません')).toBeInTheDocument();
    // 失敗したら確認からやり直させる
    expect(screen.getByRole('button', { name: '取り込む' })).toBeDisabled();
  });

  test('件数超過は分割を促す', async () => {
    onImport
      .mockResolvedValueOnce({
        status: 'dry_run', accepted: 60000, rejected: [], rejected_total: 0,
      })
      .mockRejectedValueOnce(new ApiError(413, 'too_many', null, 'too_many'));

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '取り込む' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '取り込む' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('ファイルを分けてください');
  });

  test('通信エラーでも案内を出す', async () => {
    onImport.mockRejectedValue(new ApiError(0, 'network_error', null, 'network_error'));

    renderPanel();
    await type('data');
    await userEvent.click(screen.getByRole('button', { name: /確認/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent('取り込めませんでした');
  });
});
