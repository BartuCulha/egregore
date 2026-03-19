/**
 * Local mode — setup Egregore using only GitHub (no Egregore API).
 * Zero dependencies — uses helpers from setup.js + node built-ins.
 */

const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { deviceFlow } = require("./auth");
const {
  ghApi,
  acceptPendingInvitations,
  registerInstance,
  installShellAlias,
  embedToken,
  configureGitCredentials,
  initMemoryDirs,
  run,
} = require("./setup");

// Template repo for new egregore instances
// TODO: Change to egregore-ai/egregore when OSS repo exists
const TEMPLATE_OWNER = "Curve-Labs";
const TEMPLATE_REPO = "egregore-core";

// ── GitHub API helpers ──────────────────────────────────────────────

async function getUser(token) {
  const { status, data } = await ghApi("GET", "/user", token);
  if (status !== 200) throw new Error("GitHub auth failed — could not fetch user");
  return { login: data.login, name: data.name || data.login, email: data.email || null };
}

async function listOrgs(token) {
  const { status, data } = await ghApi("GET", "/user/orgs?per_page=100", token);
  if (status !== 200 || !Array.isArray(data)) return [];
  return data.map((o) => ({ login: o.login }));
}

async function repoExists(token, owner, repo) {
  const { status } = await ghApi("GET", `/repos/${owner}/${repo}`, token);
  return status === 200;
}

async function createFromTemplate(token, owner, name, description) {
  const { status, data } = await ghApi(
    "POST",
    `/repos/${TEMPLATE_OWNER}/${TEMPLATE_REPO}/generate`,
    token,
    { owner, name, private: true, description: description || "Egregore instance" },
  );
  if (status !== 201 && status !== 200) {
    const msg = typeof data === "object" ? data.message || JSON.stringify(data) : data;
    throw new Error(`Template generation failed (HTTP ${status}): ${msg}`);
  }
  return data;
}

async function createRepo(token, owner, name, isOrg, description) {
  const apiPath = isOrg ? `/orgs/${owner}/repos` : "/user/repos";
  const { status, data } = await ghApi("POST", apiPath, token, {
    name,
    private: true,
    auto_init: true,
    description: description || "Egregore shared memory",
  });
  if (status === 422) return { already_exists: true };
  if (status !== 201 && status !== 200) {
    const msg = typeof data === "object" ? data.message || JSON.stringify(data) : data;
    throw new Error(`Create repo failed (HTTP ${status}): ${msg}`);
  }
  return data;
}

async function waitForRepo(token, owner, repo, timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await repoExists(token, owner, repo)) return true;
    await new Promise((r) => setTimeout(r, 2000));
  }
  return false;
}

async function getFileContent(token, owner, repo, filePath) {
  const { status, data } = await ghApi("GET", `/repos/${owner}/${repo}/contents/${filePath}`, token);
  if (status !== 200 || !data.content) return null;
  return Buffer.from(data.content, "base64").toString("utf-8");
}

async function putFileContent(token, owner, repo, filePath, content, message) {
  // Get SHA if file already exists
  const { status: existStatus, data: existData } = await ghApi(
    "GET",
    `/repos/${owner}/${repo}/contents/${filePath}`,
    token,
  );
  const body = {
    message,
    content: Buffer.from(content).toString("base64"),
  };
  if (existStatus === 200 && existData.sha) {
    body.sha = existData.sha;
  }
  return ghApi("PUT", `/repos/${owner}/${repo}/contents/${filePath}`, token, body);
}

async function listOrgRepos(token, org) {
  const { status, data } = await ghApi("GET", `/orgs/${org}/repos?per_page=100&sort=updated`, token);
  if (status !== 200 || !Array.isArray(data)) return [];
  return data
    .filter((r) => !r.archived && r.name !== "egregore" && r.name !== "egregore-core" && !r.name.endsWith("-memory"))
    .map((r) => ({ name: r.name, language: r.language || "", description: r.description || "" }));
}

