'use client';

/**
 * Twilio Device のライフサイクル。
 *
 * ★ Device はページに 1 つ。React の StrictMode は開発時に effect を 2 回
 *   走らせるので、素直に書くと Device が 2 つできて接続を奪い合う。
 *   cancelled フラグ + cleanup での destroy() の組み合わせが要る。
 *   本番ビルドでは再現しないので原因を探しづらい。StrictMode を外して隠さない。
 *
 * ★ destroy を忘れるとマイクが掴まれたままになり、タブのインジケータが消えない。
 */

import { Call, Device } from '@twilio/voice-sdk';
import { useCallback, useEffect, useRef, useState } from 'react';

export type DeviceState = 'idle' | 'registering' | 'ready' | 'error' | 'unsupported';

async function fetchVoiceToken(): Promise<string> {
  const res = await fetch('/api/voice-token', { method: 'POST' });
  if (!res.ok) throw new Error(`voice token: ${res.status}`);
  const body = (await res.json()) as { token: string };
  return body.token;
}

export function useTwilioDevice() {
  const deviceRef = useRef<Device | null>(null);
  const [state, setState] = useState<DeviceState>('idle');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let device: Device | null = null;

    (async () => {
      setState('registering');
      const token = await fetchVoiceToken();
      if (cancelled) return;

      device = new Device(token, { logLevel: 'error' });

      device.on('registered', () => {
        if (!cancelled) setState('ready');
      });
      device.on('error', (e: { message?: string }) => {
        console.error('[twilio] device error', e);
        if (!cancelled) {
          setState('error');
          // ★ SDK の生の英語メッセージは画面に出さない。ログには残す
          setError('電話機能を初期化できませんでした。ページを再読み込みしてください。');
        }
      });
      // ★ 期限の 3 分前に発火する。ここで取り直さないと、次の発信で
      //   「発信できません」としか出ず、原因に辿り着けない
      device.on('tokenWillExpire', async () => {
        try {
          device?.updateToken(await fetchVoiceToken());
        } catch (e) {
          // 進行中の通話は切れない（トークンは発信の認可に使われる）。
          // 次の発信で失敗させ、そこで再ログインを促す
          console.error('[twilio] token refresh failed', e);
        }
      });

      await device.register();
      if (cancelled) {
        device.destroy();
        return;
      }
      deviceRef.current = device;
    })().catch((e: unknown) => {
      if (cancelled) return;
      console.error('[twilio] device init failed', e);
      setState('error');
      setError('電話機能を初期化できませんでした。ページを再読み込みしてください。');
    });

    return () => {
      cancelled = true;
      device?.destroy();
      deviceRef.current = null;
    };
  }, []);

  const connect = useCallback(async (params: Record<string, string>): Promise<Call> => {
    const device = deviceRef.current;
    if (!device) throw new Error('device_not_ready');
    return device.connect({ params });
  }, []);

  return { state, error, connect, deviceRef };
}

/**
 * マイク権限は架電画面に入った時点で取る。
 *
 * ★ 発信ボタンを押した瞬間に権限ダイアログが出ると、担当者がそれに気を
 *   取られている間に相手が出る。
 * ★ 「デバイスがない」と「拒否された」を区別する。前者はヘッドセットの接続、
 *   後者はブラウザ設定と、対処が違う。1 つのメッセージにまとめると自力で解決できない。
 */
export type MicStatus = 'unknown' | 'granted' | 'denied' | 'no-device';

export async function ensureMicrophone(): Promise<MicStatus> {
  if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    return 'no-device';
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop()); // 権限確認が目的。掴み続けない
    return 'granted';
  } catch (e) {
    if (e instanceof DOMException && (e.name === 'NotFoundError' || e.name === 'OverconstrainedError')) {
      return 'no-device';
    }
    return 'denied';
  }
}

export const MIC_MESSAGES: Record<Exclude<MicStatus, 'granted' | 'unknown'>, string> = {
  denied: 'マイクの使用が許可されていません。ブラウザのアドレスバーの設定から許可してください。',
  'no-device': 'マイクが見つかりません。ヘッドセットを接続してから再読み込みしてください。',
};
