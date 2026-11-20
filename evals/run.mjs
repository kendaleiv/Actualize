import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { createReadStream, existsSync } from "node:fs";
import { copyFile, cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, delimiter } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cli = join(root, "node_modules", "@microsoft", "vally-cli", "dist", "index.js");
const imageTag = "actualize-vally:0.21.0";
const sourceFiles = ["SKILL.md", "scripts/actualize.py", "scripts/fetch_retail.py"];
const sha = (data) => createHash("sha256").update(data).digest("hex");

export function childEnvironment(host = process.env, paid = false) {
  const allowed = ["PATH", "Path", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "DOCKER_CONFIG", "DOCKER_CONTEXT"];
  const env = Object.fromEntries(allowed.filter((key) => host[key] !== undefined).map((key) => [key, host[key]]));
  const bin = host.LOCALAPPDATA && join(host.LOCALAPPDATA, "Programs", "DockerDesktop", "resources", "bin");
  const path = env.PATH ?? env.Path ?? "";
  delete env.Path;
  env.PATH = bin && existsSync(bin) ? `${bin}${delimiter}${path}` : path;
  env.VALLY_TELEMETRY_OPTOUT = "1";
  env.COMPOSE_DISABLE_ENV_FILE = "true";
  env.PYTHONDONTWRITEBYTECODE = "1";
  if (paid) env.COPILOT_GITHUB_TOKEN = host.COPILOT_GITHUB_TOKEN;
  return env;
}

async function run(command, args, env, { capture = false, input, allowed = [0] } = {}) {
  return new Promise((accept, reject) => {
    const child = spawn(command, args, {
      cwd: root, env, windowsHide: true,
      stdio: [input ? "pipe" : "ignore", capture ? "pipe" : "inherit", "inherit"],
    });
    let output = "";
    if (capture) child.stdout.setEncoding("utf8").on("data", (chunk) => { output += chunk; });
    if (input) {
      const stream = createReadStream(input);
      stream.on("error", reject);
      child.stdin.on("error", (error) => { if (error.code !== "EPIPE") reject(error); });
      stream.pipe(child.stdin);
    }
    child.on("error", reject);
    child.on("close", (code) => {
      if (allowed.includes(code)) accept({ code, output: output.trim() });
      else reject(new Error(`${command} exited with ${code ?? "a signal"}.`));
    });
  });
}

const vally = (args, env, options) => run(process.execPath, [cli, ...args], env, options);

export async function prepare({ base = root, directory = join(root, ".eval-tools", randomUUID()) } = {}) {
  const sources = await sourceIdentity(base);
  await mkdir(dirname(directory), { recursive: true });
  await mkdir(directory);
  for (const [file, digest] of Object.entries(sources)) {
    const data = await readFile(join(base, file));
    if (sha(data) !== digest) throw new Error(`Evaluation source changed while snapshotting: ${file}`);
    await mkdir(dirname(join(directory, file)), { recursive: true });
    await writeFile(join(directory, file), data);
  }
  if (JSON.stringify(await sourceIdentity(directory)) !== JSON.stringify(sources)) {
    throw new Error("The evaluation snapshot does not match its recorded sources.");
  }
  const skill = join(directory, ".eval-tools", "skills", "actualize");
  const grading = join(directory, ".eval-tools", "grading");
  await mkdir(join(skill, "scripts"), { recursive: true });
  const integrity = {};
  for (const file of sourceFiles) {
    await copyFile(join(directory, file), join(skill, file));
    if (file !== "SKILL.md") integrity[file] = sources[file];
  }
  await cp(join(directory, "evals", "expected"), join(grading, "expected"), { recursive: true });
  await cp(join(directory, "samples"), join(grading, "expected", "inputs", "samples"), { recursive: true });
  await cp(join(directory, "evals", "fixtures"), join(grading, "expected", "inputs", "fixtures"), { recursive: true });
  await copyFile(join(directory, "evals", "graders", "check_actualize.py"), join(grading, "check_actualize.py"));
  await writeFile(join(grading, "expected", "integrity.json"), `${JSON.stringify(integrity, null, 2)}\n`);
  return { root: directory, sources, skills: dirname(skill), grading,
    spec: join(directory, "evals", "actualize", "eval.yaml"),
    checkSpec: join(directory, "evals", "docker", "check.yaml") };
}

