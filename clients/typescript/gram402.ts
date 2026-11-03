// GRAM-402 client for TypeScript/JavaScript agents (spec: docs/protocol/GRAM-402.md). No dependencies (global fetch).
//
//   import { Gram402Client } from './gram402';
//   const cli = new Gram402Client('https://gateway.example', async (to, nano, comment) => {
//     await wallet.sendTransfer({ ..., sendMode: SendMode.PAY_GAS_SEPARATELY + SendMode.IGNORE_ERRORS,  // fee on top
//       messages: [internal({ to, value: nano, bounce: false, body: comment })] });
//   });
//   const answer = await cli.chat({ model: 'Qwen/Qwen3-8B', messages: [{ role: 'user', content: 'hi' }] });
//
// Pays on HTTP 402 through the pay callback (any TON wallet library), retries with X-PAYMENT until the transfer is
// on chain, keeps the returned api_key for the remaining credit and pays again when it runs out.
// Author: Aleksandr Shcherbakov (@miinimal).

export type PayFn = (payTo: string, amountNano: bigint, comment: string) => Promise<void>;

export class PaymentError extends Error {}

export interface Options { maxNano?: bigint; waitMs?: number; pollMs?: number; apiKey?: string }

export class Gram402Client {
  apiKey?: string;
  lastPayment?: any;
  private base: string;
  private maxNano: bigint;
  private waitMs: number;
  private pollMs: number;

  constructor(baseUrl: string, private pay: PayFn, opts: Options = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.maxNano = opts.maxNano ?? 200_000_000n;
    this.waitMs = opts.waitMs ?? 120_000;
    this.pollMs = opts.pollMs ?? 5_000;
    this.apiKey = opts.apiKey;
  }

  private async post(body: unknown, headers: Record<string, string>) {
    const r = await fetch(`${this.base}/v1/chat/completions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
    return { status: r.status, retry: r.headers.has('retry-after'), json: (await r.json().catch(() => ({}))) as any };
  }

  async chat(body: unknown): Promise<any> {
    if (this.apiKey) {
      const r = await this.post(body, { Authorization: `Bearer ${this.apiKey}` });
      if (r.status !== 402) return done(r);
      this.apiKey = undefined; // credit spent: pay again
    }
    const q = await this.post(body, {});
    if (q.status !== 402) return done(q);
    const acc = (q.json.accepts || []).find((a: any) => a.scheme === 'ton-transfer');
    if (!acc) throw new PaymentError('server offers no ton-transfer payment');
    const amount = BigInt(acc.maxAmountRequired);
    if (amount > this.maxNano) throw new PaymentError(`price ${amount} nano-GRAM exceeds maxNano ${this.maxNano}`);
    await this.pay(acc.payTo, amount, acc.extra.comment);
    const xpay = Buffer.from(JSON.stringify({ x402Version: 1, scheme: acc.scheme, network: acc.network,
      payload: { invoice: acc.extra.invoice, secret: acc.extra.secret } })).toString('base64');
    const deadline = Date.now() + this.waitMs;
    for (;;) {
      const r = await this.post(body, { 'X-PAYMENT': xpay });
      const p = r.json?.gram402?.payment ?? r.json?.minimalai?.payment;
      if (p) { this.apiKey = p.api_key; this.lastPayment = p; }
      if ((r.status === 402 || r.status === 503) && r.retry && Date.now() < deadline) { await new Promise((s) => setTimeout(s, this.pollMs)); continue; }
      return done(r);
    }
  }

  /** Call any GRAM-402 endpoint by absolute URL (e.g. a GRAM-402 Bazaar listing): pays the 402 invoice, polls while
   *  the transfer is not visible yet (402 + Retry-After), returns the final Response. */
  async call(url: string, init: RequestInit = {}): Promise<Response> {
    const send = (extra: Record<string, string>) =>
      fetch(url, { ...init, headers: { 'Content-Type': 'application/json', ...(init.headers as any), ...extra } });
    const first = await send({});
    if (first.status !== 402) return first;
    const q: any = await first.json().catch(() => ({}));
    const acc = (q.accepts || []).find((a: any) => a.scheme === 'ton-transfer');
    if (!acc) throw new PaymentError('server offers no ton-transfer payment');
    const amount = BigInt(acc.maxAmountRequired);
    if (amount > this.maxNano) throw new PaymentError(`price ${amount} nano-GRAM exceeds maxNano ${this.maxNano}`);
    await this.pay(acc.payTo, amount, acc.extra.comment);
    const xpay = Buffer.from(JSON.stringify({ x402Version: 1, scheme: acc.scheme, network: acc.network,
      payload: { invoice: acc.extra.invoice, secret: acc.extra.secret } })).toString('base64');
    const deadline = Date.now() + this.waitMs;
    for (;;) {
      const r = await send({ 'X-PAYMENT': xpay });
      if ((r.status === 402 || r.status === 503) && r.headers.has('retry-after') && Date.now() < deadline) {
        await new Promise((s) => setTimeout(s, this.pollMs)); continue;
      }
      this.lastPayment = r.headers.get('x-payment-response') ?? undefined;
      return r;
    }
  }
}

function done(r: { status: number; json: any }) {
  if (r.status !== 200) throw new PaymentError(`HTTP ${r.status}: ${JSON.stringify(r.json).slice(0, 300)}`);
  return r.json;
}
