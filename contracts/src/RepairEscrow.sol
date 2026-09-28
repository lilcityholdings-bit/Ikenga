// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/**
 * @title RepairEscrow
 * @notice Non-custodial escrow for Auto Body Shop repair bounties (services/auto-body-shop).
 *
 * Off-chain, the shop's referee does the judging: it splits cases, runs the model, and scores the
 * sealed submissions. On-chain, this contract holds the money and enforces the parts of the
 * deal that don't need judgment, so the operator never holds anyone's funds:
 *
 *   1. The owner funds a bounty. Tokens move into this contract, not to the operator.
 *   2. The referee publishes its commit-reveal commitment here *before* submissions close, so
 *      there is a public, timestamped record that the tests were fixed in advance.
 *   3. Repairers register the hash of their sealed submission here before the deadline, per
 *      address. Settlement can only pay an address that itself registered the winning hash in
 *      time, so the referee can't name an arbitrary payee. Copying someone else's hash from the
 *      mempool gets a front-runner nothing: registration is per address, and the referee pays
 *      the address that actually submitted the fix.
 *   4. The referee settles with the same formula the shop uses (award, capped take, warranty,
 *      refund). The take can never exceed the cap fixed at deployment. A self-repair (the owner
 *      winning its own bounty) pays no take and holds no warranty.
 *   5. The warranty is decided by the referee (reproduced harm -> owner, otherwise -> repairer),
 *      and if the referee goes quiet it defaults to the repairer once the grace period passes.
 *   6. If the referee never settles, anyone can refund the owner after `settleBy`.
 *
 * Bounties are keyed by (funder, id), so nobody can squat an id someone else is about to use,
 * and each bounty remembers the referee it was funded under. Changing the referee only affects
 * bounties funded afterwards, so a compromised owner key can't redirect money already in escrow.
 *
 * What the contract CANNOT stop: a referee colluding with a sybil repairer that really did
 * submit. The judging is off-chain; see the shop README's trust-boundary section. What it does
 * stop: the operator touching escrow, taking more than the cap, paying someone who never
 * submitted, or sitting on funds forever.
 *
 * Accounting is pull-based: every settlement only credits `owed`, and tokens leave through
 * `withdraw`. The contract tracks everything it owes in `totalLiabilities`, and `isSolvent()` checks
 * the token balance covers it.
 */

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