export async function sourceIdentity(base = root) {
  const identity = {};
  async function add(path) {
    for (const entry of await readdir(join(base, path), { withFileTypes: true })) {
      if (entry.name === "__pycache__" || /\.py[co]$/.test(entry.name)) continue;
      const file = `${path}/${entry.name}`;
      if (entry.isDirectory()) await add(file);
      else if (entry.isFile()) identity[file] = sha(await readFile(join(base, file)));
      else throw new Error(`Evaluation inputs must not be symlinks: ${file}`);
    }
  }
  for (const file of [...sourceFiles, "package.json", "package-lock.json", ".vally.yaml", ".dockerignore"]) {
    identity[file] = sha(await readFile(join(base, file)));
  }
  for (const path of ["samples", "evals"]) await add(path);
  return Object.fromEntries(Object.entries(identity).sort(([a], [b]) => a.localeCompare(b)));
}

async function buildImage(env, snapshot) {
  const context = JSON.parse((await run("docker", ["context", "inspect"], env, { capture: true })).output)[0];
  if (!/^(npipe|unix):\/\//.test(context.Endpoints.docker.Host)) {
    throw new Error("Evaluations require a local Docker context.");
  }
  const engine = JSON.parse((await run("docker", ["info", "--format",
    '{"version":"{{.ServerVersion}}","os":"{{.OSType}}"}'], env, { capture: true })).output);
  if (engine.os !== "linux" || Number(engine.version.split(".")[0]) < 28) {
    throw new Error("A Linux Docker engine version 28 or newer is required for isolated bridge networking.");
  }
  await run("docker", ["compose", "version"], env);
  const imageFile = join(snapshot.root, "image-id");
  const buildTag = `${imageTag}-${randomUUID()}`;
  const args = ["build", "-f", join(snapshot.root, "evals", "docker", "Dockerfile"),
    "--iidfile", imageFile, "-t", buildTag];
  if (process.env.ACTUALIZE_EVAL_NPM_REGISTRY) {
    const registry = new URL(process.env.ACTUALIZE_EVAL_NPM_REGISTRY);
    if (registry.protocol !== "https:" || registry.username || registry.password || registry.search || registry.hash) {
      throw new Error("The build registry must be an HTTPS URL without credentials, query or fragment.");
    }
    args.push("--build-arg", `NPM_REGISTRY=${registry.href}`);
  }
  await run("docker", [...args, snapshot.root], env);
  const image = (await readFile(imageFile, "utf8")).trim();
  const probe = `
    import { CopilotClient } from "@github/copilot-sdk";
    import { execFileSync } from "node:child_process";
    import { readFileSync } from "node:fs";
    const timer = setTimeout(() => { console.error("Copilot runtime startup timed out"); process.exit(1); }, 20000);
    const client = new CopilotClient({ env: { ...process.env, COPILOT_HOME: "/tmp/copilot-control" } });
    try {
      await client.start();
      const status = await client.getStatus();
      const discovered = await client.rpc.skills.discover({ skillDirectories: ["/skills"] });
      const skill = discovered.skills.find((entry) => entry.name === "actualize" && entry.enabled);
      if (!skill || discovered.errors?.length) {
        throw new Error("Actualize skill discovery failed: " + (discovered.errors ?? []).join("; "));
      }
      console.log(JSON.stringify({
        node: process.version, python: execFileSync("python3", ["--version"], { encoding: "utf8" }).trim(),
        copilotCLI: status.version, copilotSDK: JSON.parse(readFileSync("node_modules/@github/copilot-sdk/package.json", "utf8")).version,
        skill: { name: skill.name, descriptionCharacters: skill.description.length }
      }));
    } finally { await client.stop(); clearTimeout(timer); }
  `;
  const tools = JSON.parse((await run("docker", ["run", "--rm", "--network", "none",
    "--workdir", "/opt/actualize-evals",
    "--mount", `type=bind,source=${snapshot.skills},target=/skills,readonly`,
    "--entrypoint", "node", image,
    "--input-type=module", "-e", probe], env, { capture: true })).output);
  return { image, imageTag: buildTag, engine, context: context.Name, tools };
}

