// GRAM-402 PayPal mandate client for TypeScript/JavaScript agents. No dependencies (global fetch).
//
//   import { MandateClient } from './paypal_mandate';
//   const agent = new MandateClient('https://gateway.example', process.env.MANDATE_ID!, process.env.MANDATE_SECRET!, { maxCents: 25 });
//   const answer = await agent.chat({ model: 'default', messages: [{ role: 'user', content: 'hi' }] });
//
// The owner approves the mandate once on PayPal. On HTTP 402 the client picks the paypal-mandate offer, refuses
// prices above maxCents, pays with X-PAYMENT and returns the answer; lastPayment holds the PayPal capture id and
// the remaining budget. Author: Aleksandr Shcherbakov (@miinimal).

export class PaymentError extends Error {}

const SCHEME = 'paypal-mandate';

function b64(s: string): string {
  return typeof btoa === 'function' ? btoa(s) : Buffer.from(s).toString('base64');
}

export class MandateClient {
  lastPayment?: any;
  private base: string;
  private maxCents: number;

  constructor(baseUrl: string, private mandate: string, private secret: string, opts: { maxCents?: number } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.maxCents = opts.maxCents ?? 100;
  }

  async status(): Promise<any> {
    const r = await fetch(`${this.base}/v1/mandates/${encodeURIComponent(this.mandate)}`,
                          { headers: { 'X-Mandate-Secret': this.secret } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new PaymentError(`HTTP ${r.status}: ${JSON.stringify(j)}`);
    return j;
  }

  async call(path: string, body: unknown): Promise<{ status: number; body: any }> {
    const post = (headers: Record<string, string>) => fetch(this.base + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
    let r = await post({});
    if (r.status !== 402) return { status: r.status, body: await r.json().catch(() => ({})) };
    const req = await r.json();
    const acc = (req.accepts || []).find((a: any) => a.scheme === SCHEME);
    if (!acc) throw new PaymentError('server offers no paypal-mandate payment');
    const price = Number(acc.maxAmountRequired);
    if (price > this.maxCents) throw new PaymentError(`price ${price} cents exceeds maxCents ${this.maxCents}`);
    const xpay = b64(JSON.stringify({ x402Version: 1, scheme: SCHEME,
      payload: { mandate: this.mandate, secret: this.secret, invoice: acc.extra.invoice } }));
    r = await post({ 'X-PAYMENT': xpay });
    const out = await r.json().catch(() => ({}));
    const g = out.gram402;
    const hdr = r.headers.get('x-payment-response');
    this.lastPayment = g?.payment ? { ...g.payment, mandate_left_cents: g.mandate_left_cents }
      : hdr ? JSON.parse(typeof atob === 'function' ? atob(hdr) : Buffer.from(hdr, 'base64').toString()) : undefined;
    return { status: r.status, body: out };
  }

  async chat(body: unknown): Promise<any> {
    const { status, body: out } = await this.call('/v1/chat/completions', body);
    if (status !== 200) throw new PaymentError(`HTTP ${status}: ${JSON.stringify(out).slice(0, 300)}`);
    return out;
  }
}
