//! near-mandate: spending mandates for AI agents on NEAR.
//!
//! An owner funds a mandate with NEAR and names the agent account, a cap per payment, an expiry and the merchants
//! the agent may pay. The agent calls `pay` with an invoice id from an HTTP 402 offer; the contract checks every
//! limit and transfers the amount straight to the merchant. Each invoice id can be paid once, and the payment is
//! recorded on chain, so a merchant verifies it with one view call (`get_payment`). The owner can top up or revoke
//! at any time; revoking refunds the remainder. Limits live in the contract, not in the model or the agent's code.
//!
//! The agent can hold a function-call access key restricted to this contract and the `pay` method: even a fully
//! compromised agent cannot move the owner's funds anywhere except to allowed merchants, within the limits.
//! Author: Aleksandr Shcherbakov (@miinimal). License: MIT.
use near_sdk::store::{IterableMap, LookupMap};
use near_sdk::{env, log, near, require, AccountId, NearToken, PanicOnDefault, Promise};

pub type MandateId = u64;

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    Active,
    Revoked,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug)]
pub struct Mandate {
    pub owner: AccountId,
    pub agent: AccountId,
    pub budget: NearToken,
    pub spent: NearToken,
    pub per_payment_cap: NearToken,
    pub expires_at_ms: u64,
    pub merchants: Vec<AccountId>,
    pub status: Status,
    pub payments: u32,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq)]
pub struct Payment {
    pub mandate_id: MandateId,
    pub merchant: AccountId,
    pub amount: NearToken,
    pub resource: String,
    pub paid_at_ms: u64,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Contract {
    next_id: MandateId,
    mandates: IterableMap<MandateId, Mandate>,
    payments: LookupMap<String, Payment>,
}

const MAX_MERCHANTS: usize = 32;
const MAX_INVOICE_LEN: usize = 64;
const MAX_RESOURCE_LEN: usize = 128;

#[near]
impl Contract {
    #[init]
    pub fn new() -> Self {
        Self { next_id: 1, mandates: IterableMap::new(b"m"), payments: LookupMap::new(b"p") }
    }

    /// Owner creates and funds a mandate. The attached deposit is the budget.
    /// `merchants` empty means any merchant (not recommended).
    #[payable]
    pub fn create_mandate(&mut self, agent: AccountId, per_payment_cap: NearToken, valid_for_ms: u64,
                          merchants: Vec<AccountId>) -> MandateId {
        let budget = env::attached_deposit();
        require!(!budget.is_zero(), "attach the budget as deposit");
        require!(!per_payment_cap.is_zero() && per_payment_cap <= budget, "cap must be > 0 and <= budget");
        require!(valid_for_ms > 0, "validity must be > 0");
        require!(merchants.len() <= MAX_MERCHANTS, "too many merchants");
        let owner = env::predecessor_account_id();
        require!(agent != owner, "the agent must be a separate account");
        let id = self.next_id;
        self.next_id += 1;
        self.mandates.insert(id, Mandate {
            owner: owner.clone(), agent: agent.clone(), budget, spent: NearToken::from_yoctonear(0), per_payment_cap,
            expires_at_ms: env::block_timestamp_ms().saturating_add(valid_for_ms), merchants, status: Status::Active,
            payments: 0,
        });
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"1.0.0\",\"event\":\"mandate_created\",\"data\":[{{\"mandate_id\":{},\"owner\":\"{}\",\"agent\":\"{}\",\"budget\":\"{}\"}}]}}",
             id, owner, agent, budget.as_yoctonear());
        id
    }

    /// Owner adds funds to an active mandate.
    #[payable]
    pub fn top_up(&mut self, mandate_id: MandateId) {
        let m = self.mandates.get_mut(&mandate_id).unwrap_or_else(|| env::panic_str("unknown mandate"));
        require!(env::predecessor_account_id() == m.owner, "only the owner");
        require!(m.status == Status::Active, "mandate is revoked");
        m.budget = m.budget.saturating_add(env::attached_deposit());
    }

