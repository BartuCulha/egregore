/**
 * E2E test for local mode install flows.
 *
 * Simulates two real GitHub users: founder creates an Egregore,
 * invites the joiner, joiner joins. Verifies repos, config files,
 * memory structure, collaboration, and Telegram notifications.
 *
 * Requires env vars:
 *   E2E_FOUNDER_TOKEN  — GitHub PAT (scopes: repo, read:org, delete_repo)
 *   E2E_JOINER_TOKEN   — GitHub PAT (scopes: repo, read:org)
 *   E2E_TEST_ORG       — GitHub org or personal account
 *   E2E_TELEGRAM_BOT_TOKEN  — (optional) Telegram bot token
 *   E2E_TELEGRAM_GROUP_ID   — (optional) Telegram group chat ID
 *
 * Usage:
 *   E2E_FOUNDER_TOKEN=ghp_xxx E2E_JOINER_TOKEN=ghp_yyy E2E_TEST_ORG=my-org \
 *     node --test packages/create-egregore/test/e2e-local.test.js
 */

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const https = require("node:https");
const { execFileSync, execSync } = require("node:child_process");

const { getUser, repoExists, addCollaborator } = require("../lib/local");
const { ghApi, embedToken, initMemoryDirs, run } = require("../lib/setup");

// ── Env vars ────────────────────────────────────────────────────────

const FOUNDER_TOKEN = process.env.E2E_FOUNDER_TOKEN;
const JOINER_TOKEN = process.env.E2E_JOINER_TOKEN;
const TEST_ORG = process.env.E2E_TEST_ORG;
const TELEGRAM_BOT_TOKEN = process.env.E2E_TELEGRAM_BOT_TOKEN;
const TELEGRAM_GROUP_ID = process.env.E2E_TELEGRAM_GROUP_ID;

if (!FOUNDER_TOKEN || !JOINER_TOKEN || !TEST_ORG) {
  console.error("\nMissing required env vars:");
  if (!FOUNDER_TOKEN) console.error("  E2E_FOUNDER_TOKEN — GitHub PAT for founder (scopes: repo, read:org, delete_repo)");
  if (!JOINER_TOKEN) console.error("  E2E_JOINER_TOKEN  — GitHub PAT for joiner (scopes: repo, read:org)");
  if (!TEST_ORG) console.error("  E2E_TEST_ORG      — GitHub org or personal account");
  console.error("");
  process.exit(1);
}

if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_GROUP_ID) {
  console.log("Note: Telegram env vars not set — Telegram test will be skipped.\n");
}

// ── Test identifiers ────────────────────────────────────────────────

const TEST_ID = `e2e-${Date.now()}`;
const EGREGORE_REPO = `egregore-${TEST_ID}`;
const MEMORY_REPO = `${TEST_ORG}-memory-${TEST_ID}`;
const TODAY = new Date().toISOString().split("T")[0];

let founderLogin, founderName, founderEmail;
let joinerLogin, joinerName;
let founderWorkspace, joinerWorkspace;
let founderEgregoreDir, founderMemoryDir;
let joinerEgregoreDir, joinerMemoryDir;

// ── Helper functions (thin wrappers around ghApi) ───────────────────

async function createTestRepo(token, org, name, description) {
  // Detect if org is a personal account
  const { status: orgStatus } = await ghApi("GET", `/orgs/${org}`, token);
  const isOrg = orgStatus === 200;
  const apiPath = isOrg ? `/orgs/${org}/repos` : "/user/repos";
  const { status, data } = await ghApi("POST", apiPath, token, {
    name,
    private: true,
    auto_init: true,
    description: description || "E2E test repo",
  });
  if (status !== 201 && status !== 200) {
    throw new Error(`Create repo failed (HTTP ${status}): ${JSON.stringify(data)}`);
  }
  return data;
}

async function putFile(token, org, repo, filePath, content, message) {
  // Check if file exists to get SHA
  const { status: existStatus, data: existData } = await ghApi(
    "GET",
    `/repos/${org}/${repo}/contents/${filePath}`,
    token,
  );
  const body = {
    message: message || `Add ${filePath}`,
    content: Buffer.from(content).toString("base64"),
  };
  if (existStatus === 200 && existData.sha) {
    body.sha = existData.sha;
  }
  const { status, data } = await ghApi(
    "PUT",
    `/repos/${org}/${repo}/contents/${filePath}`,
    token,
    body,
  );
  if (status !== 200 && status !== 201) {
    throw new Error(`putFile ${filePath} failed (HTTP ${status}): ${JSON.stringify(data)}`);
  }
  return data;
}