async function withNetwork(env, image, snapshot, action) {
  const project = `actualize-evals-${randomUUID().replaceAll("-", "")}`;
  const compose = ["compose", "-p", project, "-f", join(snapshot.root, "evals", "docker", "compose.yaml")];
  const networkEnv = { ...env, ACTUALIZE_EVAL_IMAGE: image };
  try {
    await run("docker", [...compose, "up", "-d", "--wait", "egress"], networkEnv);
    const network = `${project}_task`;
    const config = JSON.parse((await run("docker", ["network", "inspect", network], env, { capture: true })).output)[0];
    if (!config.Internal || config.Options["com.docker.network.bridge.gateway_mode_ipv4"] !== "isolated") {
      throw new Error("The task bridge is not isolated; refusing agent execution.");
    }
    await run("docker", ["run", "--rm", "--network", network, "--dns", "127.0.0.1",
      "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
      "--mount", `type=bind,source=${join(snapshot.root, "evals", "docker", "check_network.py")},target=/check_network.py,readonly`,
      "--entrypoint", "python3", image, "/check_network.py"], env);
    return await action(network);
  } finally {
    await run("docker", [...compose, "down", "--volumes"], networkEnv);
  }
}

async function resultsFile(output) {
  const directories = (await readdir(output, { withFileTypes: true })).filter((entry) => entry.isDirectory());
  if (directories.length !== 1) throw new Error(`Expected one Vally result directory in ${output}.`);
  return join(output, directories[0].name, "results.jsonl");
}