    /// Agent pays a merchant for one invoice. Every limit is checked here; the money goes straight to the merchant.
    pub fn pay(&mut self, mandate_id: MandateId, merchant: AccountId, amount: NearToken, invoice: String,
               resource: String) -> Promise {
        require!(!invoice.is_empty() && invoice.len() <= MAX_INVOICE_LEN, "bad invoice id");
        require!(resource.len() <= MAX_RESOURCE_LEN, "resource too long");
        require!(!amount.is_zero(), "amount must be > 0");
        require!(self.payments.get(&invoice).is_none(), "invoice already paid");
        let now = env::block_timestamp_ms();
        let m = self.mandates.get_mut(&mandate_id).unwrap_or_else(|| env::panic_str("unknown mandate"));
        require!(env::predecessor_account_id() == m.agent, "only the mandate's agent can pay");
        require!(m.status == Status::Active, "mandate is revoked");
        require!(now < m.expires_at_ms, "mandate expired");
        require!(m.merchants.is_empty() || m.merchants.contains(&merchant), "merchant not allowed by this mandate");
        require!(amount <= m.per_payment_cap, "amount exceeds the per-payment cap");
        let spent = m.spent.saturating_add(amount);
        require!(spent <= m.budget, "mandate budget exhausted");
        m.spent = spent;
        m.payments += 1;
        self.payments.insert(invoice.clone(), Payment { mandate_id, merchant: merchant.clone(), amount,
                                                         resource: resource.clone(), paid_at_ms: now });
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"1.0.0\",\"event\":\"paid\",\"data\":[{{\"mandate_id\":{},\"merchant\":\"{}\",\"amount\":\"{}\",\"invoice\":\"{}\"}}]}}",
             mandate_id, merchant, amount.as_yoctonear(), invoice);
        Promise::new(merchant).transfer(amount)
    }

    /// Owner revokes the mandate; the unspent budget goes back to the owner.
    pub fn revoke(&mut self, mandate_id: MandateId) -> NearToken {
        let m = self.mandates.get_mut(&mandate_id).unwrap_or_else(|| env::panic_str("unknown mandate"));
        require!(env::predecessor_account_id() == m.owner, "only the owner");
        require!(m.status == Status::Active, "already revoked");
        m.status = Status::Revoked;
        let refund = m.budget.saturating_sub(m.spent);
        let owner = m.owner.clone();
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"1.0.0\",\"event\":\"revoked\",\"data\":[{{\"mandate_id\":{},\"refund\":\"{}\"}}]}}",
             mandate_id, refund.as_yoctonear());
        if !refund.is_zero() {
            Promise::new(owner).transfer(refund).detach();
        }
        refund
    }

    pub fn get_mandate(&self, mandate_id: MandateId) -> Option<Mandate> {
        self.mandates.get(&mandate_id).cloned()
    }

    /// Merchant-side verification: the invoice was paid, to whom, how much, under which mandate.
    pub fn get_payment(&self, invoice: String) -> Option<Payment> {
        self.payments.get(&invoice).cloned()
    }

    pub fn mandates_of(&self, account: AccountId, from: u64, limit: u64) -> Vec<(MandateId, Mandate)> {
        self.mandates.iter().filter(|(_, m)| m.owner == account || m.agent == account)
            .skip(from as usize).take(limit.min(50) as usize).map(|(k, v)| (*k, v.clone())).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::testing_env;

    fn near(n: u128) -> NearToken { NearToken::from_millinear(n) }

    fn ctx(who: usize, deposit: NearToken, ts_ms: u64) {
        testing_env!(VMContextBuilder::new().predecessor_account_id(accounts(who)).attached_deposit(deposit)
            .block_timestamp(ts_ms * 1_000_000).build());
    }

    // accounts(0) owner, accounts(1) agent, accounts(2) merchant, accounts(3) other merchant
    fn setup() -> (Contract, MandateId) {
        ctx(0, near(1000), 1_000);
        let mut c = Contract::new();
        let id = c.create_mandate(accounts(1), near(300), 60_000, vec![accounts(2)]);
        (c, id)
    }

    #[test]
    fn agent_pays_within_limits_and_merchant_can_verify() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(300), "inv-1".into(), "/v1/weather".into());
        let _ = c.pay(id, accounts(2), near(300), "inv-2".into(), "/v1/weather".into());
        let m = c.get_mandate(id).unwrap();
        assert_eq!(m.spent, near(600));
        assert_eq!(m.payments, 2);
        let p = c.get_payment("inv-1".into()).unwrap();
        assert_eq!((p.merchant, p.amount, p.mandate_id), (accounts(2), near(300), id));
    }

    #[test]
    #[should_panic(expected = "invoice already paid")]
    fn invoice_paid_once() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(100), "inv".into(), "".into());
        let _ = c.pay(id, accounts(2), near(100), "inv".into(), "".into());
    }

    #[test]
    #[should_panic(expected = "per-payment cap")]
    fn cap_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(301), "inv".into(), "".into());
    }

    #[test]
    #[should_panic(expected = "budget exhausted")]
    fn budget_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        for i in 0..4 {
            let _ = c.pay(id, accounts(2), near(300), format!("inv-{i}"), "".into());
        }
    }

    #[test]
    #[should_panic(expected = "merchant not allowed")]
    fn merchant_allowlist_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(3), near(100), "inv".into(), "".into());
    }

    #[test]
    #[should_panic(expected = "only the mandate's agent")]
    fn only_agent_pays() {
        let (mut c, id) = setup();
        ctx(3, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(100), "inv".into(), "".into());
    }

    #[test]
    #[should_panic(expected = "mandate expired")]
    fn expiry_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 61_001);
        let _ = c.pay(id, accounts(2), near(100), "inv".into(), "".into());
    }

    #[test]
    fn revoke_stops_payments_and_refunds() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(250), "inv".into(), "".into());
        ctx(0, NearToken::from_yoctonear(0), 3_000);
        assert_eq!(c.revoke(id), near(750));
        assert_eq!(c.get_mandate(id).unwrap().status, Status::Revoked);
        ctx(1, NearToken::from_yoctonear(0), 4_000);
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(id, accounts(2), near(10), "inv-2".into(), "".into());
        }));
        assert!(r.is_err());
    }

    #[test]
    #[should_panic(expected = "only the owner")]
    fn only_owner_revokes() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.revoke(id);
    }

    #[test]
    fn top_up_extends_budget() {
        let (mut c, id) = setup();
        ctx(0, near(500), 2_000);
        c.top_up(id);
        assert_eq!(c.get_mandate(id).unwrap().budget, near(1500));
        assert_eq!(c.mandates_of(accounts(1), 0, 10).len(), 1);
    }

    #[test]
    #[should_panic(expected = "separate account")]
    fn agent_cannot_be_owner() {
        ctx(0, near(1000), 1_000);
        let mut c = Contract::new();
        c.create_mandate(accounts(0), near(100), 1_000, vec![]);
    }
}