async function getFile(token, org, repo, filePath) {
  const { status, data } = await ghApi(
    "GET",
    `/repos/${org}/${repo}/contents/${filePath}`,
    token,
  );
  if (status !== 200 || !data.content) return null;
  return Buffer.from(data.content, "base64").toString("utf-8");
}

async function deleteRepo(token, org, repo) {
  const { status } = await ghApi("DELETE", `/repos/${org}/${repo}`, token);
  return status === 204;
}

async function waitForTestRepo(token, org, repo, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await repoExists(token, org, repo)) return true;
    await sleep(2000);
  }
  return false;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Git env — disable SSL verify for corporate proxies/VPNs
const GIT_ENV = { ...process.env, GIT_SSL_NO_VERIFY: "1" };

function cloneTestRepo(url, dir, token) {
  const authedUrl = embedToken(url, token);
  execFileSync("git", ["-c", "http.sslVerify=false", "clone", authedUrl, dir], {
    stdio: "pipe",
    encoding: "utf-8",
    timeout: 60000,
    env: GIT_ENV,
  });
  // Strip token from remote URL + configure identity
  try {
    execSync(`git remote set-url origin ${url}`, { cwd: dir, stdio: "pipe" });
    execSync(`git config user.name "E2E Test"`, { cwd: dir, stdio: "pipe" });
    execSync(`git config user.email "e2e@test.local"`, { cwd: dir, stdio: "pipe" });
    execSync(`git config http.sslVerify false`, { cwd: dir, stdio: "pipe" });
  } catch {}
}

function gitExec(cmd, cwd) {
  return execSync(cmd, { cwd, stdio: "pipe", encoding: "utf-8", env: GIT_ENV }).trim();
}

