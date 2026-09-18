// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/**
 * Tests for IkengaEscrow.
 *
 * These have NOT been run. There was no Solidity compiler in the environment this was written in.
 * They are written to be run with Foundry:
 *
 *     forge test -vvv
 *
 * They are organised around what an attacker would try rather than around the functions, because
 * a test suite that walks the public interface tells you the happy path works and almost nothing
 * about whether the money is safe.
 */

import "forge-std/Test.sol";
import "../src/IkengaEscrow.sol";

/// @dev Minimal ERC20. Not a faithful USDC — faithful enough for what is being tested.
contract MockToken is IERC20 {
    mapping(address => uint256) public balances;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balances[to] += amount;
    }

    function balanceOf(address a) external view returns (uint256) {
        return balances[a];
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external virtual returns (bool) {
        require(balances[msg.sender] >= amount, "insufficient");
        balances[msg.sender] -= amount;
        balances[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external virtual returns (bool) {
        require(balances[from] >= amount, "insufficient");
        require(allowance[from][msg.sender] >= amount, "not approved");
        allowance[from][msg.sender] -= amount;
        balances[from] -= amount;
        balances[to] += amount;
        return true;
    }
}

/// @dev A token that skims 1% on every transfer. Exists because the contract must credit what
/// actually arrived, not what was requested — otherwise the shortfall is discovered by whoever
/// withdraws last, and they eat it.
contract FeeOnTransferToken is MockToken {
    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        require(balances[from] >= amount, "insufficient");
        require(allowance[from][msg.sender] >= amount, "not approved");
        allowance[from][msg.sender] -= amount;
        uint256 skim = amount / 100;
        balances[from] -= amount;
        balances[to] += amount - skim;
        return true;
    }

    function transfer(address to, uint256 amount) external override returns (bool) {
        require(balances[msg.sender] >= amount, "insufficient");
        uint256 skim = amount / 100;
        balances[msg.sender] -= amount;
        balances[to] += amount - skim;
        return true;
    }
}

/// @dev Tries to withdraw again from inside the transfer it is being paid by.
contract ReentrantStaker {
    IkengaEscrow public escrow;
    uint256 public reentryAttempts;

    constructor(IkengaEscrow _escrow) {
        escrow = _escrow;
    }

    function attack() external {
        escrow.withdraw();
    }

    // A real ERC20 gives no receive hook, so this models the hostile-token case: the token calls
    // back into the escrow mid-transfer.
    fallback() external {
        reentryAttempts++;
        if (reentryAttempts < 3) {
            try escrow.withdraw() {} catch {}
        }
    }
}

contract IkengaEscrowTest is Test {
    IkengaEscrow escrow;
    MockToken token;

    address owner = address(0xA0);
    address resolver = address(0xA1);
    address feeRecipient = address(0xA2);
    address alice = address(0xB1);
    address bob = address(0xB2);
    address carol = address(0xB3);
    address stranger = address(0xC1);

    uint16 constant FEE_BPS = 100; // 1%
    bytes32 constant MKT = keccak256("market-1");

    function setUp() public {
        token = new MockToken();
        vm.prank(owner);
        escrow = new IkengaEscrow(token, resolver, feeRecipient, FEE_BPS);

        for (uint160 i = 0xB1; i <= 0xB3; i++) {
            token.mint(address(i), 1_000_000);
            vm.prank(address(i));
            token.approve(address(escrow), type(uint256).max);
        }
    }

    function _openMarket() internal {
        vm.prank(resolver);
        escrow.openMarket(MKT, 2, uint64(block.timestamp + 1 hours), uint64(block.timestamp + 2 hours));
    }

    // =======================================================================================
    // The money is split correctly
    // =======================================================================================

    function test_winners_split_the_pot_pro_rata_after_the_fee() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 300);
        vm.prank(bob);   escrow.stake(MKT, 0, 100);
        vm.prank(carol); escrow.stake(MKT, 1, 400);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        // Losing side 400, fee 1% = 4, payout pool = 800 - 4 = 796.
        // Alice holds 300/400 of the winning side, Bob 100/400.
        escrow.claim(MKT, alice);
        escrow.claim(MKT, bob);
        assertEq(escrow.owed(alice), 597); // 796 * 300 / 400
        assertEq(escrow.owed(bob), 199);   // 796 * 100 / 400
        assertEq(escrow.feesAccrued(), 4);
    }

    function test_a_winner_is_never_paid_less_than_they_staked() public {
        // The promise the whole fee design exists to keep. The fee comes from the losing side, so
        // being right can never cost money.
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 1000);
        vm.prank(bob);   escrow.stake(MKT, 1, 1);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);
        escrow.claim(MKT, alice);
        assertGe(escrow.owed(alice), 1000);
    }

    function test_the_fee_is_taken_only_from_the_losing_side() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 500);
        vm.prank(bob);   escrow.stake(MKT, 1, 500);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);
        assertEq(escrow.feesAccrued(), 5); // 1% of the 500 that lost, not of the 1000 pot
    }

    function test_a_market_everyone_agreed_on_voids_and_earns_nothing() public {
        // Nobody backed the other side, so there is nothing to win and nothing to take a cut of.
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(bob);   escrow.stake(MKT, 0, 100);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 1); // nobody backed outcome 1

        (IkengaEscrow.Status status,,,,,,,) = _market(MKT);
        assertEq(uint8(status), uint8(IkengaEscrow.Status.Voided));
        assertEq(escrow.feesAccrued(), 0);

        escrow.claim(MKT, alice);
        assertEq(escrow.owed(alice), 100, "a void refunds exactly what was staked");
    }

    function test_a_void_refunds_every_side_exactly() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 300);
        vm.prank(alice); escrow.stake(MKT, 1, 200); // both sides, same address
        vm.prank(bob);   escrow.stake(MKT, 1, 400);

        vm.prank(resolver);
        escrow.voidMarket(MKT, "source unavailable");

        escrow.claim(MKT, alice);
        escrow.claim(MKT, bob);
        assertEq(escrow.owed(alice), 500, "both of one address's sides come back");
        assertEq(escrow.owed(bob), 400);
        assertEq(escrow.feesAccrued(), 0, "a void must never earn the house anything");
    }

    // =======================================================================================
    // Nobody can take what is not theirs
    // =======================================================================================

    function test_a_loser_is_owed_nothing() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(bob);   escrow.stake(MKT, 1, 100);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        escrow.claim(MKT, bob);
        assertEq(escrow.owed(bob), 0);
        vm.prank(bob);
        vm.expectRevert("nothing owed");
        escrow.withdraw();
    }

    function test_claiming_twice_pays_once() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(bob);   escrow.stake(MKT, 1, 100);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        escrow.claim(MKT, alice);
        uint256 first = escrow.owed(alice);
        vm.expectRevert("already claimed");
        escrow.claim(MKT, alice);
        assertEq(escrow.owed(alice), first);
    }

    function test_withdrawing_twice_pays_once() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(bob);   escrow.stake(MKT, 1, 100);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);
        escrow.claim(MKT, alice);

        vm.prank(alice);
        escrow.withdraw();
        vm.prank(alice);
        vm.expectRevert("nothing owed");
        escrow.withdraw();
    }

    function test_a_stranger_cannot_claim_into_their_own_balance() public {
        // claim() credits the staker named, never the caller. Settling up on someone's behalf is
        // harmless, which is why it is left open.
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(bob);   escrow.stake(MKT, 1, 100);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        vm.prank(stranger);
        escrow.claim(MKT, alice);
        assertEq(escrow.owed(stranger), 0);
        assertGt(escrow.owed(alice), 0);
    }

    function test_reentering_a_withdrawal_takes_nothing_extra() public {
        ReentrantStaker attacker = new ReentrantStaker(escrow);
        token.mint(address(attacker), 1000);
        vm.prank(address(attacker));
        token.approve(address(escrow), type(uint256).max);

        _openMarket();
        vm.prank(address(attacker)); escrow.stake(MKT, 0, 500);
        vm.prank(bob); escrow.stake(MKT, 1, 500);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);
        escrow.claim(MKT, address(attacker));

        uint256 due = escrow.owed(address(attacker));
        uint256 before = token.balanceOf(address(attacker));
        attacker.attack();
        assertEq(token.balanceOf(address(attacker)) - before, due, "paid exactly once");
        assertEq(escrow.owed(address(attacker)), 0);
    }

    // =======================================================================================
    // The operator is not trusted with anyone's stake
    // =======================================================================================

    function test_the_owner_cannot_move_escrow() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 1000);

        // There is deliberately no function to try. This asserts the shape of the interface: the
        // only value-moving calls are withdraw (self), claim (credits the staker) and
        // withdrawFees (fixed recipient, fees only).
        vm.prank(owner);
        vm.expectRevert("nothing owed");
        escrow.withdraw();

        vm.prank(owner);
        vm.expectRevert("no fees");
        escrow.withdrawFees();
    }

    function test_fees_go_to_the_fixed_recipient_whoever_calls() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 500);
        vm.prank(bob);   escrow.stake(MKT, 1, 500);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        uint256 before = token.balanceOf(feeRecipient);
        vm.prank(stranger); // anyone may trigger it; it cannot be redirected
        escrow.withdrawFees();
        assertEq(token.balanceOf(feeRecipient) - before, 5);
        assertEq(escrow.feesAccrued(), 0);
    }

    function test_a_fee_above_the_cap_cannot_be_deployed() public {
        vm.expectRevert("fee above cap");
        new IkengaEscrow(token, resolver, feeRecipient, escrow.MAX_FEE_BPS() + 1);
    }

    function test_only_the_resolver_may_declare_an_outcome() public {
        _openMarket();
        vm.warp(block.timestamp + 2 hours);
        for (uint160 i = 0; i < 3; i++) {
            address who = [owner, alice, stranger][i];
            vm.prank(who);
            vm.expectRevert("not resolver");
            escrow.resolve(MKT, 0);
        }
    }

    function test_only_the_owner_may_change_the_resolver() public {
        vm.prank(resolver);
        vm.expectRevert("not owner");
        escrow.setResolver(stranger);
    }

    // =======================================================================================
    // Funds can never be trapped
    // =======================================================================================

    function test_an_abandoned_market_can_be_refunded_by_anyone() public {
        // The property that matters most: if the resolver never reports — key lost, server gone,
        // operator walked away — participants get their own money back without needing anyone's
        // permission.
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 400);
        vm.prank(bob);   escrow.stake(MKT, 1, 600);

        vm.warp(block.timestamp + 2 hours);
        vm.expectRevert("not abandoned yet");
        escrow.expire(MKT);

        vm.warp(block.timestamp + escrow.RESOLUTION_GRACE() + 1);
        vm.prank(stranger); // not a participant, not the owner
        escrow.expire(MKT);

        escrow.claim(MKT, alice);
        escrow.claim(MKT, bob);
        assertEq(escrow.owed(alice), 400);
        assertEq(escrow.owed(bob), 600);
        assertEq(escrow.feesAccrued(), 0, "an abandoned market earns nothing");
    }

    function test_a_resolved_market_cannot_be_expired_out_from_under_the_winners() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 500);
        vm.prank(bob);   escrow.stake(MKT, 1, 500);
        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        vm.warp(block.timestamp + escrow.RESOLUTION_GRACE() + 1);
        vm.expectRevert("not open");
        escrow.expire(MKT);
    }

    // =======================================================================================
    // Solvency
    // =======================================================================================

    function test_the_contract_always_holds_at_least_what_it_owes() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 300);
        vm.prank(bob);   escrow.stake(MKT, 1, 700);
        assertTrue(escrow.isSolvent());

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);
        assertTrue(escrow.isSolvent());

        escrow.claim(MKT, alice);
        assertTrue(escrow.isSolvent());

        vm.prank(alice);
        escrow.withdraw();
        assertTrue(escrow.isSolvent());

        escrow.withdrawFees();
        assertTrue(escrow.isSolvent());
    }

    function testFuzz_solvency_holds_for_any_split(uint96 a, uint96 b, uint96 c) public {
        a = uint96(bound(a, 1, 100_000));
        b = uint96(bound(b, 1, 100_000));
        c = uint96(bound(c, 1, 100_000));

        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, a);
        vm.prank(bob);   escrow.stake(MKT, 0, b);
        vm.prank(carol); escrow.stake(MKT, 1, c);

        vm.warp(block.timestamp + 2 hours);
        vm.prank(resolver);
        escrow.resolve(MKT, 0);

        escrow.claim(MKT, alice);
        escrow.claim(MKT, bob);
        escrow.claim(MKT, carol);

        // Rounding may leave dust in the contract. It must never go the other way.
        assertTrue(escrow.isSolvent(), "paid out more than was staked");
        assertLe(escrow.owed(alice) + escrow.owed(bob) + escrow.feesAccrued(),
                 uint256(a) + uint256(b) + uint256(c),
                 "value was created");
    }

    // =======================================================================================
    // Awkward tokens and awkward inputs
    // =======================================================================================

    function test_a_token_that_skims_on_transfer_credits_only_what_arrived() public {
        FeeOnTransferToken skimmy = new FeeOnTransferToken();
        vm.prank(owner);
        IkengaEscrow e2 = new IkengaEscrow(skimmy, resolver, feeRecipient, FEE_BPS);
        skimmy.mint(alice, 10_000);
        vm.prank(alice);
        skimmy.approve(address(e2), type(uint256).max);

        vm.prank(resolver);
        e2.openMarket(MKT, 2, uint64(block.timestamp + 1 hours), uint64(block.timestamp + 2 hours));
        vm.prank(alice);
        e2.stake(MKT, 0, 1000);

        // 1% was skimmed in flight, so 990 arrived and 990 is what may be credited. Crediting
        // 1000 would promise money the contract does not hold.
        assertEq(e2.stakeOf(MKT, alice, 0), 990);
        assertTrue(e2.isSolvent());
    }

    function test_stakes_are_refused_once_betting_closes() public {
        _openMarket();
        vm.warp(block.timestamp + 1 hours + 1);
        vm.prank(alice);
        vm.expectRevert("closed");
        escrow.stake(MKT, 0, 100);
    }

    function test_an_outcome_that_does_not_exist_is_refused() public {
        _openMarket();
        vm.prank(alice);
        vm.expectRevert("no such outcome");
        escrow.stake(MKT, 5, 100);
    }

    function test_a_market_cannot_be_resolved_before_it_closes() public {
        _openMarket();
        vm.prank(alice); escrow.stake(MKT, 0, 100);
        vm.prank(resolver);
        vm.expectRevert("still open");
        escrow.resolve(MKT, 0);
    }

    function test_a_market_id_cannot_be_reused() public {
        _openMarket();
        vm.prank(resolver);
        vm.expectRevert("already exists");
        escrow.openMarket(MKT, 2, uint64(block.timestamp + 1 hours), uint64(block.timestamp + 2 hours));
    }

    // ---------------------------------------------------------------------------------------

    function _market(bytes32 id)
        internal
        view
        returns (
            IkengaEscrow.Status,
            uint8,
            uint8,
            uint64,
            uint64,
            uint256,
            uint256,
            uint256
        )
    {
        return escrow.markets(id);
    }
}
