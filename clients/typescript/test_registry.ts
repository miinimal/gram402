// Standalone test of minimalai_registry.ts: records signed in JS with a fresh Ed25519 key, served by a fake fetch.
import { checkAgent, verifyRecord, canon } from './minimalai_registry';
const assert = (c: any, m: string) => { if (!c) { console.error('FAIL', m); process.exit(1); } };
const toHex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, '0')).join('');
const toB64 = (b: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(b)));
(async () => {
  const kp = (await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])) as CryptoKeyPair;
  const key = toHex(await crypto.subtle.exportKey('raw', kp.publicKey));
  const now = Math.floor(Date.now() / 1000);
  const agent = (name: string, level: string, ind: number, upheld = 0) =>
    ({ worker: 'a'.repeat(32), name, level, independent_completed: ind, disputes_upheld: upheld, effective_counterparties: 2.273 });
  const sign = async (agents: any[], ts = now) => {
    const rec: any = { v: 1, kind: 'registry_entry', issuer: 'MINIMALAI', ts, query: 'x', agents, wallet: null,
      passport: null, chain_head: 'h', gateway_key: key };
    return { ...rec, sig: toB64(await crypto.subtle.sign('Ed25519', kp.privateKey, new TextEncoder().encode(canon(rec)))) };
  };
  const db: Record<string, any> = { scout: await sign([agent('scout', 'active', 3)]), rookie: await sign([agent('rookie', 'new', 0)]),
    cheat: await sign([agent('cheat', 'active', 2, 1)]), wallet: await sign([agent('w1', 'new', 0), agent('w2', 'trusted', 12)]) };
  let tamper = false;
  const fetchFn: any = async (url: string) => {
    if (url.endsWith('/.well-known/minimalai.json')) return new Response(JSON.stringify({ receipt_key_ed25519: key }));
    const n = decodeURIComponent(url.split('/').pop()!);
    if (!db[n]) return new Response('{"detail":"no"}', { status: 404 });
    const rec = tamper ? { ...db[n], agents: [{ ...db[n].agents[0], independent_completed: 99 }] } : db[n];
    return new Response(JSON.stringify(rec));
  };
  const o = { key, fetchFn };
  assert((await checkAgent('scout', o)).ok, 'scout passes');
  assert((await checkAgent('rookie', o)).reason.includes('below active'), 'rookie refused');
  assert((await checkAgent('rookie', { ...o, minLevel: 'new', minIndependent: 0 })).ok, 'rookie with loose policy');
  assert((await checkAgent('cheat', o)).reason.includes('upheld'), 'upheld dispute refused');
  const w = await checkAgent('wallet', o);
  assert(w.ok && w.agent.name === 'w2', 'strongest agent behind a wallet');
  assert((await checkAgent('nobody', o)).reason === 'no public track record', 'unknown');
  assert((await checkAgent('scout', { ...o, key: '00'.repeat(32) })).reason.includes('signature'), 'wrong key');
  tamper = true; assert((await checkAgent('rookie', o)).reason.includes('signature'), 'tampered'); tamper = false;
  assert((await checkAgent('scout', { ...o, now: now + 3600 })).reason.includes('replay'), 'replay');
  assert((await checkAgent('scout', { fetchFn })).key === key, 'key fetched');
  assert(await verifyRecord(db.scout, key), 'verifyRecord');
  console.log('minimalai_registry ts OK');
})();
