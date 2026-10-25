// @exports: none (Hardhat script) -> deploys GridProofRegistry(issuer = first signer) and writes deployments/{network}.json
// @imports: contracts/GridProofRegistry.sol:GridProofRegistry(address issuer)
// @env: none (network credentials are read in hardhat.config.js)
// @schema: CLI `hardhat run scripts/deploy.js --network localhost|sepolia`; refuses any other network
// @schema: deployments/{network}.json = {"network": string, "chainId": number, "address": string, "issuer": string, "txHash": string, "blockNumber": number}
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

async function main() {
  const { ethers, network } = hre;
  if (network.name !== "localhost" && network.name !== "sepolia") {
    throw new Error(
      `Refusing to deploy on network "${network.name}": the deployment record would be useless. ` +
        "Use `npm run deploy:local` (needs `npm run node` running) or `npm run deploy:testnet`."
    );
  }

  const signers = await ethers.getSigners();
  if (signers.length === 0) {
    throw new Error(
      `No signer is available on network "${network.name}". For sepolia set DEPLOYER_PRIVATE_KEY in .env.`
    );
  }
  const deployer = signers[0];
  const balance = await ethers.provider.getBalance(deployer.address);
  if (balance === 0n) {
    throw new Error(
      `Deployer ${deployer.address} has 0 ETH on ${network.name}. Fund it (Sepolia faucet) and rerun.`
    );
  }

  const Registry = await ethers.getContractFactory("GridProofRegistry");
  const registry = await Registry.deploy(deployer.address);
  await registry.waitForDeployment();
  const receipt = await registry.deploymentTransaction().wait();
  const net = await ethers.provider.getNetwork();

  const record = {
    network: network.name,
    chainId: Number(net.chainId),
    address: await registry.getAddress(),
    issuer: deployer.address,
    txHash: receipt.hash,
    blockNumber: receipt.blockNumber,
  };

  const dir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${network.name}.json`);
  fs.writeFileSync(file, JSON.stringify(record, null, 2) + "\n");

  console.log(`GridProofRegistry deployed on ${record.network} (chainId ${record.chainId})`);
  console.log(`  address:  ${record.address}`);
  console.log(`  issuer:   ${record.issuer}`);
  console.log(`  tx:       ${record.txHash}`);
  console.log(`  record:   ${path.relative(process.cwd(), file)}`);
}

main().catch((err) => {
  console.error("DEPLOY FAILED:", err.message || err);
  process.exitCode = 1;
});
