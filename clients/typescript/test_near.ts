import { NearMandateClient, PaymentError } from './near_mandate';

const BASE = 'http://127.0.0.1:18404';

async function main() {
  const pay = async (contract: string, method: string, args: Record<string, unknown>) => {
    if (contract !== 'mandate.gram402.testnet' || method !== 'pay') throw new Error('bad call');
    const r = await fetch(`${BASE}/_test/near_pay`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args) });
    return { txHash: (await r.json()).tx, sender: 'agent.testnet' };
  };
  const agent = new NearMandateClient(BASE, 7, pay, 'mandate.gram402.testnet', { maxYocto: 10n ** 21n, pollMs: 100 });
  const out = await agent.chat({ model: 'echo', messages: [{ role: 'user', content: 'hello from ts on near' }] });
  if (!out.choices[0].message.content.startsWith('echo:')) throw new Error('bad answer');
  if (agent.lastPayment?.network !== 'near-testnet') throw new Error('no payment ' + JSON.stringify(agent.lastPayment));
  try {
    await new NearMandateClient(BASE, 7, pay, 'mandate.gram402.testnet', { maxYocto: 1n }).chat({ model: 'echo', messages: [] });
    throw new Error('cap not enforced');
  } catch (e) { if (!(e instanceof PaymentError)) throw e; }
  console.log('typescript near mandate client OK');
}
main().catch(e => { console.error(e); process.exit(1); });
