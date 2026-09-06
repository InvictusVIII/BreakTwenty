const fs = require("node:fs");
const path = require("node:path");
const yaml = require("js-yaml");

const root = __dirname;
const baseConfig = yaml.load(fs.readFileSync(path.join(root, "electron-builder.yml"), "utf8"));

function requireEnv(name) {
  const value = process.env[name];
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`Missing required official desktop signing configuration: ${name}`);
  }
  return value;
}

function mergeConfig(base, overlay) {
  if (Array.isArray(base) || Array.isArray(overlay)) {
    return overlay === undefined ? base : overlay;
  }
  if (base && overlay && typeof base === "object" && typeof overlay === "object") {
    return Object.fromEntries(
      [...new Set([...Object.keys(base), ...Object.keys(overlay)])].map((key) => [
        key,
        mergeConfig(base[key], overlay[key]),
      ]),
    );
  }
  return overlay === undefined ? base : overlay;
}

const signingPlatform = process.env.BREAKTWENTY_RELEASE_SIGNING_PLATFORM;
const publicDesktopIdentity = {
  appId: "app.breaktwenty.desktop",
  productName: "BreakTwenty",
  executableName: "BreakTwenty",
  extraMetadata: {
    breaktwentyDesktopIdentity: {
      technicalId: "app.breaktwenty.desktop",
      storageNamespace: "BreakTwenty",
      windowsLocalAppDataNamespace: "BreakTwenty",
    },
  },
};

function configuredDesktopIdentity() {
  const raw = process.env.BREAKTWENTY_DESKTOP_IDENTITY_CONFIG;
  if (!raw) {
    return publicDesktopIdentity;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`BREAKTWENTY_DESKTOP_IDENTITY_CONFIG must be valid JSON: ${error.message}`);
  }
  for (const field of ["appId", "productName", "executableName"]) {
    if (typeof parsed?.[field] !== "string" || parsed[field].trim() === "") {
      throw new Error(`BREAKTWENTY_DESKTOP_IDENTITY_CONFIG must define ${field}`);
    }
  }
  return parsed;
}

const overlays = {
  win: () => ({
    forceCodeSigning: true,
    toolsets: {
      winCodeSign: "latest",
    },
    win: {
      verifyUpdateCodeSignature: true,
      sign: {
        type: "azure",
        publisherName: requireEnv("AZURE_ARTIFACT_SIGNING_PUBLISHER"),
        endpoint: requireEnv("AZURE_ARTIFACT_SIGNING_ENDPOINT"),
        codeSigningAccountName: requireEnv("AZURE_ARTIFACT_SIGNING_ACCOUNT"),
        certificateProfileName: requireEnv("AZURE_ARTIFACT_SIGNING_PROFILE"),
        fileDigest: "SHA256",
        timestampDigest: "SHA256",
      },
    },
  }),
  mac: () => ({
    forceCodeSigning: true,
    afterSign: "scripts/notarize_macos.cjs",
    mac: {
      notarize: false,
      target: ["dmg", "zip"],
      sign: {
        type: "distribution",
        hardenedRuntime: true,
        entitlements: "build/entitlements.mac.plist",
        entitlementsInherit: "build/entitlements.mac.inherit.plist",
        binaries: ["Contents/Resources/desktop/update-helper/BreakTwentyUpdateHelper"],
      },
    },
    dmg: {
      sign: true,
      writeUpdateInfo: false,
    },
  }),
};

if (signingPlatform && !Object.hasOwn(overlays, signingPlatform)) {
  throw new Error(
    "BREAKTWENTY_RELEASE_SIGNING_PLATFORM must be set to 'win' or 'mac' for official signed builds.",
  );
}

module.exports = mergeConfig(
  mergeConfig(baseConfig, configuredDesktopIdentity()),
  signingPlatform ? overlays[signingPlatform]() : {},
);
