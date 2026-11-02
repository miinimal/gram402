// TypeScript client against the reference server (mocked chain). Run: clients/typescript/test.sh
import { Gram402Client, PaymentError } from './gram402';

const GW = 'http://127.0.0.1:18402';
const REQ = { model: 'echo', max_tokens: 64, messages: [{ role: 'user', content: 'hello' }] };
let paid = 0;
const pay = async (_to: string, nano: bigint, comment: string) => {
  paid++;
  await fetch(`${GW}/_test/pay`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment, amount: (nano * 4n).toString() }) });
};
const ok = (c: boolean, m: string) => { if (!c) { console.error('FAIL', m); process.exit(1); } };

(async () => {
  const cli = new Gram402Client(GW, pay, { pollMs: 50 });
  const a = await cli.chat(REQ);
  ok(a.choices[0].message.content === 'echo: hello', 'answer');
  ok(paid === 1 && !!cli.apiKey && !!a.gram402.receipt.payment, 'paid once, receipt bound to payment');
  await cli.chat(REQ);
  ok(paid === 1, 'credit reused');
  try { await new Gram402Client(GW, pay, { maxNano: 1n }).chat(REQ); ok(false, 'cap'); }
  catch (e) { ok(e instanceof PaymentError && String(e).includes('exceeds maxNano'), 'price cap'); }
  console.log('typescript client OK');
})();
