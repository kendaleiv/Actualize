import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { childEnvironment, prepare, preserveWorkspaces, sourceIdentity } from "./run.mjs";

test("paid execution requires explicit confirmation before setup or model calls", () => {
  for (const mode of ["smoke", "full", "workflows"]) {
    const result = spawnSync(process.execPath, [fileURLToPath(new URL("./run.mjs", import.meta.url)), mode],
      { encoding: "utf8", env: { ...process.env, COPILOT_GITHUB_TOKEN: "must-not-be-printed" } });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /Pass --paid explicitly/);
    assert.doesNotMatch(result.stdout + result.stderr, /must-not-be-printed/);
  }
});

test("only the chosen model credential can reach a paid Vally process", () => {
  const host = { PATH: "tools", COPILOT_GITHUB_TOKEN: "chosen", GH_TOKEN: "other",
    GITHUB_TOKEN: "other", AZURE_CLIENT_SECRET: "secret", DOCKER_HOST: "tcp://remote:2375",
    COPILOT_PROVIDER_API_KEY: "other", HTTPS_PROXY: "http://host-proxy",
    EVALUATE_USE_HOST_COPILOT_HOME: "1" };
  const free = childEnvironment(host);
  const paid = childEnvironment(host, true);
  assert.equal(free.COPILOT_GITHUB_TOKEN, undefined);
  assert.equal(paid.COPILOT_GITHUB_TOKEN, "chosen");
  for (const key of ["GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "DOCKER_HOST",
    "COPILOT_PROVIDER_API_KEY", "HTTPS_PROXY", "EVALUATE_USE_HOST_COPILOT_HOME"]) {
    assert.equal(paid[key], undefined);
  }
});

test("unchanged Docker workspaces are retained without rewriting native results", async (t) => {
  const source = await mkdtemp(join(tmpdir(), "vally-docker-retention-test-"));
  const output = await mkdtemp(join(tmpdir(), "actualize-eval-retention-"));
  t.after(() => rm(source, { recursive: true, force: true }));
  t.after(() => rm(output, { recursive: true, force: true }));
  await writeFile(join(source, "input.json"), '{"actual":12}');
  const directory = join(output, "vally", "run");
  await mkdir(directory, { recursive: true });
  const record = { type: "trial-result", status: "success", itemId: "trial",
    trajectory: { workDir: source } };
  const original = JSON.stringify(record) + "\n";
  await writeFile(join(directory, "results.jsonl"), original);
  const replay = await preserveWorkspaces(output, join(output, "retained"));
  const saved = JSON.parse((await readFile(replay, "utf8")).trim());
  assert.equal(await readFile(join(saved.workspacePath, "input.json"), "utf8"), '{"actual":12}');
  assert.equal(await readFile(join(directory, "results.jsonl"), "utf8"), original);
  assert.equal(await preserveWorkspaces(output, join(output, "retained")), replay);
});

test("exported workspace retention refuses paths outside the host temporary directory", async (t) => {
  const source = await mkdtemp(join(dirname(fileURLToPath(import.meta.url)), "vally-docker-retention-test-"));
  const output = await mkdtemp(join(tmpdir(), "actualize-eval-retention-"));
  t.after(() => rm(source, { recursive: true, force: true }));
  t.after(() => rm(output, { recursive: true, force: true }));
  const directory = join(output, "vally", "run");
  await mkdir(directory, { recursive: true });
  await writeFile(join(directory, "results.jsonl"), JSON.stringify({
    type: "trial-result", status: "success", itemId: "trial", trajectory: { workDir: source },
  }) + "\n");
  await assert.rejects(preserveWorkspaces(output, join(output, "retained")), /workspace is unavailable/);
});

test("source provenance ignores generated Python caches but detects changed sources", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "actualize-eval-sources-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const file of ["SKILL.md", "scripts/actualize.py", "scripts/fetch_retail.py",
    "package.json", "package-lock.json", ".vally.yaml", ".dockerignore", "samples/input.json", "evals/grader.py"]) {
    await mkdir(dirname(join(root, file)), { recursive: true });
    await writeFile(join(root, file), "source");
  }
  const before = await sourceIdentity(root);
  await mkdir(join(root, "evals", "__pycache__"));
  await writeFile(join(root, "evals", "__pycache__", "grader.pyc"), "cache");
  await writeFile(join(root, "evals", "grader.pyo"), "cache");
  assert.deepEqual(await sourceIdentity(root), before);
  await writeFile(join(root, "evals", "grader.py"), "changed");
  assert.notDeepEqual(await sourceIdentity(root), before);
});

test("concurrent snapshots preserve the copied bytes, task staging and hidden grading", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "actualize-eval-snapshots-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const base = join(directory, "source");
  for (const file of ["SKILL.md", "scripts/actualize.py", "scripts/fetch_retail.py",
    "package.json", "package-lock.json", ".vally.yaml", ".dockerignore", "samples/input.json",
    "evals/expected/cases.json", "evals/fixtures/input.json", "evals/graders/check_actualize.py",
    "evals/actualize/eval.yaml", "evals/docker/check.yaml"]) {
    await mkdir(dirname(join(base, file)), { recursive: true });
    await writeFile(join(base, file), "original");
  }
  const [first, concurrent] = await Promise.all([
    prepare({ base, directory: join(directory, "first") }),
    prepare({ base, directory: join(directory, "concurrent") }),
  ]);
  assert.notEqual(first.skills, concurrent.skills);
  assert.notEqual(first.grading, concurrent.grading);
  assert.deepEqual(first.sources, concurrent.sources);
  await writeFile(join(base, "SKILL.md"), "edited during a build");
  await writeFile(join(base, "evals", "graders", "check_actualize.py"), "new grader");
  const later = await prepare({ base, directory: join(directory, "later") });
  assert.notDeepEqual(first.sources, later.sources);
  for (const snapshot of [first, concurrent]) {
    assert.deepEqual(await sourceIdentity(snapshot.root), snapshot.sources);
    assert.equal(await readFile(join(snapshot.skills, "actualize", "SKILL.md"), "utf8"), "original");
    assert.equal(await readFile(join(snapshot.grading, "check_actualize.py"), "utf8"), "original");
  }
  assert.equal(await readFile(join(later.skills, "actualize", "SKILL.md"), "utf8"), "edited during a build");
  await assert.rejects(prepare({ base, directory: first.root }), { code: "EEXIST" });
});
