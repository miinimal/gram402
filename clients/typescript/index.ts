// gram402: HTTP 402 payments for AI agents (GRAM on TON, PayPal mandates, NEAR mandate contracts).
// Each client throws its own PaymentError; they are re-exported under rail-specific names.
export { Gram402Client, PaymentError as GramPaymentError } from './gram402';
export type { PayFn, Options } from './gram402';
export { Merchant, gate, withPaywall, expressPaywall, FACILITATOR } from './gram402_merchant';
export type { Settlement, Prices } from './gram402_merchant';
export { MandateClient, PaymentError as PayPalPaymentError } from './paypal_mandate';
export { NearMandateClient, PaymentError as NearPaymentError } from './near_mandate';
export type { NearPay } from './near_mandate';
export { checkAgent, verifyRecord, canon as registryCanon } from './minimalai_registry';
export type { CheckOptions, Decision } from './minimalai_registry';