async function addCollaborator(token, owner, repo, username) {
  const { status } = await ghApi(
    "PUT",
    `/repos/${owner}/${repo}/collaborators/${username}`,
    token,
    { permission: "push" },
  );
  return status === 201 || status === 204;
}

// ── Clone + local file helpers ──────────────────────────────────────

function cloneRepo(url, dir, token, ui, label) {
  if (fs.existsSync(dir)) {
    ui.warn(`${path.basename(dir)}/ already exists — pulling latest`);
    run("git pull", { cwd: dir });
  } else {
    execFileSync("git", ["clone", embedToken(url, token), dir], {
      stdio: "pipe",
      encoding: "utf-8",
      timeout: 60000,
    });
    try {
      run(`git remote set-url origin ${url}`, { cwd: dir });
    } catch {}
  }
  try {
    run("git config credential.helper store", { cwd: dir });
  } catch {}
  ui.success(`Cloned ${label}`);
}

function setGitIdentity(dir, user) {
  try {
    run(`git config user.name "${user.name}"`, { cwd: dir });
    run(`git config user.email "${user.login}@users.noreply.github.com"`, { cwd: dir });
  } catch {}
}

function createSymlink(egregoreDir, memoryDir, ui) {
  const target = path.join(egregoreDir, "memory");
  if (fs.existsSync(target)) {
    ui.warn("memory/ symlink already exists");
  } else if (process.platform === "win32") {
    fs.symlinkSync(path.resolve(memoryDir), target, "junction");
  } else {
    fs.symlinkSync(path.relative(egregoreDir, memoryDir), target);
  }
  ui.success("Linked");
}

// ── Flow 1: Founder (local mode) ───────────────────────────────────

