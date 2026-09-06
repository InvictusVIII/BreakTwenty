param(
  [Parameter(Mandatory = $true)]
  [string]$DistDir,

  [Parameter(Mandatory = $true)]
  [string]$ExpectedPublisher,

  [ValidatePattern("^[A-Za-z0-9._-]+$")]
  [string]$ArtifactBasename = "BreakTwenty"
)

$ErrorActionPreference = "Stop"

function Fail($Message) {
  Write-Error $Message
  exit 1
}

function Normalize-DistinguishedName([string]$Subject) {
  if ([string]::IsNullOrWhiteSpace($Subject)) {
    return ""
  }
  try {
    $dn = [System.Security.Cryptography.X509Certificates.X500DistinguishedName]::new($Subject)
    $Subject = $dn.Decode([System.Security.Cryptography.X509Certificates.X500DistinguishedNameFlags]::Reversed)
  } catch {
  }
  return (($Subject -replace '\s*,\s*', ',') -replace '\s*=\s*', '=').Trim().ToLowerInvariant()
}

function Assert-AuthenticodeSignature([string]$Path, [string]$Label) {
  if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
    Fail "$Label was not found: $Path"
  }

  $signature = Get-AuthenticodeSignature -LiteralPath $Path
  if ($null -eq $signature.SignerCertificate) {
    Fail "$Label has no signer certificate: $Path"
  }
  if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    Fail "$Label signature is not valid: $($signature.Status) $($signature.StatusMessage)"
  }

  $actual = Normalize-DistinguishedName $signature.SignerCertificate.Subject
  $expected = Normalize-DistinguishedName $ExpectedPublisher
  if ($actual -ne $expected) {
    Fail "$Label signer subject does not match expected publisher. Actual '$($signature.SignerCertificate.Subject)'."
  }

  if ($null -eq $signature.TimeStamperCertificate) {
    Fail "$Label signature has no timestamp certificate."
  }

  Write-Host "$Label signature valid: $Path"
}

$dist = Resolve-Path -LiteralPath $DistDir
$appCandidates = Get-ChildItem -LiteralPath $dist -Recurse -File -Filter "$ArtifactBasename.exe" |
  Where-Object { $_.FullName -match "\\win-unpacked\\" -or $_.FullName -match "\\__unpacked\\" }
if ($appCandidates.Count -eq 0) {
  $appCandidates = Get-ChildItem -LiteralPath $dist -Recurse -File -Filter "$ArtifactBasename.exe"
}
if ($appCandidates.Count -ne 1) {
  Fail "Expected exactly one packaged $ArtifactBasename.exe, found $($appCandidates.Count)."
}

$installerCandidates = Get-ChildItem -LiteralPath $dist -File -Filter "$ArtifactBasename-*.exe" |
  Where-Object { $_.Name -notlike "*.__uninstaller.exe" }
if ($installerCandidates.Count -ne 1) {
  Fail "Expected exactly one distributed NSIS installer, found $($installerCandidates.Count)."
}

$latestYml = Join-Path $dist "latest.yml"
if (!(Test-Path -LiteralPath $latestYml -PathType Leaf)) {
  Fail "Windows updater metadata latest.yml is missing."
}
$latestText = Get-Content -LiteralPath $latestYml -Raw
if ($latestText -notmatch [regex]::Escape($installerCandidates[0].Name)) {
  Fail "latest.yml does not reference the signed NSIS installer $($installerCandidates[0].Name)."
}

$blockmap = "$($installerCandidates[0].FullName).blockmap"
if (!(Test-Path -LiteralPath $blockmap -PathType Leaf)) {
  Fail "NSIS blockmap is missing for $($installerCandidates[0].Name)."
}

Assert-AuthenticodeSignature -Path $appCandidates[0].FullName -Label "Packaged application executable"
Assert-AuthenticodeSignature -Path $installerCandidates[0].FullName -Label "Distributed NSIS installer"
