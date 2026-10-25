// @exports: none (Hardhat/mocha test suite, 10 tests)
// @imports: contracts/GridProofRegistry.sol:GridProofRegistry (constructor, registerAsset, issueBatch, retire, leafOf, verifyLeaf, batches, retiredBy, gramsBy)
// @env: none
// @schema: JS reference Merkle implementation (sorted pairs, odd node promoted unchanged) used to build roots and proofs independently of the contract
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");

const abi = ethers.AbiCoder.defaultAbiCoder();
const LEAF_TYPES = ["bytes32", "uint32", "uint8", "uint64", "uint32"];

const BM_UNIT = "T_TEST-1";
const CAP_KW = 400000; // interval cap = 400000 * 0.5 * 1.02 = 204000 kWh
const DAY = 20250315;
const MODEL_HASH = ethers.sha256(ethers.toUtf8Bytes("model"));
const SNAP_HASH = ethers.sha256(ethers.toUtf8Bytes("snapshot"));
const REF = ethers.encodeBytes32String("ACME");

// [sp, kwh, gPerKwh]; five rows gives an odd-sized tree (exercises promotion)
const ROWS = [
  [1, 100000, 150],
  [2, 120000, 160],
  [3, 90000, 170],
  [4, 150000, 180],
  [5, 110000, 190],
];

function leafOf(assetId, day, sp, kwh, g) {
  return ethers.keccak256(ethers.keccak256(abi.encode(LEAF_TYPES, [assetId, day, sp, kwh, g])));
}

function pairHash(a, b) {
  const [lo, hi] = BigInt(a) < BigInt(b) ? [a, b] : [b, a];
  return ethers.keccak256(ethers.concat([lo, hi]));
}

function buildLevels(leaves) {
  const levels = [leaves.slice()];
  while (levels[levels.length - 1].length > 1) {
    const cur = levels[levels.length - 1];
    const next = [];
    for (let i = 0; i < cur.length; i += 2) {
      next.push(i + 1 < cur.length ? pairHash(cur[i], cur[i + 1]) : cur[i]);
    }
    levels.push(next);
  }
  return levels;
}

function rootOf(leaves) {
  const levels = buildLevels(leaves);
  return levels[levels.length - 1][0];
}

function proofOf(leaves, index) {
  const levels = buildLevels(leaves);
  const proof = [];
  let idx = index;
  for (let lvl = 0; lvl < levels.length - 1; lvl++) {
    const sibling = idx ^ 1;
    if (sibling < levels[lvl].length) {
      proof.push(levels[lvl][sibling]);
    }
    idx = idx >> 1;
  }
  return proof;
}

async function deployFixture() {
  const [owner, holder, stranger] = await ethers.getSigners();
  const Registry = await ethers.getContractFactory("GridProofRegistry");
  const registry = await Registry.deploy(owner.address);
  await registry.waitForDeployment();
  const assetId = ethers.keccak256(ethers.toUtf8Bytes(BM_UNIT));
  await registry.registerAsset(assetId, BM_UNIT, CAP_KW);
  return { registry, owner, holder, stranger, assetId };
}

async function issue(registry, assetId, day, rows, holderAddress) {
  const leaves = rows.map(([sp, kwh, g]) => leafOf(assetId, day, sp, kwh, g));
  const total = rows.reduce((acc, [, kwh]) => acc + kwh, 0);
  await registry.issueBatch(assetId, day, rootOf(leaves), MODEL_HASH, SNAP_HASH, total, rows.length, holderAddress);
  return leaves;
}