async function localFounderFlow(ui) {
  // 1. GitHub auth
  ui.info("Let's set up Egregore. First, sign in with GitHub.\n");
  let githubToken;
  try {
    githubToken = await deviceFlow(ui);
    ui.success("Authenticated with GitHub");
  } catch (err) {
    ui.error(`GitHub auth failed: ${err.message}`);
    process.exit(1);
  }

  // 2. Get user info
  const user = await getUser(githubToken);

  // 3. "What do you want to do?"
  const action = await ui.choose("What do you want to do?", [
    { label: "Start a new project", description: "Create repos and shared memory" },
    { label: "Connect existing repos", description: "Set up Egregore for an org you manage" },
  ]);
  const isNewProject = action.label.startsWith("Start");

  // 4. Detect GitHub org
  console.log("");
  const s = ui.spinner("Checking your GitHub accounts...");
  const orgs = await listOrgs(githubToken);
  s.stop("");

  let githubOrg, isOrg;
  if (orgs.length === 0) {
    // No orgs — use personal account
    githubOrg = user.login;
    isOrg = false;
    ui.info(`Using personal account: ${ui.bold(githubOrg)}`);
  } else if (orgs.length === 1) {
    // One org — use it automatically
    githubOrg = orgs[0].login;
    isOrg = true;
    ui.info(`Using organization: ${ui.bold(githubOrg)}`);
  } else {
    // Multiple orgs — picker
    const orgChoices = orgs.map((o) => ({ label: o.login, description: "Organization" }));
    orgChoices.push({ label: user.login, description: "Personal account" });
    const orgChoice = await ui.choose("Which GitHub account?", orgChoices);
    githubOrg = orgChoice.label;
    isOrg = githubOrg !== user.login;
  }

  // 5. Collect project info
  let orgName, description, selectedRepos = [], newProjectRepo = null;

  if (isNewProject) {
    const nameInput = await ui.prompt("What's your team called?");
    orgName = nameInput || githubOrg;

    const descInput = await ui.prompt("What are you building? (one line)");
    description = descInput || "";

    // Ask about project repo
    console.log("");
    const projectAction = await ui.choose("Do you have a project repo?", [
      { label: "Create a new repo", description: "Start fresh — we'll create it on GitHub" },
      { label: "Connect an existing repo", description: "I already have code" },
      { label: "Skip for now", description: "I'll add projects later" },
    ]);

    if (projectAction.label.startsWith("Create")) {
      const projectName = await ui.prompt("Repo name:");
      if (projectName) {
        newProjectRepo = projectName.trim().toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
      }
    } else if (projectAction.label.startsWith("Connect")) {
      const s0 = ui.spinner("Loading repos...");
      try {
        const repos = await listOrgRepos(githubToken, githubOrg);
        s0.stop("Loaded repos");
        if (repos.length > 0) {
          selectedRepos = await ui.multiSelect("Which repos should Egregore manage?", repos);
        } else {
          ui.info("No repos found in this org yet.");
        }
      } catch {
        s0.stop("Could not load repos");
      }
    }
  } else {
    // "Connect existing repos"
    const nameInput = await ui.prompt(`Display name [${githubOrg}]:`);
    orgName = nameInput || githubOrg;
    description = "";

    // Multi-select repos to manage
    try {
      const repos = await listOrgRepos(githubToken, githubOrg);
      if (repos.length > 0) {
        selectedRepos = await ui.multiSelect("Which repos should Egregore manage?", repos);
      }
    } catch {}
  }

  // 6. Derive repo names from team name
  const slug = orgName
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "") || "egregore";
  const repoName = slug;
  const memoryRepoName = `${slug}-memory`;

  // Guard: if egregore repo already exists with a config, don't overwrite
  console.log("");
  const s1 = ui.spinner("Checking for existing Egregore...");
  const egreExists = await repoExists(githubToken, githubOrg, repoName);
  if (egreExists) {
    const existingConfig = await getFileContent(githubToken, githubOrg, repoName, "egregore.json");
    if (existingConfig) {
      s1.stop(`${githubOrg}/${repoName} already has Egregore set up`);
      console.log("");
      ui.info("To join it:");
      ui.info(`  ${ui.bold(`npx create-egregore join ${githubOrg}/${repoName}`)}`);
      console.log("");
      ui.info("To set up a different one, choose a different team name.");
      process.exit(0);
    }
    s1.stop("Egregore repo exists but has no config — using it");
  } else {
    try {
      await createFromTemplate(githubToken, githubOrg, repoName, description);
      if (!(await waitForRepo(githubToken, githubOrg, repoName))) {
        s1.fail("Repo creation timed out");
        process.exit(1);
      }
      s1.stop("Created egregore repo from template");
    } catch (err) {
      s1.fail("Template not available — creating empty repo");
      try {
        await createRepo(githubToken, githubOrg, repoName, isOrg, description || "Egregore instance");
        if (!(await waitForRepo(githubToken, githubOrg, repoName))) {
          ui.error("Repo creation failed");
          process.exit(1);
        }
        ui.success("Created egregore repo");
      } catch (err2) {
        ui.error(`Could not create repo: ${err2.message}`);
        process.exit(1);
      }
    }
  }

  // 7. Create memory repo
  const s2 = ui.spinner("Creating memory repo...");
  const memExists = await repoExists(githubToken, githubOrg, memoryRepoName);
  if (memExists) {
    s2.stop("Memory repo already exists — using it");
  } else {
    try {
      await createRepo(githubToken, githubOrg, memoryRepoName, isOrg, `${orgName} shared memory`);
      s2.stop("Created memory repo");
    } catch (err) {
      s2.fail(`Memory repo creation failed: ${err.message}`);
      process.exit(1);
    }
  }

  // 7b. Create project repo (if requested)
  if (newProjectRepo) {
    if (newProjectRepo === repoName || newProjectRepo === memoryRepoName) {
      ui.warn(`"${newProjectRepo}" conflicts with Egregore repo names — skipping`);
      newProjectRepo = null;
    } else {
      const s3 = ui.spinner(`Creating ${newProjectRepo}...`);
      const projExists = await repoExists(githubToken, githubOrg, newProjectRepo);
      if (projExists) {
        selectedRepos.push(newProjectRepo);
        s3.stop(`${newProjectRepo} already exists — added to managed repos`);
      } else {
        try {
          await createRepo(githubToken, githubOrg, newProjectRepo, isOrg, description || "");
          if (await waitForRepo(githubToken, githubOrg, newProjectRepo)) {
            selectedRepos.push(newProjectRepo);
            s3.stop(`Created ${newProjectRepo}`);
          } else {
            s3.fail(`${newProjectRepo} creation timed out`);
          }
        } catch (err) {
          s3.fail(`Could not create ${newProjectRepo}: ${err.message}`);
        }
      }
    }
  }

  // 8. Write egregore.json to remote repo (so joiners can read it)
  const egreConfig = {
    org_name: orgName,
    github_org: githubOrg,
    memory_repo: `https://github.com/${githubOrg}/${memoryRepoName}.git`,
    repos: selectedRepos,
  };
  try {
    await putFileContent(
      githubToken,
      githubOrg,
      repoName,
      "egregore.json",
      JSON.stringify(egreConfig, null, 2) + "\n",
      `Configure egregore for ${orgName}`,
    );
  } catch {}

  // 9. Init memory dirs on remote (so joiners get the structure)
  const memDirs = ["people", "handoffs", "knowledge/decisions", "knowledge/patterns", "knowledge/findings", "quests", "wraps"];
  for (const d of memDirs) {
    try {
      await putFileContent(githubToken, githubOrg, memoryRepoName, `${d}/.gitkeep`, "", `Initialize ${d}`);
    } catch {}
  }

  // 10. Clone locally
  const base = process.cwd();
  const egregoreDir = path.join(base, repoName);
  const memoryDir = path.join(base, memoryRepoName);
  const forkUrl = `https://github.com/${githubOrg}/${repoName}.git`;
  const memoryUrl = `https://github.com/${githubOrg}/${memoryRepoName}.git`;

  const totalSteps = 6 + selectedRepos.length;
  configureGitCredentials(githubToken);

  ui.step(1, totalSteps, "Cloning egregore...");
  cloneRepo(forkUrl, egregoreDir, githubToken, ui, "egregore");
  setGitIdentity(egregoreDir, user);

  ui.step(2, totalSteps, "Cloning shared memory...");
  cloneRepo(memoryUrl, memoryDir, githubToken, ui, "memory");
  setGitIdentity(memoryDir, user);

  // Ensure memory dirs exist locally (fallback if remote init failed)
  initMemoryDirs(memoryDir);

  // 11. Symlink
  ui.step(3, totalSteps, "Linking memory...");
  createSymlink(egregoreDir, memoryDir, ui);

  // 12. Write .env (GITHUB_TOKEN only — no API key in local mode)
  ui.step(4, totalSteps, "Writing credentials...");
  fs.writeFileSync(path.join(egregoreDir, ".env"), `GITHUB_TOKEN=${githubToken}\n`, { mode: 0o600 });
  ui.success("Credentials saved");

  // 13. Write egregore.json locally
  fs.writeFileSync(path.join(egregoreDir, "egregore.json"), JSON.stringify(egreConfig, null, 2) + "\n");

  // 14. "What should we call you?" + write state
  console.log("");
  const displayName = await ui.prompt(`What should we call you? [${user.login}]:`);

  const state = {
    github_username: user.login,
    github_name: user.name,
    display_name: displayName || user.login,
    email: user.email,
    onboarding_complete: false,
    usage_type: "founder_group",
    org_setup: true,
    github_configured: true,
    workspace_ready: true,
  };
  fs.writeFileSync(path.join(egregoreDir, ".egregore-state.json"), JSON.stringify(state, null, 2) + "\n");

  // 15. Create founder's person file in memory
  const today = new Date().toISOString().split("T")[0];
  const personContent = [
    "---",
    `name: ${displayName || user.login}`,
    `github: ${user.login}`,
    `role: founder`,
    `joined: ${today}`,
    "---",
    "",
  ].join("\n");
  const personFile = path.join(memoryDir, "people", `${user.login}.md`);
  if (!fs.existsSync(personFile)) {
    fs.writeFileSync(personFile, personContent);
    try {
      run("git add -A", { cwd: memoryDir });
      run(`git commit -m "Add founder ${user.login}"`, { cwd: memoryDir });
      run("git push", { cwd: memoryDir });
    } catch {}
  }

  // 16. Clone managed repos
  for (let i = 0; i < selectedRepos.length; i++) {
    const mr = selectedRepos[i];
    ui.step(5 + i, totalSteps, `Cloning ${mr}...`);
    const repoDir = path.join(base, mr);
    try {
      const repoUrl = `https://github.com/${githubOrg}/${mr}.git`;
      cloneRepo(repoUrl, repoDir, githubToken, ui, mr);
      setGitIdentity(repoDir, user);
    } catch {
      ui.warn(`Could not clone ${mr} — you may not have access.`);
    }
  }

  // 17. Register instance + shell alias
  ui.step(5 + selectedRepos.length, totalSteps, "Registering instance...");
  registerInstance(repoName, orgName, egregoreDir);
  const alias = await installShellAlias(egregoreDir, ui);

  // 18. Telegram group setup (optional)
  console.log("");
  ui.info("Want notifications? Set up a Telegram group:");
  ui.info("");
  ui.info(`  1. Create a Telegram group for your team`);
  ui.info(`  2. Add the bot: ${ui.cyan("https://t.me/egregore_bot")}`);
  ui.info(`  3. Paste the group invite link below`);
  console.log("");
  const telegramLink = await ui.prompt("Group invite link (Enter to skip):");
  if (telegramLink) {
    egreConfig.telegram_group_link = telegramLink;
    // Update local egregore.json
    fs.writeFileSync(path.join(egregoreDir, "egregore.json"), JSON.stringify(egreConfig, null, 2) + "\n");
    // Push to remote so joiners see it
    try {
      await putFileContent(
        githubToken,
        githubOrg,
        repoName,
        "egregore.json",
        JSON.stringify(egreConfig, null, 2) + "\n",
        "Add Telegram group link",
      );
    } catch {}
    ui.success("Telegram group link saved");
  }

  // 19. Optional invite
  console.log("");
  const inviteAnswer = await ui.prompt("Know someone who'd find this useful? [y/N]:");
  if (inviteAnswer && inviteAnswer.toLowerCase() === "y") {
    const invUsername = await ui.prompt("GitHub username:");
    if (invUsername) {
      const invSpin = ui.spinner(`Inviting ${invUsername}...`);
      try {
        // Add as collaborator to all repos
        await addCollaborator(githubToken, githubOrg, repoName, invUsername);
        await addCollaborator(githubToken, githubOrg, memoryRepoName, invUsername);
        for (const mr of selectedRepos) {
          await addCollaborator(githubToken, githubOrg, mr, invUsername).catch(() => {});
        }

        // Create person file
        const invPersonContent = [
          "---",
          `name: ${invUsername}`,
          `github: ${invUsername}`,
          `invited_by: ${user.login}`,
          `joined: ${today}`,
          "---",
          "",
        ].join("\n");
        const invPersonFile = path.join(memoryDir, "people", `${invUsername}.md`);
        if (!fs.existsSync(invPersonFile)) {
          fs.writeFileSync(invPersonFile, invPersonContent);
          try {
            run("git add -A", { cwd: memoryDir });
            run(`git commit -m "Invite ${invUsername}"`, { cwd: memoryDir });
            run("git push", { cwd: memoryDir });
          } catch {}
        }

        invSpin.stop(`Invited ${invUsername}`);
        console.log("");
        ui.info(`Tell them to run:`);
        ui.info(`  ${ui.bold(`npx create-egregore join ${githubOrg}/${repoName}`)}`);
      } catch (err) {
        invSpin.fail(`Could not invite ${invUsername}: ${err.message}`);
      }
    }
  }

  // 19. Done
  console.log("");
  ui.success(`Egregore is ready for ${ui.bold(orgName)}`);
  console.log("");
  ui.info("Your workspace:");
  ui.info(`  ${ui.cyan(`./${repoName}/`)}  — Your Egregore instance`);
  ui.info(`  ${ui.cyan(`./${memoryRepoName}/`)} — Shared knowledge`);
  for (const mr of selectedRepos) {
    ui.info(`  ${ui.cyan(`./${mr}/`)}  — Managed repo`);
  }
  console.log("");
  ui.info(`Next: open a ${ui.bold("new terminal")} and type ${ui.bold(alias.aliasName)} to start.`);
  console.log("");
}

