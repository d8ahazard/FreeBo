/** G.711 µ-law codec + resampling for the 2-way call. The robot's speaker/mic are µ-law @ 8 kHz mono. */

export function linearToMulaw(sample: number): number {
  const BIAS = 0x84;
  const CLIP = 32635;
  let sign = (sample >> 8) & 0x80;
  if (sign) sample = -sample;
  if (sample > CLIP) sample = CLIP;
  sample += BIAS;
  let exponent = 7;
  for (let mask = 0x4000; (sample & mask) === 0 && exponent > 0; exponent--, mask >>= 1) {
    /* find exponent */
  }
  const mantissa = (sample >> (exponent + 3)) & 0x0f;
  return ~(sign | (exponent << 4) | mantissa) & 0xff;
}

export function mulawToLinear(u: number): number {
  u = ~u & 0xff;
  const sign = u & 0x80;
  const exponent = (u >> 4) & 0x07;
  const mantissa = u & 0x0f;
  let sample = ((mantissa << 3) + BIAS_DEC) << exponent;
  sample -= BIAS_DEC;
  return sign ? -sample : sample;
}
const BIAS_DEC = 0x84;

/** Downsample Float32 PCM at `inRate` to int16 @ 8 kHz, then µ-law encode. Returns the µ-law bytes. */
export function float32ToMulaw8k(input: Float32Array, inRate: number): Uint8Array {
  const ratio = inRate / 8000;
  const outLen = Math.floor(input.length / ratio);
  const out = new Uint8Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const f = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]));
    out[i] = linearToMulaw(f < 0 ? f * 0x8000 : f * 0x7fff);
  }
  return out;
}

/** Decode µ-law bytes to a Float32Array (values in [-1, 1]) at 8 kHz. */
export function mulaw8kToFloat32(bytes: Uint8Array): Float32Array {
  const out = new Float32Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) out[i] = mulawToLinear(bytes[i]) / 0x8000;
  return out;
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

export function b64ToBytes(b64: string): Uint8Array {
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

/** Schedules µ-law @ 8 kHz chunks for gapless playback through a Web Audio context. */
export class MulawPlayer {
  private ctx: AudioContext;
  private nextTime = 0;
  constructor() {
    this.ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
  }
  resume() {
    if (this.ctx.state === "suspended") void this.ctx.resume();
  }
  play(mulaw: Uint8Array) {
    const f32 = mulaw8kToFloat32(mulaw);
    if (!f32.length) return;
    const buf = this.ctx.createBuffer(1, f32.length, 8000);
    buf.getChannelData(0).set(f32);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const now = this.ctx.currentTime;
    if (this.nextTime < now) this.nextTime = now + 0.05;
    src.start(this.nextTime);
    this.nextTime += buf.duration;
  }
  close() {
    void this.ctx.close();
  }
}
