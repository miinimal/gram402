//! near-mandate: spending mandates for AI agents on NEAR.
//!
//! An owner funds a mandate with NEAR and names the agent account, a cap per payment, an expiry and the merchants
//! the agent may pay. The agent calls `pay` with an invoice id from an HTTP 402 offer; the contract checks every
//! limit, sends the amount straight to the merchant and logs a `paid` event. The merchant's server verifies the
//! payment from the transaction itself (event emitted by this contract, merchant, amount, invoice, no failed
//! receipt), so the contract keeps no per-payment state and nobody can squat invoice ids.
//!
//! Storage: the owner's deposit pays for the mandate's own storage; payments add no state. A failed transfer to the
//! merchant puts the amount back into the mandate; a failed refund on revoke re-activates the mandate.
//! The agent can hold a function-call access key restricted to `pay`: even a compromised agent cannot move the
//! owner's funds anywhere except to allowed merchants, within the limits.
//! Author: Aleksandr Shcherbakov (@miinimal). License: MIT.
use near_sdk::store::LookupMap;
use near_sdk::{env, log, near, require, AccountId, Gas, NearToken, PanicOnDefault, Promise};

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
    /// Spendable budget (the deposit minus the storage this mandate occupies).
    pub budget: NearToken,
    pub spent: NearToken,
    pub per_payment_cap: NearToken,
    pub expires_at_ms: u64,
    pub merchants: Vec<AccountId>,
    pub status: Status,
    pub payments: u32,
    /// Storage stake held for this mandate; returned to the owner on revoke.
    pub storage: NearToken,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Contract {
    next_id: MandateId,
    mandates: LookupMap<MandateId, Mandate>,
}

const MAX_MERCHANTS: usize = 16;
const MAX_INVOICE_LEN: usize = 64;
const CALLBACK_GAS: Gas = Gas::from_tgas(5);

#[near]
impl Contract {
    #[init]
    pub fn new() -> Self {
        Self { next_id: 1, mandates: LookupMap::new(b"m") }
    }

    /// Owner creates and funds a mandate. The attached deposit pays the mandate's storage; the rest is the budget.
    /// `merchants` empty means any merchant (not recommended).
    #[payable]
    pub fn create_mandate(&mut self, agent: AccountId, per_payment_cap: NearToken, valid_for_ms: u64,
                          merchants: Vec<AccountId>) -> MandateId {
        let deposit = env::attached_deposit();
        require!(valid_for_ms > 0, "validity must be > 0");
        require!(merchants.len() <= MAX_MERCHANTS, "too many merchants");
        let owner = env::predecessor_account_id();
        require!(agent != owner, "the agent must be a separate account");
        let id = self.next_id;
        self.next_id += 1;
        let before = env::storage_usage();
        self.mandates.insert(id, Mandate {
            owner: owner.clone(), agent: agent.clone(), budget: NearToken::from_yoctonear(0),
            spent: NearToken::from_yoctonear(0), per_payment_cap, merchants, status: Status::Active, payments: 0,
            expires_at_ms: env::block_timestamp_ms().saturating_add(valid_for_ms), storage: NearToken::from_yoctonear(0),
        });
        self.mandates.flush();
        let storage = env::storage_byte_cost().saturating_mul((env::storage_usage() - before) as u128);
        require!(deposit > storage, "deposit must cover the mandate's storage");
        let budget = deposit.saturating_sub(storage);
        require!(!per_payment_cap.is_zero() && per_payment_cap <= budget, "cap must be > 0 and <= budget");
        let m = self.mandates.get_mut(&id).unwrap();
        m.budget = budget;
        m.storage = storage;
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"2.0.0\",\"event\":\"mandate_created\",\"data\":[{{\"mandate_id\":{},\"owner\":\"{}\",\"agent\":\"{}\",\"budget\":\"{}\"}}]}}",
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

    /// Agent pays a merchant for one invoice. Every limit is checked here; the money goes straight to the merchant
    /// and the `paid` event in this transaction is the proof the merchant checks.
    pub fn pay(&mut self, mandate_id: MandateId, merchant: AccountId, amount: NearToken, invoice: String) -> Promise {
        require!(!invoice.is_empty() && invoice.len() <= MAX_INVOICE_LEN, "bad invoice id");
        require!(invoice.chars().all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-'), "bad invoice id");
        require!(!amount.is_zero(), "amount must be > 0");
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
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"2.0.0\",\"event\":\"paid\",\"data\":[{{\"mandate_id\":{},\"merchant\":\"{}\",\"amount\":\"{}\",\"invoice\":\"{}\"}}]}}",
             mandate_id, merchant, amount.as_yoctonear(), invoice);
        Promise::new(merchant).transfer(amount)
            .then(Self::ext(env::current_account_id()).with_static_gas(CALLBACK_GAS).on_paid(mandate_id, amount))
    }

    /// Transfer to the merchant failed (e.g. the account does not exist): put the amount back into the mandate.
    #[private]
    pub fn on_paid(&mut self, mandate_id: MandateId, amount: NearToken) -> bool {
        if env::promise_result_checked(0, 0).is_ok() {
            return true;
        }
        if let Some(m) = self.mandates.get_mut(&mandate_id) {
            m.spent = m.spent.saturating_sub(amount);
            m.payments = m.payments.saturating_sub(1);
        }
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"2.0.0\",\"event\":\"pay_failed\",\"data\":[{{\"mandate_id\":{},\"amount\":\"{}\"}}]}}",
             mandate_id, amount.as_yoctonear());
        false
    }

