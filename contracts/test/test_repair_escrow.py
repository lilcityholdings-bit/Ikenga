#!/usr/bin/env python3
"""RepairEscrow tests, run on an in-memory EVM (no chain, no Foundry needed):

    pip install py-solc-x "eth-tester[py-evm]" web3
    python3 contracts/test/test_repair_escrow.py

Compiles src/RepairEscrow.sol with solc 0.8.26 (downloaded once by py-solc-x), deploys it with a
6-decimal mock USDC, and drives every path, including the attacks the design has to stop.
After every test the contract must be solvent: its token balance covers everything it owes.
"""
import os
import sys
import unittest

import solcx
from eth_tester.exceptions import TransactionFailed
from web3 import EthereumTesterProvider, Web3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOLC = "0.8.26"
DAY = 86400


def compile_all():
    if SOLC not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC)
    out = solcx.compile_files([os.path.join(ROOT, "src", "RepairEscrow.sol"), os.path.join(HERE, "MockUSDC.sol")],
                              output_values=["abi", "bin"], solc_version=SOLC, optimize=True)
    pick = lambda name: next(v for k, v in out.items() if k.endswith(":" + name))
    return pick("RepairEscrow"), pick("MockUSDC")


ESCROW, USDC = compile_all()


def shop_formula(amount, award, take_bps, warranty_bps, self_repair=False):
    """services/auto-body-shop/shop.py _settle, verbatim arithmetic."""
    take = 0 if self_repair else award * take_bps // 10000
    warranty = 0 if self_repair else (award - take) * warranty_bps // 10000
    return {"take": take, "warranty": warranty, "paid_now": award - take - warranty, "refund": amount - award}