async function records(file) {
  return (await readFile(file, "utf8")).split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

async function doctor(env, image, snapshot, id) {
  const output = join(root, "vally-results", `doctor-${id}`);
  const backend = ["--backend", "docker", "--backend-args", `task-image=${image}`,
    "--backend-args", `grade-image=${image}`, "--backend-args", "grade-network=none"];
  await withNetwork(env, image, snapshot, async (network) => {
    backend.push("--backend-args", `task-network=${network}`);
    for (const control of ["pass", "fail"]) {
      const directory = join(output, control);
      const result = await vally(["eval", "-e", snapshot.checkSpec, ...backend, "--tag", `control=${control}`,
        "--workers", "1", "--max-retries", "0", "--require-pass", "--output-dir", directory,
        "--workspace", join(root, ".eval-workspaces", `doctor-${id}`, control)], {
        ...env, ACTUALIZE_HOST_SENTINEL: "must-not-reach-containers",
        GH_TOKEN: "must-not-reach-containers",
        HTTPS_PROXY: "http://egress:3128", HTTP_PROXY: "http://egress:3128", NODE_USE_ENV_PROXY: "1",
      }, { allowed: [0, 1] });
      const file = await resultsFile(directory);
      const all = await records(file);
      const trials = all.filter((record) => record.type === "trial-result");
      const summary = all.find((record) => record.type === "run-summary");
      const passing = control === "pass";
      if (trials.length !== 1 || trials[0].status !== "success" || summary?.hadExecutionErrors
          || summary?.hadGradingErrors || summary?.passed !== passing
          || trials[0].gradeResult?.passed !== passing || result.code !== (passing ? 0 : 1)) {
        throw new Error(`The ${control} control did not produce the expected valid verdict: ${file}`);
      }
      if (passing) {
        const regrade = await vally(["grade", "-e", snapshot.checkSpec, ...backend,
          "--require-pass", "--run-dir", dirname(file), "--output", "jsonl"], env,
        { input: file, capture: true });
        await writeFile(join(output, "regrade.jsonl"), `${regrade.output}\n`);
      }
    }
  });
  console.log(`Free Docker checks passed. Evidence: ${output}`);
}

async function paidEval(mode, env, runtime, snapshot, id) {
  const output = join(root, "vally-results", id);
  await mkdir(output, { recursive: true });
  const model = process.env.ACTUALIZE_EVAL_MODEL || "gpt-5.3-codex";
  const reasoning = process.env.ACTUALIZE_EVAL_REASONING || "low";
  const provenance = { runId: id, suite: mode, model, reasoning, runs: mode === "smoke" ? 1 : 3,
    workers: 1, retries: 0, tokenVerdictLimit: 200000, hardTokenLimit: false,
    node: process.version, vally: "0.21.0", ...runtime,
    revision: snapshot.revision, dirty: snapshot.dirty,
    sourceSnapshot: "snapshot", sources: snapshot.sources };
  await writeFile(join(output, "provenance.json"), `${JSON.stringify(provenance, null, 2)}\n`);
  return withNetwork(env, runtime.image, snapshot, async (network) => {
    const result = await vally(["eval", "--eval-spec", snapshot.spec,
      ...(mode !== "full" ? ["--tag", `suite=${mode}`] : []), "--model", model,
      "--reasoning-effort", reasoning, "--runs", String(provenance.runs),
      "--workers", "1", "--max-retries", "0", "--require-pass", "--run-id", id,
      "--output-dir", join(output, "vally"), "--workspace", join(root, ".eval-workspaces", id),
      "--backend", "docker", "--backend-args", `task-image=${runtime.image}`,
      "--backend-args", `grade-image=${runtime.image}`, "--backend-args", `task-network=${network}`,
      "--backend-args", "grade-network=none"], {
      ...env, HTTP_PROXY: "http://egress:3128", HTTPS_PROXY: "http://egress:3128", NODE_USE_ENV_PROXY: "1",
    }, { allowed: [0, 1] });
    await preserveWorkspaces(output, join(root, ".eval-workspaces", id, "retained"));
    console.log(`Run evidence and provenance: ${output}`);
    return result.code;
  });
}

export async function preserveWorkspaces(output, destination) {
  const file = await resultsFile(join(output, "vally"));
  const replay = join(dirname(file), "replay-results.jsonl");
  if (existsSync(replay)) return replay;
  const rows = await records(file);
  let changed = false;
  for (const row of rows) {
    if (row.type !== "trial-result" || row.status !== "success"
        || (row.workspacePath && existsSync(row.workspacePath))) continue;
    const source = row.trajectory?.workDir && resolve(row.trajectory.workDir);
    if (!source || !existsSync(source) || relative(tmpdir(), source).startsWith("..")
        || isAbsolute(relative(tmpdir(), source))
        || !basename(source).startsWith("vally-docker-")) {
      throw new Error(`Completed trial workspace is unavailable: ${row.itemId}. No retry was launched.`);
    }
    const target = join(destination, sha(row.itemId).slice(0, 24));
    await mkdir(dirname(target), { recursive: true });
    await cp(source, target, { recursive: true, dereference: false, errorOnExist: true, force: false });
    row.workspacePath = target;
    changed = true;
  }
  if (!changed) return file;
  await writeFile(replay, `${rows.map((row) => JSON.stringify(row)).join("\n")}\n`, { flag: "wx" });
  return replay;
}

async function regrade(directory, env, runtime, snapshot, outputDirectory, currentGraders) {
  const output = resolve(directory);
  const provenance = JSON.parse(await readFile(join(output, "provenance.json"), "utf8"));
  const file = await preserveWorkspaces(output, join(outputDirectory, "retained-workspaces"));
  for (const record of await records(file)) {
    if (record.type === "trial-result" && (!record.workspacePath || !existsSync(record.workspacePath))) {
      throw new Error("Program regrading requires every retained .eval-workspaces directory from the original run.");
    }
  }
  const result = await vally(["grade", "-e", snapshot.spec, "--backend", "docker",
    "--backend-args", `grade-image=${runtime.image}`, "--backend-args", "grade-network=none",
    "--require-pass", "--run-dir", dirname(file), "--output", "jsonl"], env,
  { input: file, capture: true, allowed: [0, 1] });
  await writeFile(join(outputDirectory, "results.jsonl"), `${result.output}\n`);
  const changedSources = [...new Set([...Object.keys(provenance.sources), ...Object.keys(snapshot.sources)])]
    .filter((file) => provenance.sources[file] !== snapshot.sources[file]);
  await writeFile(join(outputDirectory, "provenance.json"), `${JSON.stringify({
    originalRunId: provenance.runId, currentGraders, changedSources, ...runtime,
    sourceSnapshot: "snapshot", sources: snapshot.sources, modelCalls: 0, exitCode: result.code,
  }, null, 2)}\n`);
  console.log(`Regrading evidence: ${outputDirectory}`);
  return result.code;
}

export async function main(args = process.argv.slice(2)) {
  const [mode, ...rest] = args;
  if (!["lint", "check", "doctor", "smoke", "full", "workflows", "regrade"].includes(mode)) {
    throw new Error("Use lint, check, doctor, smoke --paid, full --paid, workflows --paid, or regrade <saved-run-directory>.");
  }
  const paid = ["smoke", "full", "workflows"].includes(mode);
  if (paid && (rest.length !== 1 || rest[0] !== "--paid")) {
    throw new Error("Agent evaluation is paid and opt-in. Pass --paid explicitly; no model calls were made.");
  }
  const currentGraders = mode === "regrade" && rest[1] === "--current-graders";
  if (!paid && (mode === "regrade" ? !rest[0] || rest.length !== (currentGraders ? 2 : 1) : rest.length !== 0)) {
    throw new Error("Unexpected arguments.");
  }
  if (paid && (!process.env.COPILOT_GITHUB_TOKEN || /[\s\x00-\x1f]/.test(process.env.COPILOT_GITHUB_TOKEN))) {
    throw new Error("Set COPILOT_GITHUB_TOKEN to a single-line Copilot-enabled token. Host logins are not reused.");
  }
  const env = childEnvironment(process.env, paid);
  const id = randomUUID();
  const directory = paid ? join(root, "vally-results", id)
    : mode === "doctor" ? join(root, "vally-results", `doctor-${id}`)
      : mode === "regrade" ? join(resolve(rest[0]), "regrades", id)
        : join(root, ".eval-tools", id);
  await mkdir(directory, { recursive: true });
  try {
    const snapshot = await prepare({ directory: join(directory, "snapshot") });
    if (paid) {
      snapshot.revision = (await run("git", ["rev-parse", "HEAD"], env, { capture: true })).output;
      snapshot.dirty = Boolean((await run("git", ["status", "--porcelain"], env, { capture: true })).output);
    }
    await vally(["lint", "--eval-spec", snapshot.spec, "--strict"], env);
    if (mode === "lint") return 0;
    if (mode === "check") {
      await run("python", ["-m", "unittest", "discover", "-s", "tests"], env);
      await run(process.execPath, ["--test", "evals/run.test.mjs"], env);
      return 0;
    }
    if (mode === "regrade" && !currentGraders) {
      const provenance = JSON.parse(await readFile(join(resolve(rest[0]), "provenance.json"), "utf8"));
      if (JSON.stringify(provenance.sources) !== JSON.stringify(snapshot.sources)) {
        throw new Error("The evaluation sources differ from this saved run. Use --current-graders for an intentional grader change.");
      }
    }
    const runtime = await buildImage(env, snapshot);
    if (mode === "doctor") {
      await doctor(env, runtime.image, snapshot, id);
      return 0;
    }
    if (mode === "regrade") return regrade(rest[0], env, runtime, snapshot, directory, currentGraders);
    return paidEval(mode, env, runtime, snapshot, id);
  } finally {
    if (mode === "lint" || mode === "check") await rm(directory, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then((code) => { process.exitCode = code; }).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
