const { PACKAGED_SAFE_STORAGE_SMOKE_FLAG } = require('./packagedSafeStorageSmokeProtocol');
const { ISOLATED_ELECTRON_JOURNEY_FLAG } = require('./isolatedElectronJourneyProtocol');

if (process.argv.includes(PACKAGED_SAFE_STORAGE_SMOKE_FLAG)) {
  require('./packagedSafeStorageSmoke');
} else if (process.argv.includes(ISOLATED_ELECTRON_JOURNEY_FLAG)) {
  require('./isolatedElectronJourney');
} else {
  require('./main');
}
