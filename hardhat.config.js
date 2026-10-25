// @exports: hardhat config: solc 0.8.24, optimizer runs 200, evmVersion paris; networks hardhat, localhost (http://127.0.0.1:8545, chainId 31337), sepolia (chainId 11155111)
// @imports: none
// @env: TESTNET_RPC_URL
// @env: DEPLOYER_PRIVATE_KEY
// @schema: network "sepolia" is defined only when TESTNET_RPC_URL and DEPLOYER_PRIVATE_KEY are both set; running with `--network sepolia` and either missing or malformed throws immediately
// @schema: values are read from process.env first, then from ./.env (parsed here; no dotenv dependency)
require("@nomicfoundation/hardhat-toolbox");
const fs = require("fs");
const path = require("path");

function readDotEnv(file) {
  const out = {};
  if (!fs.existsSync(file)) {
    return out;
  }
  fs.readFileSync(file, "utf8")
    .split(/\r?\n/)
    .forEach((raw, i) => {
      const line = raw.trim();
      if (!line || line.startsWith("#")) {
        return;
      }
      const eq = line.indexOf("=");
      if (eq < 1) {
        throw new Error(`${file}:${i + 1}: expected KEY=VALUE, got "${raw}". Fix .env (see .env.example).`);
      }
      out[line.slice(0, eq).trim()] = line
        .slice(eq + 1)
        .trim()
        .replace(/^["']|["']$/g, "");
    });
  return out;
}

const fileEnv = readDotEnv(path.join(__dirname, ".env"));

function envVar(name) {
  const fromProcess = process.env[name];
  if (fromProcess !== undefined && fromProcess !== "") {
    return fromProcess;
  }
  return fileEnv[name] || "";
}

const testnetUrl = envVar("TESTNET_RPC_URL");
const deployerKey = envVar("DEPLOYER_PRIVATE_KEY");
const wantsSepolia = process.argv.some((a) => a === "sepolia" || a === "--network=sepolia");

if (wantsSepolia) {
  if (!/^https?:\/\//.test(testnetUrl)) {
    throw new Error(
      "TESTNET_RPC_URL is missing or not an http(s) URL. Set it in .env to a Sepolia JSON-RPC endpoint (see .env.example)."
    );
  }
  if (!/^0x[0-9a-fA-F]{64}$/.test(deployerKey)) {
    throw new Error(
      "DEPLOYER_PRIVATE_KEY is missing or malformed. Set it in .env to a funded Sepolia key: 0x followed by 64 hex characters."
    );
  }
}

const networks = {
  hardhat: { chainId: 31337 },
  localhost: { url: "http://127.0.0.1:8545", chainId: 31337 },
};

if (/^https?:\/\//.test(testnetUrl) && /^0x[0-9a-fA-F]{64}$/.test(deployerKey)) {
  networks.sepolia = {
    url: testnetUrl,
    chainId: 11155111,
    accounts: [deployerKey],
  };
}

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: "paris",
    },
  },
  networks,
};
