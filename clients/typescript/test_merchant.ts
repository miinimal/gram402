// Merchant paywall test with a stubbed facilitator (no network). Run: bash test_merchant.sh
import { Merchant, withPaywall, expressPaywall } from './gram402_merchant';
import { Gram402Client } from './gram402';

const calls: any[] = [];
let polls = 1, settled = false, settleAny = false;
(globalThis as any).fetch = async (url: string, init: any) => {
  const body = JSON.parse(init.body);
  calls.push([url, init.headers.Authorization, body]);
  const json = (o: unknown, status = 200) => new Response(JSON.stringify(o), { status });
  if (init.headers.Authorization !== 'Bearer k') return json({ detail: 'invalid key' }, 401);
  if (url.endsWith('/invoices')) return json({ x402: { x402Version: 1, accepts: [{ scheme: 'ton-transfer', network: 'ton-testnet', payTo: body.pay_to, maxAmountRequired: String(body.amount_nano), extra: { invoice: 'i1', secret: 's1', comment: 'f402:i1' } }] } });
  if (url.endsWith('/settle')) {
    if (body.payment !== 'good' && !settleAny) return json({ isValid: false, invalidReason: 'unknown invoice or wrong secret' });
    if (polls-- > 0) return json({ isValid: false, retry: true, invalidReason: 'payment not found on chain yet' });
    if (settled) return json({ isValid: false, invalidReason: 'already settled' });
    settled = true;
    return json({ isValid: true, success: true, x_payment_response: 'cmVjZWlwdA==' });
  }
  return json({}, 404);
};

function assert(c: unknown, m: string) { if (!c) { console.error('FAIL', m); process.exit(1); } }

(async () => {
  const m = new Merchant('k', 'UQmerchant', 'https://fac.example');
  const h = withPaywall(m, { '/v1/weather': 20_000_000n }, () => new Response('{"temp_c":19}', { headers: { 'Content-Type': 'application/json' } }));
  assert((await h(new Request('https://shop.example/free'))).status === 200, 'unpriced path passes');
  const r1 = await h(new Request('https://shop.example/v1/weather'));
  const b1: any = await r1.json();
  assert(r1.status === 402 && b1.accepts[0].payTo === 'UQmerchant' && b1.accepts[0].maxAmountRequired === '20000000', '402 invoice');
  const r2 = await h(new Request('https://shop.example/v1/weather', { headers: { 'X-PAYMENT': 'good' } }));
  assert(r2.status === 402 && r2.headers.get('retry-after') === '5', 'retry while not on chain');
  const r3 = await h(new Request('https://shop.example/v1/weather', { headers: { 'X-PAYMENT': 'good' } }));
  assert(r3.status === 200 && r3.headers.get('x-payment-response') === 'cmVjZWlwdA==' && (await r3.json() as any).temp_c === 19, 'paid passes');
  const r4 = await h(new Request('https://shop.example/v1/weather', { headers: { 'X-PAYMENT': 'good' } }));
  assert(r4.status === 402 && !r4.headers.get('retry-after'), 'no replay');

  // express
  const mw = expressPaywall(m, { '/v1/weather': 20_000_000n });
  const res: any = { headers: {} as any, statusCode: 200, body: '', setHeader(k: string, v: string) { this.headers[k.toLowerCase()] = v; }, end(b: string) { this.body = b; } };
  let nexted = false;
  await mw({ path: '/v1/weather', headers: {} }, res, () => { nexted = true; });
  assert(res.statusCode === 402 && JSON.parse(res.body).accepts && !nexted, 'express 402');
  const res2: any = { ...res, headers: {}, statusCode: 200 };
  await mw({ path: '/v1/weather', headers: { 'x-payment': 'bad' } }, res2, () => { nexted = true; });
  assert(res2.statusCode === 402 && !nexted, 'express rejects bad payment');
  await mw({ path: '/other', headers: {} }, res, () => { nexted = true; });
  assert(nexted, 'express passes unpriced');
  // wrong key -> 503 from invoice
  const bad = withPaywall(new Merchant('x', 'UQmerchant', 'https://fac.example'), { '/p': 1n }, () => new Response('x'));
  assert((await bad(new Request('https://shop.example/p'))).status === 503, 'facilitator error -> 503');
  // agent side: Gram402Client.call pays and polls against the fetch-style paywall
  polls = 2; settled = false;
  const shopFetch = (globalThis as any).fetch;
  (globalThis as any).fetch = (u: any, i: any) => String(u).startsWith('https://shop.example') ? h(new Request(u, i)) : shopFetch(u, i);
  const paid: any[] = [];
  const cli = new Gram402Client('https://shop.example', async (to, nano, c) => { paid.push([to, nano, c]); }, { pollMs: 1, maxNano: 10n ** 8n });
  const cap = new Gram402Client('https://shop.example', async () => { paid.push('x'); }, { maxNano: 1n });
  let capped = false;
  try { await cap.call('https://shop.example/v1/weather'); } catch (e) { capped = String(e).includes('exceeds maxNano'); }
  assert(capped && paid.length === 0, 'agent price cap');
  // full agent flow: the stub settles the X-PAYMENT the client builds once 'paid' (settle accepts it after one poll)
  settleAny = true; polls = 1; settled = false;
  const ok = await cli.call('https://shop.example/v1/weather');
  assert(ok.status === 200 && paid[0][0] === 'UQmerchant' && paid[0][1] === 20_000_000n && paid[0][2] === 'f402:i1', 'agent paid and got the answer');
  assert(cli.lastPayment === 'cmVjZWlwdA==', 'agent got receipt');
  console.log('merchant ts OK');
})();
