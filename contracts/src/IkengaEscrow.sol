// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/**
 * @title IkengaEscrow
 * @notice Holds stakes for prediction markets, pays winners, and routes the operator's fee.
 *
 * ============================================================================================
 *                                    READ THIS FIRST
 * ============================================================================================
 *
 * This contract has NOT been compiled, deployed, or tested. The environment it was written in
 * had no Solidity compiler and no chain access. Every line below is written to established safe
 * patterns and reasoned about carefully, and none of it has been executed even once.
 *
 * Do not put real money behind it until it has been compiled, run against the test suite beside
 * it, and reviewed by someone whose job that is. "Carefully written" and "verified" are different
 * things, and the difference is where money is lost.
 *
 * ============================================================================================
 *                                    THE THREAT MODEL
 * ============================================================================================
 *
 * The design question is not "how do we pay winners" — that is arithmetic. It is "what is the
 * worst thing each party can do", and the answer has to be boring for every one of them.
 *
 * THE OPERATOR (you). Can declare outcomes and collect fees. Cannot touch a single unit of
 * anybody's stake: fees accrue in a separate balance and `withdrawFees` can only ever move that
 * balance. There is no owner function that transfers escrow, no upgrade path, no proxy, no
 * `delegatecall`, and no way to add one later. Even if your key is stolen, the thief can set
 * outcomes and take future fees — they cannot drain the pot.
 *
 * THE RESOLVER (the engine's key). Can declare outcomes and nothing else. Deliberately separate
 * from the operator so the hot key that runs on a server is not the key that can move money.
 *
 * A PARTICIPANT. Can stake and withdraw what they are owed. Cannot withdraw twice — the balance
 * is zeroed before the transfer, which is also what makes reentrancy pointless. Cannot withdraw
 * anyone else's, because payouts are computed per-address from that address's own stake.
 *
 * A STRANGER. Can call anything public. Every state-changing function either checks the caller
 * or is safe to call by anyone (`claim`, `refund`, `expire`).
 *
 * EVERYONE, TOGETHER. Cannot get funds stuck. This is the property most escrow contracts miss:
 * if the resolver never reports, `expire` becomes callable by anyone after a deadline and every
 * participant takes their own stake back. The operator disappearing is inconvenient, not fatal.
 *
 * ============================================================================================
 *                              WHY PULL AND NOT PUSH
 * ============================================================================================
 *
 * The contract never pays anyone. It records what they are owed and they come and get it.
 *
 * Paying out in a loop is how escrow contracts die. One participant with a contract address that
 * reverts on receipt, or that burns all the gas, and the loop fails — which means *nobody* gets
 * paid, forever, because the transaction can never complete. Paying in a loop also means the gas
 * cost grows with the number of participants until settlement is simply too expensive to run.
 *
 * With pull payments, one participant's broken receiver is one participant's problem. Everyone
 * else is unaffected, settlement is constant-gas, and there is no external call inside any loop.
 */

/// @dev The subset of ERC20 this needs. Deliberately minimal.
interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract IkengaEscrow {
    // ---------------------------------------------------------------------------------------
    // Immutables. Set once at deploy, unchangeable afterwards — which is the point.
    // ---------------------------------------------------------------------------------------

    /// @notice The settlement token (USDC). Immutable so nobody can point this at a token they
    /// control and mint their way to a payout.
    IERC20 public immutable token;

    /// @notice Where the operator's fee goes. Immutable: a compromised owner key cannot redirect
    /// the revenue stream to itself, only collect to the address chosen at deploy.
    address public immutable feeRecipient;

    /// @notice Fee in basis points, taken from the losing side only. Immutable and capped.
    ///
    /// A mutable fee is a rug pull waiting to happen: whoever holds the key raises it to 100% and
    /// the next settlement pays them the whole pot. Fixed at deploy and capped at MAX_FEE_BPS, so
    /// the worst case is knowable by anyone reading the deployed bytecode.
    uint16 public immutable feeBps;

    /// @notice Hard ceiling on the fee, enforced in the constructor. 5%.
    uint16 public constant MAX_FEE_BPS = 500;

    /// @notice How long after a market's resolution deadline anyone may force a refund.
    ///
    /// The escape hatch. If the resolver never reports — key lost, server gone, operator walked
    /// away — participants are not waiting forever for permission to have their own money back.
    uint256 public constant RESOLUTION_GRACE = 7 days;

    // ---------------------------------------------------------------------------------------
    // Roles
    // ---------------------------------------------------------------------------------------

    /// @notice May declare outcomes. Nothing else. Separate from the owner on purpose: this key
    /// lives on a running server, and a key that lives on a server should never be able to move
    /// money.
    address public resolver;

    /// @notice May change the resolver and withdraw accrued fees. Cannot touch escrow, ever.
    address public owner;

    // ---------------------------------------------------------------------------------------
    // State
    // ---------------------------------------------------------------------------------------

    enum Status {
        None,     // never created
        Open,     // accepting stakes
        Resolved, // an outcome is final; winners may claim
        Voided    // refunded; everyone may take their own stake back
    }

    struct Market {
        Status status;
        uint8 outcomeCount;
        uint8 winningOutcome;
        uint64 closesAt;      // no stakes after this
        uint64 resolveBy;     // after this + RESOLUTION_GRACE, anyone may force a refund
        uint256 totalPool;    // everything staked
        uint256 winningPool;  // staked on the winning outcome, set at resolution
        uint256 payoutPool;   // what winners share: totalPool minus the fee. Set at resolution.
    }

    mapping(bytes32 => Market) public markets;

    /// @dev market => outcome => total staked on it.
    mapping(bytes32 => mapping(uint8 => uint256)) public outcomePool;

    /// @dev market => staker => outcome => amount. Per-outcome so one address can hold both
    /// sides, and so a claim can be computed from that address's own record alone.
    mapping(bytes32 => mapping(address => mapping(uint8 => uint256))) public stakeOf;

    /// @dev market => staker => already taken. Guards against a second claim without needing to
    /// zero the stake, which keeps the record readable after settlement.
    mapping(bytes32 => mapping(address => bool)) public claimed;

    /// @notice What each address may withdraw. The only balance `withdraw` can move.
    mapping(address => uint256) public owed;

    /// @notice Fees accrued and not yet collected. Tracked separately from `owed` so no accounting
    /// mistake can let a fee withdrawal reach into participant money.
    uint256 public feesAccrued;

    /// @notice Everything currently owed to participants. Used by the solvency invariant below.
    uint256 public totalOwed;

    /// @notice Everything still locked in open markets.
    uint256 public totalEscrowed;

    // ---------------------------------------------------------------------------------------
    // Reentrancy
    // ---------------------------------------------------------------------------------------

    uint256 private _lock = 1;

    /// @dev Belt and braces. Every function that makes an external call already zeroes its state
    /// first, so reentering gains nothing — this exists so that remains true after somebody edits
    /// the file six months from now without reading this comment.
    modifier nonReentrant() {
        require(_lock == 1, "reentrant");
        _lock = 2;
        _;
        _lock = 1;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyResolver() {
        require(msg.sender == resolver, "not resolver");
        _;
    }

    // ---------------------------------------------------------------------------------------
    // Events. Everything that moves value emits one, so the off-chain ledger can be reconciled
    // against the chain rather than trusted.
    // ---------------------------------------------------------------------------------------

    event MarketOpened(bytes32 indexed marketId, uint8 outcomeCount, uint64 closesAt, uint64 resolveBy);
    event Staked(bytes32 indexed marketId, address indexed staker, uint8 outcome, uint256 amount);
    event Resolved(bytes32 indexed marketId, uint8 winningOutcome, uint256 payoutPool, uint256 fee);
    event Voided(bytes32 indexed marketId, string reason);
    event Claimed(bytes32 indexed marketId, address indexed staker, uint256 amount);
    event Withdrawn(address indexed to, uint256 amount);
    event FeesWithdrawn(address indexed to, uint256 amount);
    event ResolverChanged(address indexed from, address indexed to);
    event OwnerChanged(address indexed from, address indexed to);

    // ---------------------------------------------------------------------------------------

    constructor(IERC20 _token, address _resolver, address _feeRecipient, uint16 _feeBps) {
        require(address(_token) != address(0), "token is zero");
        require(_resolver != address(0), "resolver is zero");
        require(_feeRecipient != address(0), "fee recipient is zero");
        require(_feeBps <= MAX_FEE_BPS, "fee above cap");

        token = _token;
        resolver = _resolver;
        feeRecipient = _feeRecipient;
        feeBps = _feeBps;
        owner = msg.sender;
    }

    // ---------------------------------------------------------------------------------------
    // Market lifecycle
    // ---------------------------------------------------------------------------------------

    /// @notice Opens a market. The id is the off-chain commitment hash, so the on-chain market
    /// and the published terms are the same object and cannot drift apart.
    function openMarket(bytes32 marketId, uint8 outcomeCount, uint64 closesAt, uint64 resolveBy)
        external
        onlyResolver
    {
        require(markets[marketId].status == Status.None, "already exists");
        require(outcomeCount >= 2, "need two outcomes");
        require(closesAt > block.timestamp, "closes in the past");
        require(resolveBy > closesAt, "must resolve after it closes");

        markets[marketId] = Market({
            status: Status.Open,
            outcomeCount: outcomeCount,
            winningOutcome: 0,
            closesAt: closesAt,
            resolveBy: resolveBy,
            totalPool: 0,
            winningPool: 0,
            payoutPool: 0
        });
        emit MarketOpened(marketId, outcomeCount, closesAt, resolveBy);
    }

    /// @notice Stakes on an outcome. Caller must have approved this contract for `amount` first.
    ///
    /// @dev The transfer is measured rather than assumed: `received` is the actual balance
    /// change, not the requested amount. A token that takes a cut on transfer would otherwise
    /// credit more than arrived, and the shortfall would be discovered by whoever tried to
    /// withdraw last.
    function stake(bytes32 marketId, uint8 outcome, uint256 amount) external nonReentrant {
        Market storage m = markets[marketId];
        require(m.status == Status.Open, "not open");
        require(block.timestamp < m.closesAt, "closed");
        require(outcome < m.outcomeCount, "no such outcome");
        require(amount > 0, "zero stake");

        uint256 before = token.balanceOf(address(this));
        require(token.transferFrom(msg.sender, address(this), amount), "transfer failed");
        uint256 received = token.balanceOf(address(this)) - before;
        require(received > 0, "nothing received");

        stakeOf[marketId][msg.sender][outcome] += received;
        outcomePool[marketId][outcome] += received;
        m.totalPool += received;
        totalEscrowed += received;

        emit Staked(marketId, msg.sender, outcome, received);
    }

    /// @notice Declares the winning outcome and computes the split. Moves no money.
    ///
    /// @dev The fee comes out of the losing side only, matching the off-chain engine: a correct
    /// forecaster never receives less than they staked. If nobody backed the winning outcome the
    /// market voids instead — otherwise the house would collect most from questions nobody could
    /// answer, which is a direct incentive to write bad markets.
    function resolve(bytes32 marketId, uint8 winningOutcome) external onlyResolver {
        Market storage m = markets[marketId];
        require(m.status == Status.Open, "not open");
        require(block.timestamp >= m.closesAt, "still open");
        require(winningOutcome < m.outcomeCount, "no such outcome");

        uint256 winning = outcomePool[marketId][winningOutcome];
        if (winning == 0) {
            _void(marketId, "nobody backed the winning outcome");
            return;
        }

        uint256 losing = m.totalPool - winning;
        uint256 fee = (losing * feeBps) / 10_000;

        m.status = Status.Resolved;
        m.winningOutcome = winningOutcome;
        m.winningPool = winning;
        m.payoutPool = m.totalPool - fee;

        feesAccrued += fee;
        // The fee leaves escrow the moment it is earned; the rest stays until claimed.
        totalEscrowed -= fee;

        emit Resolved(marketId, winningOutcome, m.payoutPool, fee);
    }

    /// @notice Voids a market. Everyone may take back exactly what they staked, no fee.
    function voidMarket(bytes32 marketId, string calldata reason) external onlyResolver {
        require(markets[marketId].status == Status.Open, "not open");
        _void(marketId, reason);
    }

    /// @notice Anyone may void a market the resolver has abandoned.
    ///
    /// @dev The escape hatch, and the reason nobody's money can be held hostage. Callable by
    /// anyone — not just participants — because the point is that it does not depend on any
    /// particular party still being around or still caring.
    function expire(bytes32 marketId) external {
        Market storage m = markets[marketId];
        require(m.status == Status.Open, "not open");
        require(block.timestamp > uint256(m.resolveBy) + RESOLUTION_GRACE, "not abandoned yet");
        _void(marketId, "resolution deadline passed with no outcome");
    }

    function _void(bytes32 marketId, string memory reason) private {
        markets[marketId].status = Status.Voided;
        emit Voided(marketId, reason);
    }

    // ---------------------------------------------------------------------------------------
    // Getting paid
    // ---------------------------------------------------------------------------------------

    /// @notice Credits the caller what this market owes them. Does not transfer — see `withdraw`.
    ///
    /// @dev Split from the transfer on purpose. Claiming across several markets and withdrawing
    /// once is cheaper, and separating "work out what is owed" from "send it" keeps the only
    /// external call in the contract in one small function that does nothing else.
    ///
    /// Callable by anyone on anyone's behalf: it credits `staker`, never the caller, so a third
    /// party settling up for someone gains nothing and costs them nothing.
    function claim(bytes32 marketId, address staker) public {
        Market storage m = markets[marketId];
        require(m.status == Status.Resolved || m.status == Status.Voided, "not settled");
        require(!claimed[marketId][staker], "already claimed");

        uint256 amount;
        if (m.status == Status.Voided) {
            // Refund: exactly what they put in, across every outcome they backed.
            for (uint8 i = 0; i < m.outcomeCount; i++) {
                amount += stakeOf[marketId][staker][i];
            }
        } else {
            uint256 backed = stakeOf[marketId][staker][m.winningOutcome];
            if (backed > 0) {
                // Their share of the payout pool, in proportion to their share of the winning
                // side. Multiply before dividing, so the rounding loss is one wei rather than the
                // whole fraction.
                amount = (m.payoutPool * backed) / m.winningPool;
            }
        }

        claimed[marketId][staker] = true;
        if (amount > 0) {
            owed[staker] += amount;
            totalOwed += amount;
            totalEscrowed -= amount;
            emit Claimed(marketId, staker, amount);
        }
    }

    /// @notice Claims several markets in one transaction.
    /// @dev No external calls inside the loop, so one bad entry cannot strand the rest.
    function claimMany(bytes32[] calldata marketIds, address staker) external {
        for (uint256 i = 0; i < marketIds.length; i++) {
            if (!claimed[marketIds[i]][staker]) {
                claim(marketIds[i], staker);
            }
        }
    }

    /// @notice Sends the caller everything they are owed.
    ///
    /// @dev Checks-effects-interactions: the balance is zeroed before the transfer, so reentering
    /// finds nothing to take. The `nonReentrant` guard is a second lock on the same door.
    function withdraw() external nonReentrant {
        uint256 amount = owed[msg.sender];
        require(amount > 0, "nothing owed");

        owed[msg.sender] = 0;
        totalOwed -= amount;

        require(token.transfer(msg.sender, amount), "transfer failed");
        emit Withdrawn(msg.sender, amount);
    }

    /// @notice Sends accrued fees to the immutable fee recipient.
    ///
    /// @dev Can only move `feesAccrued`, which only `resolve` ever increases, and only by the
    /// fee on a losing side. There is no arithmetic path from a participant's stake into this
    /// number. Anyone may call it — it pays the fixed recipient regardless of who asked — so a
    /// lost owner key does not strand the revenue.
    function withdrawFees() external nonReentrant {
        uint256 amount = feesAccrued;
        require(amount > 0, "no fees");

        feesAccrued = 0;
        require(token.transfer(feeRecipient, amount), "transfer failed");
        emit FeesWithdrawn(feeRecipient, amount);
    }

    // ---------------------------------------------------------------------------------------
    // Administration. Note what is absent: no function moves escrow, and there is no upgrade path.
    // ---------------------------------------------------------------------------------------

    function setResolver(address newResolver) external onlyOwner {
        require(newResolver != address(0), "resolver is zero");
        emit ResolverChanged(resolver, newResolver);
        resolver = newResolver;
    }

    /// @dev Two-step would be safer against a typo. Kept one-step for size; if you change one
    /// thing before deploying, make it this.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "owner is zero");
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    // ---------------------------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------------------------

    /// @notice What `staker` would receive from this market, before they claim it.
    function claimable(bytes32 marketId, address staker) external view returns (uint256) {
        Market storage m = markets[marketId];
        if (claimed[marketId][staker]) return 0;
        if (m.status == Status.Voided) {
            uint256 total;
            for (uint8 i = 0; i < m.outcomeCount; i++) {
                total += stakeOf[marketId][staker][i];
            }
            return total;
        }
        if (m.status != Status.Resolved) return 0;
        uint256 backed = stakeOf[marketId][staker][m.winningOutcome];
        if (backed == 0 || m.winningPool == 0) return 0;
        return (m.payoutPool * backed) / m.winningPool;
    }

    /// @notice The solvency check, on chain: does this contract hold at least what it owes?
    ///
    /// @dev Should always be true. If it is ever false, stop and find out why before anything
    /// else — the same question `/v1/reserves` answers off-chain, asked of the chain instead so
    /// nobody has to take the operator's word for it.
    function isSolvent() external view returns (bool) {
        return token.balanceOf(address(this)) >= totalOwed + totalEscrowed + feesAccrued;
    }

    /// @notice Everything the contract believes it is holding, for reconciliation.
    function accounting()
        external
        view
        returns (uint256 held, uint256 owedToStakers, uint256 escrowed, uint256 fees)
    {
        return (token.balanceOf(address(this)), totalOwed, totalEscrowed, feesAccrued);
    }
}
