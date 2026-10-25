// @exports: GridProofRegistry(address issuer) -> contract, owner = issuer (OpenZeppelin 5.0.2 Ownable; owner() view)
// @exports: registerAsset(bytes32 assetId, string bmUnit, uint32 capacityKw) onlyOwner  // reverts "ASSET_EXISTS"
// @exports: issueBatch(bytes32 assetId, uint32 day, bytes32 root, bytes32 modelHash, bytes32 snapshotHash, uint64 totalKwh, uint16 intervals, address holder) onlyOwner  // reverts "UNKNOWN_ASSET" "BATCH_EXISTS" "EMPTY_BATCH" "EXCEEDS_DAILY_CAPACITY"
// @exports: retire(bytes32 assetId, uint32 day, uint8 sp, uint64 kwh, uint32 gPerKwh, bytes32[] proof, bytes32 beneficiaryRef)  // reverts "BATCH_MISSING" "NOT_HOLDER" "EXCEEDS_INTERVAL_CAPACITY" "BAD_PROOF" "ALREADY_RETIRED"
// @exports: leafOf(bytes32,uint32,uint8,uint64,uint32) pure -> bytes32  // keccak256(bytes.concat(keccak256(abi.encode(assetId,day,sp,kwh,gPerKwh))))
// @exports: verifyLeaf(bytes32,uint32,uint8,uint64,uint32,bytes32[] proof) view -> bool
// @exports: batches(bytes32,uint32) -> (root,modelHash,snapshotHash,totalKwh,intervals,issuedAt,holder)
// @exports: assets(bytes32) -> (capacityKw,exists) ; retiredBy(bytes32) -> address ; gramsBy(bytes32) -> uint256
// @exports: event AssetRegistered(bytes32 indexed assetId, string bmUnit, uint32 capacityKw)
// @exports: event BatchIssued(bytes32 indexed assetId, uint32 indexed day, bytes32 root, bytes32 modelHash, bytes32 snapshotHash, uint64 totalKwh, uint16 intervals, address holder)
// @exports: event Retired(bytes32 indexed assetId, uint32 indexed day, uint8 sp, bytes32 indexed beneficiaryRef, uint64 kwh, uint32 gPerKwh, uint256 grams)
// @imports: @openzeppelin/contracts/access/Ownable.sol:Ownable
// @imports: @openzeppelin/contracts/utils/cryptography/MerkleProof.sol:MerkleProof.verifyCalldata(bytes32[] proof, bytes32 root, bytes32 leaf)
// @env: none
// @schema: leaf ABI types (bytes32 assetId, uint32 day, uint8 sp, uint64 kwh, uint32 g); Merkle: sorted-pair hashing, odd nodes promoted unchanged
// @schema: revert strings (not custom errors) except OpenZeppelin's OwnableUnauthorizedAccount(address)
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {MerkleProof} from "@openzeppelin/contracts/utils/cryptography/MerkleProof.sol";

