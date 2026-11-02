// GRAM-402 NEAR mandate client for TypeScript/JavaScript agents. No dependencies (global fetch).
//
//   import { NearMandateClient } from './near_mandate';
//   const agent = new NearMandateClient('https://api.example', 1, async (contract, method, args) => {
//     const out = await account.callFunction({ contractId: contract, methodName: method, args, gas: 30_000_000_000_000n });
//     return { txHash: out.transaction.hash, sender: account.accountId };
//   }, { maxYocto: 10n ** 22n });
//   const answer = await agent.chat({ model: 'default', messages: [{ role: 'user', content: 'hi' }] });
//
// On HTTP 402 the client takes the near-mandate offer, refuses prices above maxYocto, calls `pay` on the mandate
// contract through your signing callback, then retries with X-PAYMENT until the payment is visible on chain.
// Author: Aleksandr Shcherbakov (@miinimal).

export type NearPay = (contract: string, method: string, args: Record<string, unknown>) => Promise<{ txHash: string; sender: string }>;

export class PaymentError extends Error {}

const SCHEME = 'near-mandate';
const b64 = (s: string) => (typeof btoa === 'function' ? btoa(s) : Buffer.from(s).toString('base64'));
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export class NearMandateClient {
  lastPayment?: any;
  private base: string;
  private maxYocto: bigint;
  private waitMs: number;
  private pollMs: number;

  constructor(baseUrl: string, private mandateId: number, private pay: NearPay,
              opts: { maxYocto?: bigint; waitMs?: number; pollMs?: number } = {}) {
    this.base = baseUrl.replace(/\/+$/, '');
    this.maxYocto = opts.maxYocto ?? 10n ** 22n;
    this.waitMs = opts.waitMs ?? 60_000;
    this.pollMs = opts.pollMs ?? 3_000;
  }

  private safe(path: string): string {
    // Paths may come from the model: never let them change the host the secret or payment goes to.
    if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new PaymentError('path must be an absolute path on the configured server');
    if (new URL(this.base + path).host !== new URL(this.base).host) throw new PaymentError('path changes the host');
    return path;
  }

  async call(path: string, body: unknown): Promise<{ status: number; body: any }> {
    path = this.safe(path);
    const post = (headers: Record<string, string>) => fetch(this.base + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
    let r = await post({});
    if (r.status !== 402) return { status: r.status, body: await r.json().catch(() => ({})) };
    const req = await r.json();
    const acc = (req.accepts || []).find((a: any) => a.scheme === SCHEME);
    if (!acc) throw new PaymentError('server offers no near-mandate payment');
    const price = BigInt(acc.maxAmountRequired);
    if (price > this.maxYocto) throw new PaymentError(`price ${price} yoctoNEAR exceeds maxYocto ${this.maxYocto}`);
    const ex = acc.extra;
    const tx = await this.pay(ex.contract, ex.method || 'pay', { mandate_id: this.mandateId, ...ex.args });
    if (!tx?.txHash || !tx?.sender) throw new PaymentError('pay callback must return { txHash, sender }');
    const xpay = b64(JSON.stringify({ x402Version: 1, scheme: SCHEME,
      payload: { invoice: ex.invoice, secret: ex.secret, tx_hash: tx.txHash, sender: tx.sender } }));
    const deadline = Date.now() + this.waitMs;
    for (;;) {
      r = await post({ 'X-PAYMENT': xpay });
      if (r.status === 402 && r.headers.get('retry-after') && Date.now() < deadline) { await sleep(this.pollMs); continue; }
      const out = await r.json().catch(() => ({}));
      this.lastPayment = out?.gram402?.payment;
      return { status: r.status, body: out };
    }
  }

  async chat(body: unknown): Promise<any> {
    const { status, body: out } = await this.call('/v1/chat/completions', body);
    if (status !== 200) throw new PaymentError(`HTTP ${status}: ${JSON.stringify(out).slice(0, 300)}`);
    return out;
  }
}
