const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SUBMIT_TIMEOUT_SECONDS = Number(process.env.BREAKTWENTY_NOTARY_SUBMIT_TIMEOUT_SECONDS || 600);
const POLL_TIMEOUT_SECONDS = Number(process.env.BREAKTWENTY_NOTARY_POLL_TIMEOUT_SECONDS || 1200);
const POLL_INTERVAL_SECONDS = Number(process.env.BREAKTWENTY_NOTARY_POLL_INTERVAL_SECONDS || 30);

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function requireEnv(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${name} is required for macOS notarization.`);
  }
  return value;
}

function redact(value, secrets) {
  let redacted = String(value || "");
  for (const secret of secrets) {
    if (secret) {
      redacted = redacted.split(secret).join("***");
    }
  }
  return redacted;
}

function parseJsonOutput(output, label, secrets) {
  try {
    return JSON.parse(output.trim());
  } catch (error) {
    throw new Error(`${label} returned non-JSON output:\n${redact(output, secrets)}`);
  }
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = Number(bytes || 0);
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function summarizeTree(rootPath) {
  const summary = { files: 0, dirs: 0, bytes: 0 };
  const stack = [rootPath];
  while (stack.length > 0) {
    const current = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_error) {
      continue;
    }
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        summary.dirs += 1;
        stack.push(entryPath);
      } else if (entry.isFile()) {
        summary.files += 1;
        try {
          summary.bytes += fs.statSync(entryPath).size;
        } catch (_error) {
          // Ignore files that disappear while summarizing.
        }
      }
    }
  }
  return summary;
}

function logTreeSummary(label, targetPath) {
  const summary = summarizeTree(targetPath);
  console.log(
    `[notary] ${label}: ${formatBytes(summary.bytes)}, ${summary.files} files, ${summary.dirs} directories`,
  );
}

function logChildBreakdown(label, targetPath) {
  let entries;
  try {
    entries = fs.readdirSync(targetPath, { withFileTypes: true });
  } catch (_error) {
    return;
  }
  const children = entries.map((entry) => {
    const entryPath = path.join(targetPath, entry.name);
    if (entry.isDirectory()) {
      return { name: `${entry.name}/`, ...summarizeTree(entryPath) };
    }
    if (entry.isFile()) {
      let bytes = 0;
      try {
        bytes = fs.statSync(entryPath).size;
      } catch (_error) {
        bytes = 0;
      }
      return { name: entry.name, files: 1, dirs: 0, bytes };
    }
    return { name: entry.name, files: 0, dirs: 0, bytes: 0 };
  }).sort((left, right) => right.bytes - left.bytes).slice(0, 12);
  console.log(`[notary] Largest ${label} entries:`);
  for (const child of children) {
    console.log(
      `[notary]   ${child.name}: ${formatBytes(child.bytes)}, ${child.files} files, ${child.dirs} directories`,
    );
  }
}

function runCommand(label, command, args, options = {}) {
  const {
    cwd,
    secrets = [],
    timeoutSeconds = 300,
  } = options;
  return new Promise((resolve, reject) => {
    console.log(`[notary] ${label}`);
    const child = spawn(command, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => {
        if (!child.killed) {
          child.kill("SIGKILL");
        }
      }, 5000).unref();
    }, Math.max(timeoutSeconds, 1) * 1000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      const output = `${stdout}${stderr}`;
      if (timedOut) {
        reject(new Error(`${label} timed out after ${timeoutSeconds}s.`));
        return;
      }
      if (code !== 0) {
        reject(
          new Error(
            `${label} failed with code ${code ?? "none"} signal ${signal ?? "none"}:\n${redact(output, secrets)}`,
          ),
        );
        return;
      }
      resolve(output);
    });
  });
}

async function notarizeApp(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }

  const keyPath = requireEnv("APPLE_API_KEY");
  const keyId = requireEnv("APPLE_API_KEY_ID");
  const issuer = requireEnv("APPLE_API_ISSUER");
  if (!fs.existsSync(keyPath)) {
    throw new Error(`APPLE_API_KEY must point to a materialized .p8 file for notarization: ${keyPath}`);
  }

  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);
  if (!fs.existsSync(appPath)) {
    throw new Error(`Signed macOS app bundle was not found: ${appPath}`);
  }

  const secrets = [keyPath, keyId, issuer];
  const authArgs = ["--key", keyPath, "--key-id", keyId, "--issuer", issuer];
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "breaktwenty-notary-"));
  const zipPath = path.join(tempDir, `${path.parse(appName).name}.zip`);

  try {
    logTreeSummary("App bundle payload before notarization", appPath);
    logChildBreakdown("app bundle", appPath);
    logChildBreakdown("Contents/Resources", path.join(appPath, "Contents", "Resources"));
    await runCommand(
      "Create notarization zip",
      "ditto",
      ["-c", "-k", "--sequesterRsrc", "--keepParent", appName, zipPath],
      { cwd: context.appOutDir, timeoutSeconds: 600 },
    );
    console.log(`[notary] Notarization ZIP size: ${formatBytes(fs.statSync(zipPath).size)}`);

    const submitStartedAt = Date.now();
    const submitOutput = await runCommand(
      `Submit to Apple notarization service (timeout ${SUBMIT_TIMEOUT_SECONDS}s)`,
      "xcrun",
      [
        "notarytool",
        "submit",
        zipPath,
        ...authArgs,
        "--output-format",
        "json",
      ],
      { secrets, timeoutSeconds: SUBMIT_TIMEOUT_SECONDS },
    );
    const submit = parseJsonOutput(submitOutput, "notarytool submit", secrets);
    const submissionId = submit.id;
    if (typeof submissionId !== "string" || !submissionId) {
      throw new Error(`notarytool submit did not return a submission id:\n${redact(submitOutput, secrets)}`);
    }
    console.log(
      `[notary] Submission id: ${submissionId} (upload/submit returned in ${Math.round((Date.now() - submitStartedAt) / 1000)}s)`,
    );
    console.log(
      "[notary] If this remains In Progress until timeout, file Apple Feedback with this submission id and the upload/submit duration above.",
    );

    const pollStartedAt = Date.now();
    const deadline = pollStartedAt + Math.max(POLL_TIMEOUT_SECONDS, 1) * 1000;
    while (Date.now() < deadline) {
      const infoOutput = await runCommand(
        "Poll Apple notarization status",
        "xcrun",
        ["notarytool", "info", submissionId, ...authArgs, "--output-format", "json"],
        { secrets, timeoutSeconds: 120 },
      );
      const info = parseJsonOutput(infoOutput, "notarytool info", secrets);
      const status = String(info.status || "").trim();
      const elapsedSeconds = Math.round((Date.now() - pollStartedAt) / 1000);
      console.log(`[notary] ${submissionId} status: ${status || "unknown"} after ${elapsedSeconds}s`);
      if (status === "Accepted") {
        await runCommand(
          "Staple notarization ticket",
          "xcrun",
          ["stapler", "staple", "-v", appName],
          { cwd: context.appOutDir, timeoutSeconds: 300 },
        );
        console.log(`[notary] ${appName} notarized and stapled.`);
        return;
      }
      if (["Invalid", "Rejected"].includes(status)) {
        const logOutput = await runCommand(
          "Fetch Apple notarization diagnostics",
          "xcrun",
          ["notarytool", "log", submissionId, ...authArgs],
          { secrets, timeoutSeconds: 120 },
        );
        throw new Error(`Apple notarization failed with status ${status}:\n${redact(logOutput, secrets)}`);
      }
      await delay(Math.max(POLL_INTERVAL_SECONDS, 1) * 1000);
    }
    throw new Error(
      `Apple notarization did not finish within ${POLL_TIMEOUT_SECONDS}s for submission ${submissionId}. ` +
      "The upload already returned a submission id, so this is Apple-side processing time.",
    );
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

module.exports = notarizeApp;
