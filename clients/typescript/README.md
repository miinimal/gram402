# gram402 (TypeScript / JavaScript)

HTTP 402 payments for AI agents. No dependencies (global `fetch`, Node 18+, browsers, Deno, Bun).

```ts
import { MandateClient, NearMandateClient, Gram402Client, withPaywall } from 'gram402';

// PayPal: the owner approved a spending mandate once; the agent pays each 402 within its limits.
const agent = new MandateClient('https://api.example', MANDATE_ID, MANDATE_SECRET, { maxCents: 25 });
const answer = await agent.chat({ model: 'default', messages: [{ role: 'user', content: 'hi' }] });
```

Protocol, server, NEAR contract and docs: https://github.com/miinimal/gram402 (MIT).