class Escrow(unittest.TestCase):
    def setUp(self):
        self.w3 = Web3(EthereumTesterProvider())
        self.tester = self.w3.provider.ethereum_tester
        a = self.w3.eth.accounts
        self.deployer, self.referee, self.fee_to, self.owner, self.repairer, self.thief, self.new_referee = a[:7]
        self.usdc = self.deploy(USDC)
        self.escrow = self.deploy(ESCROW, self.usdc.address, self.referee, self.fee_to, 1000)
        for who in (self.owner, self.repairer, self.thief):
            self.tx(self.usdc.functions.mint(who, 10_000_000_000), self.deployer)
            self.tx(self.usdc.functions.approve(self.escrow.address, 2**255), who)

    def tearDown(self):
        self.assertTrue(self.escrow.functions.isSolvent().call())
        liabilities = self.escrow.functions.totalLiabilities().call()
        self.assertEqual(self.usdc.functions.balanceOf(self.escrow.address).call(), liabilities)

    # ---- helpers --------------------------------------------------------------------------

    def deploy(self, art, *args):
        c = self.w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
        rcpt = self.w3.eth.wait_for_transaction_receipt(c.constructor(*args).transact({"from": self.w3.eth.accounts[0]}))
        return self.w3.eth.contract(address=rcpt.contractAddress, abi=art["abi"])

    def tx(self, fn, sender):
        return self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender, "gas": 3_000_000}))

    def reverts(self, fn, sender, reason):
        # Simulate first to get the revert reason, then send it anyway and check it failed on-chain.
        with self.assertRaises(Exception) as cm:
            fn.call({"from": sender})
        self.assertIn(reason, str(cm.exception))
        try:
            rcpt = self.w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender, "gas": 3_000_000}))
            self.assertEqual(rcpt.status, 0)
        except Exception:
            pass  # rejected before mining: also a revert

    def now(self):
        return self.w3.eth.get_block("latest").timestamp

    def travel(self, seconds):
        self.tester.time_travel(self.now() + seconds)
        self.tester.mine_blocks(1)

    def open_bounty(self, amount=1_000_000, bid=b"b1".ljust(32, b"\0"), funder=None):
        funder = funder or self.owner
        t = self.now()
        self.tx(self.escrow.functions.fund(bid, amount, t + DAY, t + 3 * DAY), funder)
        key = self.escrow.functions.bountyKey(funder, bid).call()
        self.tx(self.escrow.functions.commit(key, b"C" * 32), self.referee)
        return key

    def bal(self, who):
        return self.usdc.functions.balanceOf(who).call()

    def withdraw_all(self, who):
        owed = self.escrow.functions.owed(who).call()
        if owed:
            self.tx(self.escrow.functions.withdraw(), who)
        return owed

    # ---- lifecycle ----------------------------------------------------------------------------

    def test_full_lifecycle_matches_shop_formula(self):
        key = self.open_bounty(1_000_000)
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)
        self.travel(DAY + 1)
        award, until = 750_000, self.now() + 7 * DAY
        self.tx(self.escrow.functions.settle(key, self.repairer, b"H" * 32, award, 1000, 3000, until), self.referee)
        want = shop_formula(1_000_000, award, 1000, 3000)
        self.assertEqual(self.escrow.functions.owed(self.repairer).call(), want["paid_now"])
        self.assertEqual(self.escrow.functions.owed(self.owner).call(), want["refund"])
        self.assertEqual(self.escrow.functions.fees().call(), want["take"])
        self.assertEqual(self.escrow.functions.bounties(key).call()[8], want["warranty"])
        before = self.bal(self.repairer)
        self.withdraw_all(self.repairer)
        self.assertEqual(self.bal(self.repairer), before + want["paid_now"])
        self.reverts(self.escrow.functions.withdraw(), self.repairer, "nothing owed")
        self.tx(self.escrow.functions.withdrawFees(), self.thief)            # anyone can trigger...
        self.assertEqual(self.bal(self.fee_to), want["take"])                # ...but it only goes here
        self.withdraw_all(self.owner)
        self.tx(self.escrow.functions.resolveWarranty(key, False), self.referee)
        self.withdraw_all(self.repairer)

    def test_no_winner_refunds_everything(self):
        key = self.open_bounty(500_000)
        self.travel(DAY + 1)
        self.tx(self.escrow.functions.settle(key, self.repairer, b"\0" * 32, 0, 1000, 3000, 0), self.referee)
        self.assertEqual(self.escrow.functions.owed(self.owner).call(), 500_000)
        self.withdraw_all(self.owner)

    def test_self_repair_pays_no_take_and_holds_no_warranty(self):
        key = self.open_bounty(1_000_000)
        self.tx(self.escrow.functions.submit(key, b"S" * 32), self.owner)
        self.travel(DAY + 1)
        self.tx(self.escrow.functions.settle(key, self.owner, b"S" * 32, 1_000_000, 1000, 3000, 0), self.referee)
        self.assertEqual((self.escrow.functions.owed(self.owner).call(), self.escrow.functions.fees().call()), (1_000_000, 0))
        self.withdraw_all(self.owner)

    # ---- the attacks ---------------------------------------------------------------------------

    def test_front_running_a_submission_hash_gains_nothing(self):
        key = self.open_bounty()
        # The thief sees the repairer's pending submit and registers the same hash first.
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.thief)
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)   # not blocked by the copy
        self.travel(DAY + 1)
        self.tx(self.escrow.functions.settle(key, self.repairer, b"H" * 32, 100_000, 1000, 0, 0), self.referee)
        self.assertEqual(self.escrow.functions.owed(self.thief).call(), 0)
        self.withdraw_all(self.repairer)
        self.withdraw_all(self.owner)

    def test_referee_cannot_pay_an_address_that_never_submitted(self):
        key = self.open_bounty()
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)
        self.travel(DAY + 1)
        self.reverts(self.escrow.functions.settle(key, self.thief, b"H" * 32, 100_000, 1000, 0, 0), self.referee,
                     "did not register")
        self.tx(self.escrow.functions.settle(key, self.repairer, b"H" * 32, 100_000, 1000, 0, 0), self.referee)
        self.withdraw_all(self.repairer)
        self.withdraw_all(self.owner)

    def test_owner_swapping_referee_cannot_touch_funded_bounties(self):
        key = self.open_bounty()
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)
        self.tx(self.escrow.functions.setReferee(self.new_referee), self.deployer)
        self.travel(DAY + 1)
        self.reverts(self.escrow.functions.settle(key, self.repairer, b"H" * 32, 1, 0, 0, 0), self.new_referee,
                     "not this bounty's referee")
        self.tx(self.escrow.functions.settle(key, self.repairer, b"H" * 32, 1, 0, 0, 0), self.referee)
        self.withdraw_all(self.repairer)
        self.withdraw_all(self.owner)

    def test_bounty_ids_cannot_be_squatted(self):
        bid = b"shared-id".ljust(32, b"\0")
        self.open_bounty(1, bid=bid, funder=self.thief)
        key = self.open_bounty(1_000, bid=bid, funder=self.owner)   # still works: keyed by funder
        self.assertNotEqual(key, self.escrow.functions.bountyKey(self.thief, bid).call())
        self.travel(3 * DAY + 1)
        for f in (self.thief, self.owner):
            self.tx(self.escrow.functions.expire(self.escrow.functions.bountyKey(f, bid).call()), self.repairer)
            self.withdraw_all(f)

    def test_access_control_and_deadlines(self):
        key = self.open_bounty(1_000_000)
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)
        settle = lambda **kw: self.escrow.functions.settle(key, self.repairer, b"H" * 32, kw.get("award", 1),
                                                           kw.get("take", 0), 0, 0)
        self.reverts(settle(), self.thief, "not this bounty's referee")
        self.reverts(settle(), self.referee, "outside settlement window")          # before submissions close
        self.reverts(self.escrow.functions.commit(key, b"D" * 32), self.referee, "cannot commit")  # commit once
        self.reverts(self.escrow.functions.setReferee(self.thief), self.thief, "not owner")
        self.travel(DAY + 1)
        self.reverts(self.escrow.functions.submit(key, b"L" * 32), self.repairer, "submissions closed")
        self.reverts(settle(award=1_000_001), self.referee, "award exceeds escrow")
        self.reverts(settle(take=1001), self.referee, "bad bps")                      # take cap fixed at deploy
        self.reverts(self.escrow.functions.expire(key), self.thief, "not expired")
        self.travel(2 * DAY + 1)
        self.reverts(settle(), self.referee, "outside settlement window")              # referee too late
        self.tx(self.escrow.functions.expire(key), self.thief)                          # anyone can refund
        self.reverts(self.escrow.functions.expire(key), self.thief, "not expired")
        self.assertEqual(self.withdraw_all(self.owner), 1_000_000)

    def test_warranty_paths(self):
        # Ruled harmed by the referee: back to the funder.
        key = self.open_bounty(1_000_000)
        self.tx(self.escrow.functions.submit(key, b"H" * 32), self.repairer)
        self.travel(DAY + 1)
        self.tx(self.escrow.functions.settle(key, self.repairer, b"H" * 32, 1_000_000, 1000, 3000, self.now() + 7 * DAY),
                self.referee)
        w = shop_formula(1_000_000, 1_000_000, 1000, 3000)["warranty"]
        self.reverts(self.escrow.functions.claimWarranty(key), self.thief, "grace period not over")
        self.tx(self.escrow.functions.resolveWarranty(key, True), self.referee)
        self.assertEqual(self.escrow.functions.owed(self.owner).call(), w)
        self.reverts(self.escrow.functions.resolveWarranty(key, False), self.referee, "no warranty held")
        # Referee silent: after the grace period anyone releases it to the repairer.
        key2 = self.open_bounty(1_000_000, bid=b"b2".ljust(32, b"\0"))
        self.tx(self.escrow.functions.submit(key2, b"H" * 32), self.repairer)
        self.travel(DAY + 1)
        self.tx(self.escrow.functions.settle(key2, self.repairer, b"H" * 32, 1_000_000, 1000, 3000, self.now() + DAY),
                self.referee)
        self.travel(8 * DAY + 1)
        self.reverts(self.escrow.functions.resolveWarranty(key2, True), self.referee, "ruling window over")
        self.tx(self.escrow.functions.claimWarranty(key2), self.thief)
        self.withdraw_all(self.owner)
        self.withdraw_all(self.repairer)
        self.tx(self.escrow.functions.withdrawFees(), self.deployer)

    def test_constructor_caps_the_take(self):
        with self.assertRaises(Exception):
            self.deploy(ESCROW, self.usdc.address, self.referee, self.fee_to, 2001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
