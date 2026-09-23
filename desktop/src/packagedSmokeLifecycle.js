function requestGracefulSmokeExit(
  app,
  code,
  {
    forceAfterMs = 5000,
    schedule = setTimeout,
    setExitCode = (value) => { process.exitCode = value; },
  } = {},
) {
  setExitCode(code);
  app.quit();
  const fallback = schedule(() => app.exit(code), forceAfterMs);
  fallback?.unref?.();
  return fallback;
}

module.exports = {
  requestGracefulSmokeExit,
};
