import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // ★ StrictMode を外して Twilio Device の二重初期化を隠さない。
  //   本番ビルドでのみ再現するバグに気付けなくなる。

  // ★ Docker 用。node_modules を丸ごと運ぶとイメージが数百 MB 膨らむ。
  //   standalone は必要な依存だけを .next/standalone にまとめる
  output: 'standalone',
};

export default nextConfig;