function telegramPost(method, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      {
        hostname: "api.telegram.org",
        path: `/bot${TELEGRAM_BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(buf));
          } catch {
            resolve({ ok: false, description: buf });
          }
        });
      },
    );
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

// ── Test suite ──────────────────────────────────────────────────────

describe("Local mode E2E", { timeout: 300000 }, () => {
  before(async () => {
    console.log(`\nTest run: ${TEST_ID}`);
    console.log(`Egregore repo: ${TEST_ORG}/${EGREGORE_REPO}`);
    console.log(`Memory repo:   ${TEST_ORG}/${MEMORY_REPO}\n`);

    // Create temp workspaces
    founderWorkspace = path.join(os.tmpdir(), `egregore-founder-${TEST_ID}`);
    joinerWorkspace = path.join(os.tmpdir(), `egregore-joiner-${TEST_ID}`);
    fs.mkdirSync(founderWorkspace, { recursive: true });
    fs.mkdirSync(joinerWorkspace, { recursive: true });
  });

  after(async () => {
    console.log("\nCleaning up...");

    // Delete repos (always, even on failure)
    for (const repo of [EGREGORE_REPO, MEMORY_REPO]) {
      try {
        const deleted = await deleteRepo(FOUNDER_TOKEN, TEST_ORG, repo);
        console.log(`  ${deleted ? "Deleted" : "Could not delete"} ${TEST_ORG}/${repo}`);
      } catch (err) {
        console.log(`  Warning: cleanup of ${repo} failed: ${err.message}`);
      }
    }

    // Remove temp dirs
    for (const dir of [founderWorkspace, joinerWorkspace]) {
      try {
        if (dir && fs.existsSync(dir)) {
          fs.rmSync(dir, { recursive: true, force: true });
          console.log(`  Removed ${path.basename(dir)}`);
        }
      } catch (err) {
        console.log(`  Warning: cleanup of ${path.basename(dir)} failed: ${err.message}`);
      }
    }

    console.log("Cleanup complete.\n");
  });

  // ── Test 1: Founder creates Egregore ────────────────────────────

  it("Founder creates Egregore (--local flow)", { timeout: 60000 }, async () => {
    // 1. Get founder identity
    console.log("  Getting founder identity...");
    const founder = await getUser(FOUNDER_TOKEN);
    founderLogin = founder.login;
    founderName = founder.name;
    founderEmail = founder.email;
    assert.ok(founderLogin, "founder login should exist");
    console.log(`  Founder: ${founderLogin}`);

    // 2. Create egregore repo
    console.log("  Creating egregore repo...");
    await createTestRepo(FOUNDER_TOKEN, TEST_ORG, EGREGORE_REPO, "E2E test egregore");
    assert.ok(await waitForTestRepo(FOUNDER_TOKEN, TEST_ORG, EGREGORE_REPO), "egregore repo should exist");

    // 3. Create memory repo
    console.log("  Creating memory repo...");
    await createTestRepo(FOUNDER_TOKEN, TEST_ORG, MEMORY_REPO, "E2E test memory");
    assert.ok(await waitForTestRepo(FOUNDER_TOKEN, TEST_ORG, MEMORY_REPO), "memory repo should exist");

    // 4. Write egregore.json (NO api_url — local mode signal)
    console.log("  Writing egregore.json...");
    const egreConfig = {
      org_name: "E2E Test Org",
      github_org: TEST_ORG,
      memory_repo: `https://github.com/${TEST_ORG}/${MEMORY_REPO}.git`,
      repos: [],
    };
    await putFile(
      FOUNDER_TOKEN,
      TEST_ORG,
      EGREGORE_REPO,
      "egregore.json",
      JSON.stringify(egreConfig, null, 2) + "\n",
      "Configure egregore for E2E test",
    );

    // Verify: no api_url
    const configRaw = await getFile(FOUNDER_TOKEN, TEST_ORG, EGREGORE_REPO, "egregore.json");
    assert.ok(configRaw, "egregore.json should be readable");
    const config = JSON.parse(configRaw);
    assert.equal(config.org_name, "E2E Test Org");
    assert.equal(config.github_org, TEST_ORG);
    assert.equal(config.api_url, undefined, "api_url must NOT be present (local mode)");

    // 5. Create founder person file (implicitly creates people/ directory)
    console.log("  Creating founder person file...");
    const founderPerson = [
      "---",
      `name: ${founderLogin}`,
      `github: ${founderLogin}`,
      "role: founder",
      `joined: ${TODAY}`,
      "---",
      "",
    ].join("\n");
    await putFile(
      FOUNDER_TOKEN,
      TEST_ORG,
      MEMORY_REPO,
      `people/${founderLogin}.md`,
      founderPerson,
      `Add founder ${founderLogin}`,
    );

    // Small delay for GitHub to settle after commits
    await sleep(2000);

    // Verify person file on GitHub
    const personRaw = await getFile(FOUNDER_TOKEN, TEST_ORG, MEMORY_REPO, `people/${founderLogin}.md`);
    assert.ok(personRaw, "founder person file should exist on GitHub");
    assert.ok(personRaw.includes(`github: ${founderLogin}`), "person file should have correct github field");

    // 6. Clone both repos locally
    console.log("  Cloning repos to founder workspace...");
    founderEgregoreDir = path.join(founderWorkspace, EGREGORE_REPO);
    founderMemoryDir = path.join(founderWorkspace, MEMORY_REPO);

    cloneTestRepo(
      `https://github.com/${TEST_ORG}/${EGREGORE_REPO}.git`,
      founderEgregoreDir,
      FOUNDER_TOKEN,
    );
    cloneTestRepo(
      `https://github.com/${TEST_ORG}/${MEMORY_REPO}.git`,
      founderMemoryDir,
      FOUNDER_TOKEN,
    );

    // 8. Create symlink
    const symlinkTarget = path.join(founderEgregoreDir, "memory");
    const relPath = path.relative(founderEgregoreDir, founderMemoryDir);
    fs.symlinkSync(relPath, symlinkTarget);
    assert.ok(fs.existsSync(symlinkTarget), "memory symlink should exist");
    assert.ok(fs.lstatSync(symlinkTarget).isSymbolicLink(), "memory should be a symlink");

    // 9. Init memory dirs locally (like initMemoryDirs does in the real flow)
    initMemoryDirs(founderMemoryDir);
    assert.ok(fs.existsSync(path.join(founderMemoryDir, "handoffs")), "handoffs/ should exist");
    assert.ok(fs.existsSync(path.join(founderMemoryDir, "knowledge", "decisions")), "knowledge/decisions/ should exist");
    assert.ok(fs.existsSync(path.join(founderMemoryDir, "quests")), "quests/ should exist");
    assert.ok(fs.existsSync(path.join(founderMemoryDir, "wraps")), "wraps/ should exist");

    // 10. Write .env (GITHUB_TOKEN only — no EGREGORE_API_KEY)
    const envContent = `GITHUB_TOKEN=${FOUNDER_TOKEN}\n`;
    fs.writeFileSync(path.join(founderEgregoreDir, ".env"), envContent, { mode: 0o600 });

    const envRead = fs.readFileSync(path.join(founderEgregoreDir, ".env"), "utf-8");
    assert.ok(envRead.includes("GITHUB_TOKEN="), ".env should have GITHUB_TOKEN");
    assert.ok(!envRead.includes("EGREGORE_API_KEY"), ".env must NOT have EGREGORE_API_KEY");

    // 10. Write .egregore-state.json
    const state = {
      github_username: founderLogin,
      github_name: founderName,
      display_name: founderLogin,
      email: founderEmail,
      onboarding_complete: false,
      usage_type: "founder_group",
      org_setup: true,
      github_configured: true,
      workspace_ready: true,
    };
    fs.writeFileSync(
      path.join(founderEgregoreDir, ".egregore-state.json"),
      JSON.stringify(state, null, 2) + "\n",
    );

    const stateRead = JSON.parse(
      fs.readFileSync(path.join(founderEgregoreDir, ".egregore-state.json"), "utf-8"),
    );
    assert.equal(stateRead.github_username, founderLogin);
    assert.equal(stateRead.onboarding_complete, false);
    assert.equal(stateRead.usage_type, "founder_group");

    console.log("  Founder setup complete.");
  });

  // ── Test 2: Founder invites joiner ──────────────────────────────

  it("Founder invites joiner", { timeout: 30000 }, async () => {
    // 1. Get joiner identity
    console.log("  Getting joiner identity...");
    const joiner = await getUser(JOINER_TOKEN);
    joinerLogin = joiner.login;
    joinerName = joiner.name;
    assert.ok(joinerLogin, "joiner login should exist");
    assert.notEqual(joinerLogin, founderLogin, "joiner must be a different user than founder");
    console.log(`  Joiner: ${joinerLogin}`);

    // 2. Add as collaborator on both repos
    console.log("  Adding joiner as collaborator...");
    const addedEgregore = await addCollaborator(FOUNDER_TOKEN, TEST_ORG, EGREGORE_REPO, joinerLogin);
    assert.ok(addedEgregore, "should add joiner to egregore repo");

    const addedMemory = await addCollaborator(FOUNDER_TOKEN, TEST_ORG, MEMORY_REPO, joinerLogin);
    assert.ok(addedMemory, "should add joiner to memory repo");

    // 3. Create joiner person file with welcome note
    console.log("  Creating joiner person file...");
    const joinerPerson = [
      "---",
      `name: ${joinerLogin}`,
      `github: ${joinerLogin}`,
      `invited_by: ${founderLogin}`,
      `joined: ${TODAY}`,
      "---",
      "",
      "E2E test invite — welcome!",
      "",
    ].join("\n");
    await putFile(
      FOUNDER_TOKEN,
      TEST_ORG,
      MEMORY_REPO,
      `people/${joinerLogin}.md`,
      joinerPerson,
      `Invite ${joinerLogin}`,
    );

    // Verify person file on GitHub
    const personRaw = await getFile(FOUNDER_TOKEN, TEST_ORG, MEMORY_REPO, `people/${joinerLogin}.md`);
    assert.ok(personRaw, "joiner person file should exist");
    assert.ok(personRaw.includes(`invited_by: ${founderLogin}`), "should have invited_by field");
    assert.ok(personRaw.includes("E2E test invite"), "should have welcome note");

    // 4. Verify invitations exist
    const { status: invStatus, data: invData } = await ghApi(
      "GET",
      `/repos/${TEST_ORG}/${EGREGORE_REPO}/invitations`,
      FOUNDER_TOKEN,
    );
    assert.equal(invStatus, 200, "should be able to list invitations");
    const joinerInvite = invData.find(
      (inv) => inv.invitee?.login?.toLowerCase() === joinerLogin.toLowerCase(),
    );
    assert.ok(joinerInvite, "joiner should have a pending invitation to egregore repo");

    console.log("  Invite complete.");
  });

  // ── Test 3: Joiner accepts and joins ────────────────────────────

  it("Joiner accepts and joins", { timeout: 60000 }, async () => {
    // 1. Accept pending invitations
    console.log("  Accepting invitations...");
    const { status: invListStatus, data: invitations } = await ghApi(
      "GET",
      "/user/repository_invitations",
      JOINER_TOKEN,
    );
    assert.equal(invListStatus, 200, "should list joiner's invitations");

    const ourInvites = invitations.filter(
      (inv) => inv.repository?.owner?.login?.toLowerCase() === TEST_ORG.toLowerCase()
        && (inv.repository?.name === EGREGORE_REPO || inv.repository?.name === MEMORY_REPO),
    );
    assert.ok(ourInvites.length >= 1, "joiner should have invitations for test repos");

    for (const inv of ourInvites) {
      const { status: acceptStatus } = await ghApi(
        "PATCH",
        `/user/repository_invitations/${inv.id}`,
        JOINER_TOKEN,
      );
      assert.ok(acceptStatus < 300, `should accept invite to ${inv.repository.name}`);
      console.log(`  Accepted invite to ${inv.repository.full_name}`);
    }

    // 2. Verify collaborator status
    await sleep(2000); // GitHub needs a moment
    const { status: collabStatus } = await ghApi(
      "GET",
      `/repos/${TEST_ORG}/${EGREGORE_REPO}/collaborators/${joinerLogin}`,
      FOUNDER_TOKEN,
    );
    assert.equal(collabStatus, 204, "joiner should be a collaborator on egregore repo");

    // 3. Read egregore.json from remote
    console.log("  Reading remote config...");
    const configRaw = await getFile(JOINER_TOKEN, TEST_ORG, EGREGORE_REPO, "egregore.json");
    assert.ok(configRaw, "joiner should be able to read egregore.json");
    const config = JSON.parse(configRaw);
    assert.equal(config.org_name, "E2E Test Org");
    assert.equal(config.github_org, TEST_ORG);
    assert.equal(config.api_url, undefined, "no api_url for local mode");

    // 4. Clone repos to joiner workspace
    console.log("  Cloning repos to joiner workspace...");
    joinerEgregoreDir = path.join(joinerWorkspace, EGREGORE_REPO);
    joinerMemoryDir = path.join(joinerWorkspace, MEMORY_REPO);

    cloneTestRepo(
      `https://github.com/${TEST_ORG}/${EGREGORE_REPO}.git`,
      joinerEgregoreDir,
      JOINER_TOKEN,
    );
    cloneTestRepo(
      `https://github.com/${TEST_ORG}/${MEMORY_REPO}.git`,
      joinerMemoryDir,
      JOINER_TOKEN,
    );

    // 5. Symlink
    const symlinkTarget = path.join(joinerEgregoreDir, "memory");
    const relPath = path.relative(joinerEgregoreDir, joinerMemoryDir);
    fs.symlinkSync(relPath, symlinkTarget);

    // 6. Write joiner's .env
    fs.writeFileSync(
      path.join(joinerEgregoreDir, ".env"),
      `GITHUB_TOKEN=${JOINER_TOKEN}\n`,
      { mode: 0o600 },
    );
    const joinerEnv = fs.readFileSync(path.join(joinerEgregoreDir, ".env"), "utf-8");
    assert.ok(!joinerEnv.includes("EGREGORE_API_KEY"), "joiner .env must not have API key");

    // 7. Write joiner's state
    const joinerState = {
      github_username: joinerLogin,
      github_name: joinerName,
      display_name: joinerLogin,
      email: null,
      onboarding_complete: false,
      usage_type: "joiner_group",
      org_setup: true,
      github_configured: true,
      workspace_ready: true,
    };
    fs.writeFileSync(
      path.join(joinerEgregoreDir, ".egregore-state.json"),
      JSON.stringify(joinerState, null, 2) + "\n",
    );

    // 8. Verify welcome note
    const welcomePath = path.join(joinerMemoryDir, "people", `${joinerLogin}.md`);
    assert.ok(fs.existsSync(welcomePath), "joiner should see their person file locally");
    const welcomeContent = fs.readFileSync(welcomePath, "utf-8");
    assert.ok(welcomeContent.includes(`invited_by: ${founderLogin}`), "should have invited_by");
    assert.ok(welcomeContent.includes("E2E test invite"), "should have welcome note");

    // 9. Cross-visibility: joiner can see founder's person file
    const founderPersonPath = path.join(joinerMemoryDir, "people", `${founderLogin}.md`);
    assert.ok(fs.existsSync(founderPersonPath), "joiner should see founder's person file");

    console.log("  Join complete.");
  });

  // ── Test 4: Both can write and see each other's work ────────────

  it("Both can write and see each other's work", { timeout: 60000 }, async () => {
    // 1. Founder creates a handoff
    console.log("  Founder writing handoff...");
    const handoffDir = path.join(founderMemoryDir, "handoffs", "2026-03");
    fs.mkdirSync(handoffDir, { recursive: true });

    const handoffContent = [
      "---",
      `date: ${TODAY}`,
      `author: ${founderLogin}`,
      `to: ${joinerLogin}`,
      "topic: E2E test handoff",
      "status: pending",
      "---",
      "",
      "# Handoff: E2E Test",
      "",
      "Test handoff from founder to joiner.",
      "",
    ].join("\n");
    const handoffFile = path.join(handoffDir, `${TODAY.replace(/-/g, "").slice(6)}-${founderLogin}-e2e-test.md`);
    fs.writeFileSync(handoffFile, handoffContent);

    // 2. Founder: pull (remote has joiner's person file from Test 2), then commit + push
    console.log("  Founder pushing handoff...");
    gitExec("git pull --rebase origin main", founderMemoryDir);
    gitExec("git add -A", founderMemoryDir);
    gitExec(`git commit -m "Handoff: E2E test from ${founderLogin}"`, founderMemoryDir);
    gitExec("git push origin main", founderMemoryDir);

    // 3. Joiner: git pull
    console.log("  Joiner pulling...");
    gitExec("git pull origin main", joinerMemoryDir);

    // 4. Verify: joiner can read the handoff
    assert.ok(fs.existsSync(handoffFile.replace(founderMemoryDir, joinerMemoryDir)),
      "joiner should see founder's handoff after pull");
    const joinerReadHandoff = fs.readFileSync(
      handoffFile.replace(founderMemoryDir, joinerMemoryDir),
      "utf-8",
    );
    assert.ok(joinerReadHandoff.includes(`to: ${joinerLogin}`), "handoff should be addressed to joiner");

    // 5. Joiner creates a decision
    console.log("  Joiner writing decision...");
    const decisionDir = path.join(joinerMemoryDir, "knowledge", "decisions");
    fs.mkdirSync(decisionDir, { recursive: true });

    const decisionContent = [
      "---",
      `date: ${TODAY}`,
      `author: ${joinerLogin}`,
      "category: decision",
      "topic: e2e-test-decision",
      "---",
      "",
      "# Decision: E2E Test",
      "",
      "Test decision by joiner.",
      "",
    ].join("\n");
    const decisionFile = path.join(decisionDir, `${TODAY}-e2e-test-decision.md`);
    fs.writeFileSync(decisionFile, decisionContent);

    // 6. Joiner: git add + commit + push
    console.log("  Joiner pushing decision...");
    gitExec("git add -A", joinerMemoryDir);
    gitExec(`git commit -m "Decision: E2E test by ${joinerLogin}"`, joinerMemoryDir);
    gitExec("git push origin main", joinerMemoryDir);

    // 7. Founder: git pull
    console.log("  Founder pulling...");
    gitExec("git pull origin main", founderMemoryDir);

    // 8. Verify: founder can read joiner's decision
    const founderReadDecision = fs.readFileSync(
      decisionFile.replace(joinerMemoryDir, founderMemoryDir),
      "utf-8",
    );
    assert.ok(founderReadDecision.includes(`author: ${joinerLogin}`), "decision should be authored by joiner");
    assert.ok(founderReadDecision.includes("category: decision"), "should be categorized as decision");

    console.log("  Collaboration verified.");
  });

  // ── Test 5: Telegram group link in config ───────────────────────

  it("Telegram group link flows from config to joiner", { timeout: 30000 }, async () => {
    // 1. Founder adds telegram_group_link to egregore.json on remote
    console.log("  Adding telegram_group_link to remote config...");
    const configRaw = await getFile(FOUNDER_TOKEN, TEST_ORG, EGREGORE_REPO, "egregore.json");
    const config = JSON.parse(configRaw);
    config.telegram_group_link = "https://t.me/+e2e_test_group";
    await putFile(
      FOUNDER_TOKEN,
      TEST_ORG,
      EGREGORE_REPO,
      "egregore.json",
      JSON.stringify(config, null, 2) + "\n",
      "Add Telegram group link",
    );

    // 2. Verify it's readable from remote
    const updatedRaw = await getFile(JOINER_TOKEN, TEST_ORG, EGREGORE_REPO, "egregore.json");
    assert.ok(updatedRaw, "joiner should read egregore.json");
    const updatedConfig = JSON.parse(updatedRaw);
    assert.equal(updatedConfig.telegram_group_link, "https://t.me/+e2e_test_group",
      "telegram_group_link should be present in remote config for joiner");

    // 3. Verify the localJoinFlow would pick it up (config.telegram_group_link check)
    assert.ok(updatedConfig.telegram_group_link.startsWith("https://t.me/"),
      "telegram_group_link should be a valid Telegram link");

    // 4. Clean up: remove telegram_group_link (so it doesn't affect other tests)
    delete config.telegram_group_link;
    await putFile(
      FOUNDER_TOKEN,
      TEST_ORG,
      EGREGORE_REPO,
      "egregore.json",
      JSON.stringify(config, null, 2) + "\n",
      "Remove Telegram group link (cleanup)",
    );

    console.log("  Telegram group link flow verified.");
  });

  // ── Test 6: Telegram notifications ──────────────────────────────

  it("Telegram group notifications", { timeout: 15000 }, async (t) => {
    if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_GROUP_ID) {
      t.skip("No Telegram env vars — skipping");
      return;
    }

    // 1. Send test message
    console.log("  Sending Telegram test message...");
    const result1 = await telegramPost("sendMessage", {
      chat_id: TELEGRAM_GROUP_ID,
      text: `E2E test: Egregore notification (run ${TEST_ID})`,
    });
    assert.equal(result1.ok, true, "first message should succeed");
    assert.ok(result1.result.message_id, "should have message_id");

    // 2. Send handoff notification
    console.log("  Sending handoff notification...");
    const result2 = await telegramPost("sendMessage", {
      chat_id: TELEGRAM_GROUP_ID,
      text: `\u{1F4CB} ${founderLogin} handed off to ${joinerLogin}: E2E test handoff`,
    });
    assert.equal(result2.ok, true, "handoff notification should succeed");
    assert.ok(result2.result.message_id, "should have message_id");

    console.log("  Telegram notifications verified.");
  });

  // ── Test 7: Config compatible with local mode ───────────────────

  it("Config compatible with local mode (session-start.sh)", { timeout: 5000 }, async () => {
    // 1. Read egregore.json — no api_url
    const configPath = path.join(founderEgregoreDir, "egregore.json");
    assert.ok(fs.existsSync(configPath), "egregore.json should exist locally");
    const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));

    // Simulate session-start.sh detection logic (line 154-158)
    const apiUrl = config.api_url || "";
    assert.equal(apiUrl, "", "api_url should be empty for local mode");

    // 2. Read .env — no EGREGORE_API_KEY
    const envContent = fs.readFileSync(path.join(founderEgregoreDir, ".env"), "utf-8");
    const apiKeyLine = envContent.split("\n").find((l) => l.startsWith("EGREGORE_API_KEY="));
    assert.equal(apiKeyLine, undefined, "no EGREGORE_API_KEY line in .env");

    // 3. Read state file — valid JSON, correct fields
    const statePath = path.join(founderEgregoreDir, ".egregore-state.json");
    assert.ok(fs.existsSync(statePath), ".egregore-state.json should exist");
    const state = JSON.parse(fs.readFileSync(statePath, "utf-8"));
    assert.equal(state.github_username, founderLogin);
    assert.equal(state.onboarding_complete, false);
    assert.equal(state.workspace_ready, true);
    assert.equal(typeof state.display_name, "string");

    // 4. Simulate full local mode detection
    // In session-start.sh: LOCAL_MODE="false"; if [ -z "$API_URL_CONFIGURED" ]; then LOCAL_MODE="true"; fi
    const LOCAL_MODE = !config.api_url;
    assert.equal(LOCAL_MODE, true, "should trigger LOCAL_MODE=true in session-start.sh");

    console.log("  Config compatibility verified.");
  });
});
