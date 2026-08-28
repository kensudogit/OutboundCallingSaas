import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * フロントの単体テストとカバレッジ。
 *
 * ★ 対象は「壊れると気付けない場所」に絞る。架電画面は通話中に使われるので、
 *   不具合が出たときに担当者は相手と話しながら対処することになる。
 *   特に次の 3 つは目視では検出しにくい。
 *
 *   - Twilio Device の二重初期化（StrictMode。本番ビルドでは再現しない）
 *   - 発信ボタンの二度押し（速く押さないと再現しない）
 *   - 関門で止まった理由の出し分け（403 の中身で分岐する）
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['test/**/*.test.{ts,tsx}'],
    setupFiles: ['test/setup.ts'],
    css: false,

    coverage: {
      provider: 'v8',
      reportsDirectory: 'coverage',
      reporter: ['text', 'text-summary', 'html', 'lcov', 'json-summary'],
      include: ['lib/**/*.ts', 'app/**/*.tsx'],
      exclude: [
        // サーバー側でしか動かない部分。jsdom では意味のある検証ができない
        'app/layout.tsx',
        'app/page.tsx',
        'app/**/route.ts',
        'app/login/page.tsx',
        'app/dial/page.tsx',
        '**/*.d.ts',
      ],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 75,
        statements: 80,
      },
    },
  },
});