contract GridProofRegistry is Ownable {
    struct Asset {
        uint32 capacityKw;
        bool exists;
    }

    struct Batch {
        bytes32 root;
        bytes32 modelHash;
        bytes32 snapshotHash;
        uint64 totalKwh;
        uint16 intervals;
        uint64 issuedAt;
        address holder;
    }

    mapping(bytes32 => Asset) public assets;
    mapping(bytes32 => mapping(uint32 => Batch)) public batches;
    mapping(bytes32 => address) public retiredBy;
    mapping(bytes32 => uint256) public gramsBy;

    event AssetRegistered(bytes32 indexed assetId, string bmUnit, uint32 capacityKw);
    event BatchIssued(
        bytes32 indexed assetId,
        uint32 indexed day,
        bytes32 root,
        bytes32 modelHash,
        bytes32 snapshotHash,
        uint64 totalKwh,
        uint16 intervals,
        address holder
    );
    event Retired(
        bytes32 indexed assetId,
        uint32 indexed day,
        uint8 sp,
        bytes32 indexed beneficiaryRef,
        uint64 kwh,
        uint32 gPerKwh,
        uint256 grams
    );

    constructor(address issuer) Ownable(issuer) {}

    function registerAsset(bytes32 assetId, string calldata bmUnit, uint32 capacityKw) external onlyOwner {
        require(!assets[assetId].exists, "ASSET_EXISTS");
        assets[assetId] = Asset({capacityKw: capacityKw, exists: true});
        emit AssetRegistered(assetId, bmUnit, capacityKw);
    }

    function issueBatch(
        bytes32 assetId,
        uint32 day,
        bytes32 root,
        bytes32 modelHash,
        bytes32 snapshotHash,
        uint64 totalKwh,
        uint16 intervals,
        address holder
    ) external onlyOwner {
        require(assets[assetId].exists, "UNKNOWN_ASSET");
        Batch storage b = batches[assetId][day];
        require(b.issuedAt == 0, "BATCH_EXISTS");
        require(intervals > 0, "EMPTY_BATCH");
        // 25 hours of full output: covers the 50-period (fall-back) day
        require(uint256(totalKwh) <= uint256(assets[assetId].capacityKw) * 25, "EXCEEDS_DAILY_CAPACITY");

        b.root = root;
        b.modelHash = modelHash;
        b.snapshotHash = snapshotHash;
        b.totalKwh = totalKwh;
        b.intervals = intervals;
        b.issuedAt = uint64(block.timestamp);
        b.holder = holder;

        _emitIssued(assetId, day, b);
    }

    function retire(
        bytes32 assetId,
        uint32 day,
        uint8 sp,
        uint64 kwh,
        uint32 gPerKwh,
        bytes32[] calldata proof,
        bytes32 beneficiaryRef
    ) external {
        Batch storage b = batches[assetId][day];
        require(b.issuedAt != 0, "BATCH_MISSING");
        require(b.holder == msg.sender, "NOT_HOLDER");
        // kwh > capacityKw * 0.5 * 1.02  <=>  kwh * 20000 > capacityKw * 10200
        require(
            uint256(kwh) * 20000 <= uint256(assets[assetId].capacityKw) * 10200,
            "EXCEEDS_INTERVAL_CAPACITY"
        );
        bytes32 leaf = leafOf(assetId, day, sp, kwh, gPerKwh);
        require(MerkleProof.verifyCalldata(proof, b.root, leaf), "BAD_PROOF");
        require(retiredBy[leaf] == address(0), "ALREADY_RETIRED");

        retiredBy[leaf] = msg.sender;
        uint256 grams = uint256(kwh) * uint256(gPerKwh);
        gramsBy[beneficiaryRef] += grams;

        emit Retired(assetId, day, sp, beneficiaryRef, kwh, gPerKwh, grams);
    }

    function leafOf(bytes32 assetId, uint32 day, uint8 sp, uint64 kwh, uint32 gPerKwh)
        public
        pure
        returns (bytes32)
    {
        return keccak256(bytes.concat(keccak256(abi.encode(assetId, day, sp, kwh, gPerKwh))));
    }

    function verifyLeaf(
        bytes32 assetId,
        uint32 day,
        uint8 sp,
        uint64 kwh,
        uint32 gPerKwh,
        bytes32[] calldata proof
    ) external view returns (bool) {
        Batch storage b = batches[assetId][day];
        if (b.issuedAt == 0) {
            return false;
        }
        return MerkleProof.verifyCalldata(proof, b.root, leafOf(assetId, day, sp, kwh, gPerKwh));
    }

    // Separate function so issueBatch does not hit "stack too deep" with 8 parameters.
    function _emitIssued(bytes32 assetId, uint32 day, Batch storage b) private {
        emit BatchIssued(assetId, day, b.root, b.modelHash, b.snapshotHash, b.totalKwh, b.intervals, b.holder);
    }
}