contract RepairEscrow {
    enum State { None, Funded, Settled, Refunded }

    struct Bounty {
        address funder;
        address referee;
        uint64 submitBy;
        uint64 settleBy;
        State state;
        uint256 amount;
        bytes32 commitment;
        address repairer;
        uint256 warranty;
        uint64 warrantyUntil;
    }

    /// @notice Absolute ceiling on the platform take, checked at deployment. 20%.
    uint16 public constant MAX_TAKE_CAP_BPS = 2000;
    /// @notice How long after `warrantyUntil` the referee may still rule on a warranty claim.
    uint64 public constant WARRANTY_GRACE = 7 days;
    uint64 public constant MAX_WARRANTY = 90 days;
    uint64 public constant MAX_DURATION = 365 days;

    IERC20 public immutable token;
    address public immutable feeRecipient;
    uint16 public immutable maxTakeBps;

    address public owner;
    address public referee;

    mapping(bytes32 => Bounty) public bounties;
    /// @notice bounty key => repairer => sealed submission hash => registered in time.
    mapping(bytes32 => mapping(address => mapping(bytes32 => bool))) public registered;
    mapping(address => uint256) public owed;
    uint256 public fees;
    /// @notice Everything the contract owes: live escrow, held warranties, `owed` and `fees`.
    uint256 public totalLiabilities;

    uint256 private locked = 1;

    event Funded(bytes32 indexed key, bytes32 id, address indexed funder, address referee, uint256 amount,
                 uint64 submitBy, uint64 settleBy);
    event Committed(bytes32 indexed id, bytes32 commitment);
    event Submitted(bytes32 indexed id, address indexed repairer, bytes32 sealedHash);
    event Settled(bytes32 indexed id, address indexed repairer, uint256 award, uint256 take, uint256 paidNow,
                  uint256 warranty, uint256 refund);
    event WarrantyReleased(bytes32 indexed id, address indexed to, uint256 amount, bool harmed);
    event Expired(bytes32 indexed id, uint256 refunded);
    event Withdrawn(address indexed to, uint256 amount);
    event FeesWithdrawn(address indexed to, uint256 amount);
    event RefereeChanged(address indexed from, address indexed to);
    event OwnerChanged(address indexed from, address indexed to);

    modifier nonReentrant() {
        require(locked == 1, "reentrant");
        locked = 2;
        _;
        locked = 1;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyBountyReferee(bytes32 key) {
        require(msg.sender == bounties[key].referee, "not this bounty's referee");
        _;
    }

    constructor(IERC20 _token, address _referee, address _feeRecipient, uint16 _maxTakeBps) {
        require(address(_token) != address(0) && _referee != address(0) && _feeRecipient != address(0), "zero address");
        require(_maxTakeBps <= MAX_TAKE_CAP_BPS, "take cap too high");
        token = _token;
        referee = _referee;
        feeRecipient = _feeRecipient;
        maxTakeBps = _maxTakeBps;
        owner = msg.sender;
    }

    // ---- lifecycle ------------------------------------------------------------------------

    /// @notice A bounty's storage key. Including the funder means nobody else can claim an id first.
    function bountyKey(address funder, bytes32 id) public pure returns (bytes32) {
        return keccak256(abi.encode(funder, id));
    }

    function fund(bytes32 id, uint256 amount, uint64 submitBy, uint64 settleBy) external nonReentrant returns (bytes32 key) {
        key = bountyKey(msg.sender, id);
        require(bounties[key].state == State.None, "bounty exists");
        require(amount > 0, "zero amount");
        require(submitBy > block.timestamp && settleBy > submitBy, "bad deadlines");
        require(settleBy - block.timestamp <= MAX_DURATION, "too long");
        bounties[key] = Bounty(msg.sender, referee, submitBy, settleBy, State.Funded, amount, bytes32(0), address(0), 0, 0);
        totalLiabilities += amount;
        _pull(msg.sender, amount);
        emit Funded(key, id, msg.sender, referee, amount, submitBy, settleBy);
    }

    function commit(bytes32 key, bytes32 commitment) external onlyBountyReferee(key) {
        Bounty storage b = bounties[key];
        require(b.state == State.Funded && b.commitment == bytes32(0), "cannot commit");
        require(commitment != bytes32(0), "empty commitment");
        require(block.timestamp < b.submitBy, "submissions closed");
        b.commitment = commitment;
        emit Committed(key, commitment);
    }

    function submit(bytes32 key, bytes32 sealedHash) external {
        Bounty storage b = bounties[key];
        require(b.state == State.Funded && b.commitment != bytes32(0), "not open");
        require(block.timestamp < b.submitBy, "submissions closed");
        require(sealedHash != bytes32(0), "empty hash");
        registered[key][msg.sender][sealedHash] = true;
        emit Submitted(key, msg.sender, sealedHash);
    }

    /// @param award  0 means no winner: everything goes back to the funder.
    function settle(bytes32 key, address repairer, bytes32 winningHash, uint256 award, uint16 takeBps, uint16 warrantyBps,
                    uint64 warrantyUntil) external onlyBountyReferee(key) {
        Bounty storage b = bounties[key];
        require(b.state == State.Funded, "not funded");
        require(block.timestamp >= b.submitBy && block.timestamp <= b.settleBy, "outside settlement window");
        require(award <= b.amount, "award exceeds escrow");
        require(takeBps <= maxTakeBps && warrantyBps <= 10000, "bad bps");
        b.state = State.Settled;
        if (award == 0) {
            owed[b.funder] += b.amount;
            emit Settled(key, address(0), 0, 0, 0, 0, b.amount);
            return;
        }
        require(registered[key][repairer][winningHash], "repairer did not register this submission");
        bool selfRepair = repairer == b.funder;
        uint256 take = selfRepair ? 0 : award * takeBps / 10000;
        uint256 warranty = selfRepair ? 0 : (award - take) * warrantyBps / 10000;
        uint256 paidNow = award - take - warranty;
        uint256 refund = b.amount - award;
        if (warranty > 0) {
            require(warrantyUntil >= block.timestamp && warrantyUntil <= block.timestamp + MAX_WARRANTY, "bad warranty period");
        }
        b.repairer = repairer;
        b.warranty = warranty;
        b.warrantyUntil = warrantyUntil;
        owed[repairer] += paidNow;
        owed[b.funder] += refund;
        fees += take;
        emit Settled(key, repairer, award, take, paidNow, warranty, refund);
    }

    /// @notice The referee's ruling on a warranty claim: `harmed` only when the harm reproduced.
    function resolveWarranty(bytes32 key, bool harmed) external onlyBountyReferee(key) {
        Bounty storage b = bounties[key];
        require(b.state == State.Settled && b.warranty > 0, "no warranty held");
        require(block.timestamp <= b.warrantyUntil + WARRANTY_GRACE, "ruling window over");
        _releaseWarranty(key, b, harmed ? b.funder : b.repairer, harmed);
    }

    /// @notice Anyone can release an unruled warranty to the repairer once the grace period ends:
    /// with no verified claim, the money follows the fix.
    function claimWarranty(bytes32 key) external {
        Bounty storage b = bounties[key];
        require(b.state == State.Settled && b.warranty > 0, "no warranty held");
        require(block.timestamp > b.warrantyUntil + WARRANTY_GRACE, "grace period not over");
        _releaseWarranty(key, b, b.repairer, false);
    }

    /// @notice If the referee never settles, anyone can return the escrow to the funder.
    function expire(bytes32 key) external {
        Bounty storage b = bounties[key];
        require(b.state == State.Funded && block.timestamp > b.settleBy, "not expired");
        b.state = State.Refunded;
        owed[b.funder] += b.amount;
        emit Expired(key, b.amount);
    }

    function _releaseWarranty(bytes32 id, Bounty storage b, address to, bool harmed) private {
        uint256 amount = b.warranty;
        b.warranty = 0;
        owed[to] += amount;
        emit WarrantyReleased(id, to, amount, harmed);
    }

    // ---- money out --------------------------------------------------------------------------

    function withdraw() external nonReentrant {
        uint256 amount = owed[msg.sender];
        require(amount > 0, "nothing owed");
        owed[msg.sender] = 0;
        totalLiabilities -= amount;
        _send(msg.sender, amount);
        emit Withdrawn(msg.sender, amount);
    }

    function withdrawFees() external nonReentrant {
        uint256 amount = fees;
        require(amount > 0, "no fees");
        fees = 0;
        totalLiabilities -= amount;
        _send(feeRecipient, amount);
        emit FeesWithdrawn(feeRecipient, amount);
    }

    // ---- administration: note what is absent. No function moves escrow. --------------------

    function setReferee(address newReferee) external onlyOwner {
        require(newReferee != address(0), "zero address");
        emit RefereeChanged(referee, newReferee);
        referee = newReferee;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero address");
        emit OwnerChanged(owner, newOwner);
        owner = newOwner;
    }

    function isSolvent() external view returns (bool) {
        return token.balanceOf(address(this)) >= totalLiabilities;
    }

    // ---- token plumbing: tolerate tokens that return nothing; refuse fee-on-transfer tokens --

    function _pull(address from, uint256 amount) private {
        uint256 before = token.balanceOf(address(this));
        (bool ok, bytes memory data) =
            address(token).call(abi.encodeWithSelector(IERC20.transferFrom.selector, from, address(this), amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transferFrom failed");
        require(token.balanceOf(address(this)) - before == amount, "fee-on-transfer token");
    }

    function _send(address to, uint256 amount) private {
        (bool ok, bytes memory data) = address(token).call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transfer failed");
    }
}