// ── Flow 2: Join (local mode) ──────────────────────────────────────

async function localJoinFlow(orgArg, ui) {
  // Parse org/repo if provided (e.g., "acme-org/ops-team")
  let orgLogin, explicitRepo;
  if (orgArg.includes("/")) {
    [orgLogin, explicitRepo] = orgArg.split("/", 2);
  } else {
    orgLogin = orgArg;
    explicitRepo = null;
  }

  // 1. GitHub auth
  ui.info(`Joining ${ui.bold(orgLogin)}. First, sign in with GitHub.\n`);
  let githubToken;
  try {
    githubToken = await deviceFlow(ui);
    ui.success("Authenticated with GitHub");
  } catch (err) {
    ui.error(`GitHub auth failed: ${err.message}`);
    process.exit(1);
  }

  // 2. Get user info
  const user = await getUser(githubToken);

  // 3. Accept pending invitations
  console.log("");
  const s1 = ui.spinner("Checking for invitations...");
  await acceptPendingInvitations(githubToken, orgLogin, ui);
  s1.stop("Checked invitations");

  // 4. Find egregore repo
  const s2 = ui.spinner(`Looking for egregore in ${orgLogin}...`);
  let repoName = null;
  if (explicitRepo) {
    // User specified org/repo — use it directly
    if (await repoExists(githubToken, orgLogin, explicitRepo)) {
      repoName = explicitRepo;
    }
  } else {
    // Search for known repo names
    for (const candidate of ["egregore", "egregore-core"]) {
      if (await repoExists(githubToken, orgLogin, candidate)) {
        repoName = candidate;
        break;
      }
    }
  }
  if (!repoName) {
    s2.fail(`No egregore repo found in ${orgLogin}${explicitRepo ? `/${explicitRepo}` : ""}`);
    ui.error("Make sure the org admin has set up Egregore and invited you.");
    ui.info("If the repo has a custom name, use: npx create-egregore join <org>/<repo>");
    process.exit(1);
  }

  // 5. Read egregore.json from remote
  const configRaw = await getFileContent(githubToken, orgLogin, repoName, "egregore.json");
  if (!configRaw) {
    s2.fail("Could not read egregore.json");
    ui.error("The egregore repo exists but has no egregore.json. Ask the admin to set up first.");
    process.exit(1);
  }
  let config;
  try {
    config = JSON.parse(configRaw);
  } catch {
    s2.fail("Invalid egregore.json");
    process.exit(1);
  }
  s2.stop(`Found ${ui.bold(config.org_name || orgLogin)}`);

  const orgName = config.org_name || orgLogin;
  const memoryRepoName = config.memory_repo
    ? config.memory_repo.split("/").pop().replace(/\.git$/, "")
    : `${orgLogin}-memory`;
  const managedRepos = config.repos || [];

  // 6. Clone locally
  const base = process.cwd();
  const forkUrl = `https://github.com/${orgLogin}/${repoName}.git`;
  const memoryUrl = config.memory_repo || `https://github.com/${orgLogin}/${memoryRepoName}.git`;
  const egregoreDir = path.join(base, repoName);
  const memoryDir = path.join(base, memoryRepoName);

  const totalSteps = 5 + managedRepos.length;
  configureGitCredentials(githubToken);

  ui.step(1, totalSteps, "Cloning egregore...");
  cloneRepo(forkUrl, egregoreDir, githubToken, ui, "egregore");
  setGitIdentity(egregoreDir, user);

  ui.step(2, totalSteps, "Cloning shared memory...");
  cloneRepo(memoryUrl, memoryDir, githubToken, ui, "memory");
  setGitIdentity(memoryDir, user);

  // 7. Symlink
  ui.step(3, totalSteps, "Linking memory...");
  createSymlink(egregoreDir, memoryDir, ui);

  // 8. Write .env
  ui.step(4, totalSteps, "Writing credentials...");
  fs.writeFileSync(path.join(egregoreDir, ".env"), `GITHUB_TOKEN=${githubToken}\n`, { mode: 0o600 });
  ui.success("Credentials saved");

  // 9. "What should we call you?" + write state
  console.log("");
  const displayName = await ui.prompt(`What should we call you? [${user.login}]:`);

  const state = {
    github_username: user.login,
    github_name: user.name,
    display_name: displayName || user.login,
    email: user.email,
    onboarding_complete: false,
    usage_type: "joiner_group",
    org_setup: true,
    github_configured: true,
    workspace_ready: true,
  };
  fs.writeFileSync(path.join(egregoreDir, ".egregore-state.json"), JSON.stringify(state, null, 2) + "\n");

  // 10. Register + alias
  ui.step(5, totalSteps, "Registering instance...");
  registerInstance(repoName, orgName, egregoreDir);
  const alias = await installShellAlias(egregoreDir, ui);

  // 11. Clone managed repos
  for (let i = 0; i < managedRepos.length; i++) {
    const mr = managedRepos[i];
    ui.step(6 + i, totalSteps, `Cloning ${mr}...`);
    const repoDir = path.join(base, mr);
    try {
      const repoUrl = `https://github.com/${orgLogin}/${mr}.git`;
      cloneRepo(repoUrl, repoDir, githubToken, ui, mr);
      setGitIdentity(repoDir, user);
    } catch {
      ui.warn(`Could not clone ${mr} — you may not have access yet.`);
    }
  }

  // 12. Show Telegram group link if available
  if (config.telegram_group_link) {
    console.log("");
    ui.info("Join the team's Telegram group for notifications:");
    ui.info(`  ${ui.cyan(config.telegram_group_link)}`);
  }

  // 13. Check for welcome note
  const welcomeFile = path.join(memoryDir, "people", `${user.login}.md`);
  if (fs.existsSync(welcomeFile)) {
    try {
      const content = fs.readFileSync(welcomeFile, "utf-8");
      const inviterMatch = content.match(/invited_by:\s*(\S+)/);
      // Check for content after the frontmatter closing ---
      const bodyMatch = content.match(/^---[\s\S]*?---\s*\n([\s\S]+)/m);
      if (inviterMatch) {
        console.log("");
        const note = bodyMatch && bodyMatch[1].trim() ? ` — "${bodyMatch[1].trim()}"` : "";
        ui.info(`${inviterMatch[1]} invited you${note}`);
      }
    } catch {}
  }

  // 14. Done
  console.log("");
  ui.success(`Joined ${ui.bold(orgName)}`);
  console.log("");
  ui.info("Your workspace:");
  ui.info(`  ${ui.cyan(`./${repoName}/`)}  — Egregore instance`);
  ui.info(`  ${ui.cyan(`./${memoryRepoName}/`)} — Shared knowledge`);
  for (const mr of managedRepos) {
    ui.info(`  ${ui.cyan(`./${mr}/`)}  — Managed repo`);
  }
  console.log("");
  ui.info(`Next: open a ${ui.bold("new terminal")} and type ${ui.bold(alias.aliasName)} to start.`);
  console.log("");
}

module.exports = { localFounderFlow, localJoinFlow, addCollaborator, getUser, repoExists, createRepo, waitForRepo };
