// Sell any HTTP API to AI agents for GRAM through the GRAM-402 facilitator. Payments go straight to your wallet.
// No dependencies (global fetch). Author: Aleksandr Shcherbakov (@miinimal).
//
// Fetch-style handlers (Hono, Bun, Deno, Cloudflare Workers, Next.js route handlers):
//   const m = new Merchant('mai-...', 'UQ...your wallet');
//   export default { fetch: withPaywall(m, { '/v1/weather': 20_000_000n }, handler) };
// Express / Connect:
//   app.use(expressPaywall(m, { '/v1/weather': 20_000_000n }));
//
// Unpaid calls get HTTP 402 with the invoice; a call with X-PAYMENT is settled once and passed through with
// X-PAYMENT-RESPONSE; a payment not yet visible on chain gets 402 with Retry-After so GRAM-402 clients poll.

export const FACILITATOR = 'https://ai.minimalcult.org';

export interface Settlement {
  success?: boolean; isValid?: boolean; retry?: boolean; invalidReason?: string;
  transaction?: string; payer?: string; x_payment_response?: string; status?: number;
}

export class Merchant {
  private base: string;
  constructor(private apiKey: string, public payTo: string, base: string = FACILITATOR) {
    this.base = base.replace(/\/+$/, '');
  }

  private async post(path: string, body: unknown): Promise<any> {
    try {
      const r = await fetch(this.base + path, {
        method: 'POST', body: JSON.stringify(body),
        headers: { Authorization: 'Bearer ' + this.apiKey, 'Content-Type': 'application/json' },
      });
      const out = await r.json().catch(() => ({}));
      return r.ok ? out : { success: false, isValid: false, status: r.status, invalidReason: JSON.stringify(out).slice(0, 300) };
    } catch (e) {
      return { success: false, isValid: false, status: 0, invalidReason: 'facilitator unreachable: ' + String(e) };
    }
  }

  invoice(amountNano: bigint, resource = '', description = ''): Promise<any> {
    return this.post('/v1/facilitator/invoices', { pay_to: this.payTo, amount_nano: Number(amountNano), resource, description });
  }
  verify(xPayment: string): Promise<Settlement> { return this.post('/v1/facilitator/verify', { payment: xPayment }); }
  settle(xPayment: string): Promise<Settlement> { return this.post('/v1/facilitator/settle', { payment: xPayment }); }
}

export type Prices = Record<string, bigint>;

type Gate =
  | { pass: true; receipt: string }
  | { pass: false; status: number; body: unknown; headers: Record<string, string> };

export async function gate(m: Merchant, price: bigint, path: string, xPayment: string | null, description = ''): Promise<Gate> {
  if (!xPayment) {
    const inv = await m.invoice(price, path, description);
    if (!inv.x402) return { pass: false, status: 503, body: { error: 'payment service unavailable', detail: inv.invalidReason }, headers: {} };
    return { pass: false, status: 402, body: inv.x402, headers: {} };
  }
  const s = await m.settle(xPayment);
  if (s.success && s.x_payment_response) return { pass: true, receipt: s.x_payment_response };
  return { pass: false, status: 402, body: { error: s.invalidReason || 'payment rejected' }, headers: s.retry ? { 'Retry-After': '5' } : {} };
}

export function withPaywall(m: Merchant, prices: Prices, handler: (req: Request) => Response | Promise<Response>, description = '') {
  return async (req: Request): Promise<Response> => {
    const path = new URL(req.url).pathname;
    const price = prices[path];
    if (price === undefined) return handler(req);
    const g = await gate(m, price, path, req.headers.get('x-payment'), description);
    if (!g.pass) {
      return new Response(JSON.stringify(g.body), { status: g.status, headers: { 'Content-Type': 'application/json', ...g.headers } });
    }
    const res = await handler(req);
    const out = new Response(res.body, res);
    out.headers.set('X-PAYMENT-RESPONSE', g.receipt);
    return out;
  };
}

export function expressPaywall(m: Merchant, prices: Prices, description = '') {
  return async (req: any, res: any, next: (e?: unknown) => void) => {
    const path: string = (req.path ?? (req.url || '').split('?')[0]);
    const price = prices[path];
    if (price === undefined) return next();
    try {
      const g = await gate(m, price, path, req.headers['x-payment'] ?? null, description);
      if (!g.pass) {
        for (const [k, v] of Object.entries(g.headers)) res.setHeader(k, v);
        res.statusCode = g.status;
        res.setHeader('Content-Type', 'application/json');
        return res.end(JSON.stringify(g.body));
      }
      res.setHeader('X-PAYMENT-RESPONSE', g.receipt);
      next();
    } catch (e) {
      next(e);
    }
  };
}