    /// Owner revokes the mandate; the unspent budget and the storage stake go back to the owner.
    pub fn revoke(&mut self, mandate_id: MandateId) -> Promise {
        let m = self.mandates.get_mut(&mandate_id).unwrap_or_else(|| env::panic_str("unknown mandate"));
        require!(env::predecessor_account_id() == m.owner, "only the owner");
        require!(m.status == Status::Active, "already revoked");
        m.status = Status::Revoked;
        let refund = m.budget.saturating_sub(m.spent);
        let owner = m.owner.clone();
        log!("EVENT_JSON:{{\"standard\":\"near-mandate\",\"version\":\"2.0.0\",\"event\":\"revoked\",\"data\":[{{\"mandate_id\":{},\"refund\":\"{}\"}}]}}",
             mandate_id, refund.as_yoctonear());
        // The storage stake stays locked while the record exists; it is part of the record's cost, so the refund is
        // the unspent budget. (Records are kept so merchants and owners can audit the mandate later.)
        Promise::new(owner).transfer(refund)
            .then(Self::ext(env::current_account_id()).with_static_gas(CALLBACK_GAS).on_refund(mandate_id))
    }

    /// Refund failed: re-activate the mandate so the owner can revoke again; funds never get stuck.
    #[private]
    pub fn on_refund(&mut self, mandate_id: MandateId) -> bool {
        if env::promise_result_checked(0, 0).is_ok() {
            return true;
        }
        if let Some(m) = self.mandates.get_mut(&mandate_id) {
            m.status = Status::Active;
        }
        false
    }