describe("GridProofRegistry", function () {
  it("1. only the owner can register assets and issue batches", async function () {
    const { registry, stranger, assetId } = await loadFixture(deployFixture);
    const other = ethers.keccak256(ethers.toUtf8Bytes("T_OTHER-1"));
    await expect(registry.connect(stranger).registerAsset(other, "T_OTHER-1", 1000))
      .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
      .withArgs(stranger.address);
    await expect(
      registry.connect(stranger).issueBatch(assetId, DAY, MODEL_HASH, MODEL_HASH, SNAP_HASH, 1, 1, stranger.address)
    )
      .to.be.revertedWithCustomError(registry, "OwnableUnauthorizedAccount")
      .withArgs(stranger.address);
  });

  it("2. issueBatch stores the batch and emits BatchIssued", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const leaves = ROWS.map(([sp, kwh, g]) => leafOf(assetId, DAY, sp, kwh, g));
    const root = rootOf(leaves);
    const total = ROWS.reduce((acc, [, kwh]) => acc + kwh, 0);
    await expect(registry.issueBatch(assetId, DAY, root, MODEL_HASH, SNAP_HASH, total, ROWS.length, holder.address))
      .to.emit(registry, "BatchIssued")
      .withArgs(assetId, DAY, root, MODEL_HASH, SNAP_HASH, total, ROWS.length, holder.address);
    const b = await registry.batches(assetId, DAY);
    expect(b.root).to.equal(root);
    expect(b.modelHash).to.equal(MODEL_HASH);
    expect(b.snapshotHash).to.equal(SNAP_HASH);
    expect(b.totalKwh).to.equal(BigInt(total));
    expect(b.intervals).to.equal(BigInt(ROWS.length));
    expect(b.holder).to.equal(holder.address);
    expect(b.issuedAt > 0n).to.equal(true);
  });

  it("3. duplicate asset, duplicate batch, unknown asset and empty batch are rejected", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    await expect(registry.registerAsset(assetId, BM_UNIT, CAP_KW)).to.be.revertedWith("ASSET_EXISTS");
    await issue(registry, assetId, DAY, ROWS, holder.address);
    await expect(
      registry.issueBatch(assetId, DAY, MODEL_HASH, MODEL_HASH, SNAP_HASH, 1, 1, holder.address)
    ).to.be.revertedWith("BATCH_EXISTS");
    const unknown = ethers.keccak256(ethers.toUtf8Bytes("T_UNKNOWN-1"));
    await expect(
      registry.issueBatch(unknown, DAY, MODEL_HASH, MODEL_HASH, SNAP_HASH, 1, 1, holder.address)
    ).to.be.revertedWith("UNKNOWN_ASSET");
    await expect(
      registry.issueBatch(assetId, DAY + 1, MODEL_HASH, MODEL_HASH, SNAP_HASH, 0, 0, holder.address)
    ).to.be.revertedWith("EMPTY_BATCH");
  });

  it("4. daily capacity guard: capacityKw * 25 passes, one kWh more reverts", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const limit = CAP_KW * 25;
    await registry.issueBatch(assetId, 20250316, MODEL_HASH, MODEL_HASH, SNAP_HASH, limit, 1, holder.address);
    await expect(
      registry.issueBatch(assetId, 20250317, MODEL_HASH, MODEL_HASH, SNAP_HASH, limit + 1, 1, holder.address)
    ).to.be.revertedWith("EXCEEDS_DAILY_CAPACITY");
  });

  it("5. interval capacity guard: 204000 kWh retires, 204001 kWh reverts", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const day = 20250318;
    const rows = [
      [1, 204001, 100],
      [2, 204000, 100],
    ];
    const leaves = await issue(registry, assetId, day, rows, holder.address);
    await expect(
      registry.connect(holder).retire(assetId, day, 1, 204001, 100, proofOf(leaves, 0), REF)
    ).to.be.revertedWith("EXCEEDS_INTERVAL_CAPACITY");
    await expect(registry.connect(holder).retire(assetId, day, 2, 204000, 100, proofOf(leaves, 1), REF)).to.emit(
      registry,
      "Retired"
    );
  });

  it("6. retire records the holder, emits Retired and accumulates grams per beneficiary", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const leaves = await issue(registry, assetId, DAY, ROWS, holder.address);
    const [sp3, kwh3, g3] = ROWS[2];
    await expect(registry.connect(holder).retire(assetId, DAY, sp3, kwh3, g3, proofOf(leaves, 2), REF))
      .to.emit(registry, "Retired")
      .withArgs(assetId, DAY, sp3, REF, kwh3, g3, BigInt(kwh3) * BigInt(g3));
    expect(await registry.retiredBy(leaves[2])).to.equal(holder.address);
    const [sp1, kwh1, g1] = ROWS[0];
    await registry.connect(holder).retire(assetId, DAY, sp1, kwh1, g1, proofOf(leaves, 0), REF);
    expect(await registry.gramsBy(REF)).to.equal(BigInt(kwh3) * BigInt(g3) + BigInt(kwh1) * BigInt(g1));
  });

  it("7. an interval cannot be retired twice", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const leaves = await issue(registry, assetId, DAY, ROWS, holder.address);
    const [sp, kwh, g] = ROWS[3];
    await registry.connect(holder).retire(assetId, DAY, sp, kwh, g, proofOf(leaves, 3), REF);
    await expect(
      registry.connect(holder).retire(assetId, DAY, sp, kwh, g, proofOf(leaves, 3), REF)
    ).to.be.revertedWith("ALREADY_RETIRED");
  });

  it("8. forged kWh, forged intensity and wrong proofs fail with BAD_PROOF", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const leaves = await issue(registry, assetId, DAY, ROWS, holder.address);
    const [sp, kwh, g] = ROWS[1];
    const proof = proofOf(leaves, 1);
    await expect(
      registry.connect(holder).retire(assetId, DAY, sp, kwh + 1, g, proof, REF)
    ).to.be.revertedWith("BAD_PROOF");
    await expect(
      registry.connect(holder).retire(assetId, DAY, sp, kwh, g + 1, proof, REF)
    ).to.be.revertedWith("BAD_PROOF");
    await expect(
      registry.connect(holder).retire(assetId, DAY, sp, kwh, g, proofOf(leaves, 0), REF)
    ).to.be.revertedWith("BAD_PROOF");
    expect(await registry.verifyLeaf(assetId, DAY, sp, kwh, g, proof)).to.equal(true);
    expect(await registry.verifyLeaf(assetId, DAY, sp, kwh + 1, g, proof)).to.equal(false);
  });

  it("9. only the batch holder can retire, and a missing batch reverts", async function () {
    const { registry, holder, stranger, assetId } = await loadFixture(deployFixture);
    const leaves = await issue(registry, assetId, DAY, ROWS, holder.address);
    const [sp, kwh, g] = ROWS[0];
    await expect(
      registry.connect(stranger).retire(assetId, DAY, sp, kwh, g, proofOf(leaves, 0), REF)
    ).to.be.revertedWith("NOT_HOLDER");
    await expect(
      registry.connect(holder).retire(assetId, 20250101, sp, kwh, g, proofOf(leaves, 0), REF)
    ).to.be.revertedWith("BATCH_MISSING");
  });

  it("10. leafOf matches the JS leaf and verifyLeaf accepts every leaf in trees of size 1 to 5", async function () {
    const { registry, holder, assetId } = await loadFixture(deployFixture);
    const samples = [
      [assetId, 20250330, 1, 0, 0],
      [assetId, 20250330, 46, 123456, 77],
      [assetId, 20251026, 50, 204000, 4294967295],
      [assetId, 20250315, 23, 18446744073709551615n, 1],
    ];
    for (const [a, d, sp, kwh, g] of samples) {
      expect(await registry.leafOf(a, d, sp, kwh, g)).to.equal(leafOf(a, d, sp, kwh, g));
    }
    for (let size = 1; size <= ROWS.length; size++) {
      const day = 20250400 + size;
      const rows = ROWS.slice(0, size);
      const leaves = await issue(registry, assetId, day, rows, holder.address);
      for (let i = 0; i < size; i++) {
        const [sp, kwh, g] = rows[i];
        expect(await registry.verifyLeaf(assetId, day, sp, kwh, g, proofOf(leaves, i))).to.equal(true);
      }
    }
  });
});
