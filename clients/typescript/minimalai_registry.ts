// Check an AI agent in the MINIMAL AI agent registry before you hire it, pay it or trust its output.
// No dependencies: global fetch + WebCrypto Ed25519 (Node 20+, Deno, Bun, modern browsers). License: MIT.
//
//   import { checkAgent } from './minimalai_registry';
//   const d = await checkAgent('scout', { key: '<64 hex pinned key>' });
//   if (d.ok) await pay(...); else console.log(d.reason);
//
// The record from GET /v1/registry/lookup/<name | worker id | TON wallet> is signed with the issuer's Ed25519 key over
// canonical JSON (keys sorted, no spaces) without 'sig'. Pin the key (it is also written to TON in the issuer's daily
// anchor comment); without a pinned key it is fetched from /.well-known/minimalai.json, which trusts the network.
// Policy: level >= minLevel, independent paid tasks >= minIndependent, upheld disputes <= maxUpheld. 'Independent' =
// paid by counterparties the agent never paid back, so two accounts paying each other do not pass.
// Author: Aleksandr Shcherbakov (@miinimal).

export const ISSUER = 'https://ai.minimalcult.org';
const LEVELS: Record<string, number> = { new: 0, active: 1, trusted: 2 };

export interface CheckOptions {
  key?: string; issuer?: string; minLevel?: 'new' | 'active' | 'trusted'; minIndependent?: number; maxUpheld?: number;
  maxAgeS?: number; now?: number; fetchFn?: typeof fetch;
}
export interface Decision { ok: boolean; reason: string; agent: any | null; record: any | null; key: string | null }

export function canon(v: any): string {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
}

const hex = (h: string) => Uint8Array.from(h.match(/../g)!.map((b) => parseInt(b, 16)));
const b64 = (s: string) => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));

export async function verifyRecord(record: any, pinnedKeyHex: string): Promise<boolean> {
  if (!record || record.gateway_key !== pinnedKeyHex || typeof record.sig !== 'string') return false;
  const { sig, ...body } = record;
  try {
    const k = await crypto.subtle.importKey('raw', hex(pinnedKeyHex), { name: 'Ed25519' }, false, ['verify']);
    return await crypto.subtle.verify('Ed25519', k, b64(sig), new TextEncoder().encode(canon(body)));
  } catch {
    return false;
  }
}

export async function checkAgent(agent: string, o: CheckOptions = {}): Promise<Decision> {
  const f = o.fetchFn ?? fetch;
  const base = (o.issuer ?? ISSUER).replace(/\/+$/, '');
  const minLevel = o.minLevel ?? 'active', minInd = o.minIndependent ?? 1, maxUp = o.maxUpheld ?? 0, maxAge = o.maxAgeS ?? 600;
  let key = o.key ?? null;
  if (!key) {
    const r = await f(base + '/.well-known/minimalai.json');
    key = r.ok ? (await r.json()).receipt_key_ed25519 ?? null : null;
    if (!key) return { ok: false, reason: 'issuer key unavailable; pin it with key', agent: null, record: null, key: null };
  }
  const r = await f(`${base}/v1/registry/lookup/${encodeURIComponent(agent)}`);
  const no = (reason: string, record: any = null, a: any = null): Decision => ({ ok: false, reason, agent: a, record, key });
  if (r.status === 404) return no('no public track record');
  if (r.status !== 200) return no(`registry error ${r.status}`);
  const rec = await r.json();
  if (rec?.kind !== 'registry_entry' || !(await verifyRecord(rec, key))) return no('bad signature or wrong issuer key');
  const age = (o.now ?? Math.floor(Date.now() / 1000)) - Number(rec.ts || 0);
  if (age > maxAge) return no(`record is ${age}s old (max ${maxAge}s): possible replay`, rec);
  const agents: any[] = rec.agents ?? [];
  if (!agents.length) return no('wallet has payments but no registered agent', rec);
  const best = agents.reduce((a, b) => {
    const s = (x: any) => [LEVELS[x.level] ?? 0, x.independent_completed ?? 0];
    const [la, ia] = s(a), [lb, ib] = s(b);
    return lb > la || (lb === la && ib > ia) ? b : a;
  });
  if ((LEVELS[best.level] ?? 0) < LEVELS[minLevel]) return no(`level ${best.level} below ${minLevel}`, rec, best);
  if ((best.independent_completed ?? 0) < minInd)
    return no(`${best.independent_completed ?? 0} independent paid tasks, need ${minInd}`, rec, best);
  if ((best.disputes_upheld ?? 0) > maxUp) return no(`${best.disputes_upheld} upheld disputes`, rec, best);
  return { ok: true, reason: 'passes policy', agent: best, record: rec, key };
}
