#!/usr/bin/env node

const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");

const audience = "api://AzureADTokenExchange";
const requestUrl = process.env.ACTIONS_ID_TOKEN_REQUEST_URL;
const requestToken = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN;
const runnerTemp = process.env.RUNNER_TEMP || os.tmpdir();
const outputFile =
  process.env.AZURE_FEDERATED_TOKEN_OUTPUT ||
  path.join(runnerTemp, "breaktwenty-azure-federated-token");

if (!requestUrl || !requestToken) {
  throw new Error("GitHub OIDC request environment is unavailable. Ensure id-token: write is granted.");
}

const url = new URL(requestUrl);
url.searchParams.set("audience", audience);

function getJson(targetUrl) {
  return new Promise((resolve, reject) => {
    const request = https.request(
      targetUrl,
      {
        headers: {
          Authorization: `bearer ${requestToken}`,
          Accept: "application/json",
        },
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          if (response.statusCode < 200 || response.statusCode >= 300) {
            reject(new Error(`GitHub OIDC token request failed with HTTP ${response.statusCode}.`));
            return;
          }
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new Error(`GitHub OIDC token response was not valid JSON: ${error.message}`));
          }
        });
      },
    );
    request.on("error", reject);
    request.end();
  });
}

(async () => {
  const response = await getJson(url);
  if (typeof response.value !== "string" || response.value.length === 0) {
    throw new Error("GitHub OIDC response did not include a token value.");
  }
  fs.writeFileSync(outputFile, response.value, { encoding: "utf8", mode: 0o600 });
  fs.appendFileSync(process.env.GITHUB_ENV, `AZURE_FEDERATED_TOKEN_FILE=${outputFile}${os.EOL}`);
})();
