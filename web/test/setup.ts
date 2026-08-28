import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

/**
 * 全テスト共通のセットアップ。
 *
 * ★ jsdom には getUserMedia が無い。架電画面は起動時に必ずマイク権限を
 *   取りに行くので、既定を用意しておかないと全テストが「マイクなし」の
 *   分岐に落ちて、本来見たい経路を通らない。
 */
beforeEach(() => {
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: {
      getUserMedia: vi.fn(async () => ({
        getTracks: () => [{ stop: vi.fn() }],
      })),
    },
  });

  // ハートビートなど、テストの主題ではない fetch を既定で成功させる
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 200 })),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