    pub fn get_mandate(&self, mandate_id: MandateId) -> Option<Mandate> {
        self.mandates.get(&mandate_id).cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::{testing_env, PromiseResult, RuntimeFeesConfig};

    fn near(n: u128) -> NearToken { NearToken::from_millinear(n) }

    fn ctx(who: usize, deposit: NearToken, ts_ms: u64) {
        testing_env!(VMContextBuilder::new().predecessor_account_id(accounts(who)).attached_deposit(deposit)
            .block_timestamp(ts_ms * 1_000_000).build());
    }

    fn ctx_result(result: PromiseResult) {
        testing_env!(VMContextBuilder::new().predecessor_account_id(accounts(4)).current_account_id(accounts(4)).build(),
                     near_sdk::test_vm_config(), RuntimeFeesConfig::test(), Default::default(), vec![result]);
    }

    // accounts(0) owner, accounts(1) agent, accounts(2) merchant, accounts(3) other merchant
    fn setup() -> (Contract, MandateId) {
        ctx(0, near(1000), 1_000);
        let mut c = Contract::new();
        let id = c.create_mandate(accounts(1), near(300), 60_000, vec![accounts(2)]);
        (c, id)
    }

    #[test]
    fn storage_is_paid_by_the_deposit() {
        let (c, id) = setup();
        let m = c.get_mandate(id).unwrap();
        assert!(!m.storage.is_zero());
        assert_eq!(m.budget.saturating_add(m.storage), near(1000));
    }

    #[test]
    #[should_panic(expected = "deposit must cover")]
    fn tiny_deposit_rejected() {
        ctx(0, NearToken::from_yoctonear(1), 1_000);
        let mut c = Contract::new();
        c.create_mandate(accounts(1), NearToken::from_yoctonear(1), 60_000, vec![]);
    }

    #[test]
    fn agent_pays_within_limits() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(300), "inv-1".into());
        let _ = c.pay(id, accounts(2), near(300), "inv-2".into());
        let m = c.get_mandate(id).unwrap();
        assert_eq!((m.spent, m.payments), (near(600), 2));
    }

    #[test]
    #[should_panic(expected = "per-payment cap")]
    fn cap_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(301), "inv".into());
    }

    #[test]
    #[should_panic(expected = "budget exhausted")]
    fn budget_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        for i in 0..4 {
            let _ = c.pay(id, accounts(2), near(300), format!("inv-{i}"));
        }
    }

    #[test]
    #[should_panic(expected = "merchant not allowed")]
    fn merchant_allowlist_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(3), near(100), "inv".into());
    }

    #[test]
    #[should_panic(expected = "only the mandate's agent")]
    fn only_agent_pays() {
        let (mut c, id) = setup();
        ctx(3, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(100), "inv".into());
    }

    #[test]
    #[should_panic(expected = "mandate expired")]
    fn expiry_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 61_001);
        let _ = c.pay(id, accounts(2), near(100), "inv".into());
    }

    #[test]
    #[should_panic(expected = "bad invoice id")]
    fn invoice_charset_enforced() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(100), "inv\",\"x".into());
    }

    #[test]
    fn failed_transfer_returns_the_amount() {
        let (mut c, id) = setup();
        ctx(1, NearToken::from_yoctonear(0), 2_000);
        let _ = c.pay(id, accounts(2), near(200), "inv".into());
        ctx_result(PromiseResult::Failed);
        assert!(!c.on_paid(id, near(200)));
        let m = c.get_mandate(id).unwrap();
        assert_eq!((m.spent, m.payments), (NearToken::from_yoctonear(0), 0));
    }

    #[test]
    fn revoke_stops_payments_and_failed_refund_reactivates() {
        let (mut c, id) = setup();
        ctx(0, NearToken::from_yoctonear(0), 3_000);
        let _ = c.revoke(id);
        assert_eq!(c.get_mandate(id).unwrap().status, Status::Revoked);
        ctx(1, NearToken::from_yoctonear(0), 4_000);
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(id, accounts(2), near(10), "inv-2".into());
        }));
        assert!(r.is_err());
        ctx_result(PromiseResult::Failed);
        assert!(!c.on_refund(id));
        assert_eq!(c.get_mandate(id).unwrap().status, Status::Active);
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
        let before = c.get_mandate(id).unwrap().budget;
        ctx(0, near(500), 2_000);
        c.top_up(id);
        assert_eq!(c.get_mandate(id).unwrap().budget, before.saturating_add(near(500)));
    }

    #[test]
    #[should_panic(expected = "separate account")]
    fn agent_cannot_be_owner() {
        ctx(0, near(1000), 1_000);
        let mut c = Contract::new();
        c.create_mandate(accounts(0), near(100), 1_000, vec![]);
    }
}
