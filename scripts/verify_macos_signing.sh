#!/usr/bin/env bash
set -euo pipefail

dist_dir="${1:?Usage: verify_macos_signing.sh <dist-dir>}"
expected_team_id="${APPLE_TEAM_ID:?APPLE_TEAM_ID is required for macOS signing verification.}"
expected_app_name="${BREAKTWENTY_EXPECTED_APP_NAME:-BreakTwenty}"
expected_artifact_name="${BREAKTWENTY_EXPECTED_ARTIFACT_BASENAME:-BreakTwenty}"

fail() {
  echo "error: $*" >&2
  exit 1
}

[[ "$expected_app_name" =~ ^[A-Za-z0-9._\ -]+$ ]] || fail "Unsafe expected application name."
[[ "$expected_artifact_name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "Unsafe expected artifact basename."

find_one() {
  local description="$1"
  shift
  local matches=("$@")
  if [ "${#matches[@]}" -ne 1 ]; then
    fail "Expected exactly one ${description}, found ${#matches[@]}."
  fi
  printf '%s\n' "${matches[0]}"
}

verify_app_bundle() {
  local app_path="$1"
  local label="$2"

  [ -d "$app_path" ] || fail "${label} is missing: ${app_path}"
  codesign --verify --deep --strict --verbose=2 "$app_path"

  local details
  details="$(codesign -dv --verbose=4 "$app_path" 2>&1)"
  echo "$details"
  grep -q "Authority=Developer ID Application" <<<"$details" || fail "${label} is not signed by a Developer ID Application identity."
  grep -q "TeamIdentifier=${expected_team_id}" <<<"$details" || fail "${label} TeamIdentifier does not match APPLE_TEAM_ID."
  grep -q "Runtime Version=" <<<"$details" || fail "${label} does not have Hardened Runtime enabled."
  grep -q "Timestamp=" <<<"$details" || fail "${label} does not have a secure signing timestamp."

  local spctl_output
  spctl_output="$(spctl --assess --verbose=4 --type execute "$app_path" 2>&1)"
  echo "$spctl_output"
  grep -q "accepted" <<<"$spctl_output" || fail "${label} was not accepted by Gatekeeper."
  grep -q "source=Notarized Developer ID" <<<"$spctl_output" || fail "${label} Gatekeeper source is not Notarized Developer ID."

  xcrun stapler validate "$app_path"
}

shopt -s nullglob
apps=("${dist_dir}"/mac*/"${expected_app_name}.app")
dmgs=("${dist_dir}"/"${expected_artifact_name}"-*.dmg)
zips=("${dist_dir}"/"${expected_artifact_name}"-*.zip)

app_path="$(find_one "built ${expected_app_name}.app bundle" "${apps[@]}")"
dmg_path="$(find_one "distributed ${expected_artifact_name} DMG" "${dmgs[@]}")"
zip_path="$(find_one "macOS updater ZIP" "${zips[@]}")"

verify_app_bundle "$app_path" "Built application bundle"

codesign --verify --strict --verbose=2 "$dmg_path"
echo "Distributed DMG signature is valid. Notarization is verified on the contained app bundle."

mount_point=""
plist_file="$(mktemp)"
zip_extract_dir="$(mktemp -d)"
cleanup() {
  if [ -n "$mount_point" ]; then
    hdiutil detach "$mount_point" -quiet || true
  fi
  rm -f "$plist_file"
  rm -rf "$zip_extract_dir"
}
trap cleanup EXIT

/usr/bin/ditto -x -k "$zip_path" "$zip_extract_dir"
zipped_apps=("${zip_extract_dir}"/"${expected_app_name}.app")
zipped_app="$(find_one "${expected_app_name}.app inside the updater ZIP" "${zipped_apps[@]}")"
verify_app_bundle "$zipped_app" "ZIP-contained updater application bundle"

hdiutil attach -readonly -nobrowse -plist "$dmg_path" >"$plist_file"
mount_point="$(
  /usr/libexec/PlistBuddy -c 'Print :system-entities' "$plist_file" 2>/dev/null |
    awk -F'= ' '/mount-point = / { print $2; exit }' ||
    true
)"
if [ -z "$mount_point" ]; then
  echo "hdiutil attach plist:" >&2
  /usr/libexec/PlistBuddy -c 'Print' "$plist_file" >&2 || true
  fail "Could not determine mounted DMG path."
fi

mounted_apps=("${mount_point}"/"${expected_app_name}.app")
mounted_app="$(find_one "${expected_app_name}.app inside the DMG" "${mounted_apps[@]}")"
verify_app_bundle "$mounted_app" "DMG-contained application bundle"
