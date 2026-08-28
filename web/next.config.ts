import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // ★ StrictMode を外して Twilio Device の二重初期化を隠さない。
  //   本番ビルドでのみ再現するバグに気付けなくなる。
};

export default nextConfig;
