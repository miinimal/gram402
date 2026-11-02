import { MandateClient, PaymentError } from './paypal_mandate';

const BASE = 'http://127.0.0.1:18403';

async function main() {
  const m = await (await fetch(`${BASE}/v1/mandates`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ budget_cents: 30, per_request_cents: 10, days: 1 }) })).json();
  await fetch(`${BASE}/v1/mandates/${m.mandate}/approved`);
  const agent = new MandateClient(BASE, m.mandate, m.secret, { maxCents: 10 });
  const out = await agent.chat({ model: 'echo', messages: [{ role: 'user', content: 'hello from ts' }] });
  if (!out.choices[0].message.content.startsWith('echo:')) throw new Error('bad answer');
  if (!String(agent.lastPayment.transaction).startsWith('CAP') || agent.lastPayment.mandate_left_cents !== 20)
    throw new Error('bad payment ' + JSON.stringify(agent.lastPayment));
  if ((await agent.status()).spent_cents !== 10) throw new Error('status');
  try {
    await new MandateClient(BASE, m.mandate, m.secret, { maxCents: 5 }).chat({ model: 'echo', messages: [] });
    throw new Error('cap not enforced');
  } catch (e) { if (!(e instanceof PaymentError)) throw e; }
  console.log('typescript paypal mandate client OK');
}
main().catch(e => { console.error(e); process.exit(1); });
