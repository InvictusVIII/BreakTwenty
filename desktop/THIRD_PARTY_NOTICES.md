# BreakTwenty Desktop Third-Party Notices

BreakTwenty packages `sqlcipher3` 0.6.2 with SQLCipher 4.12.0 Community Edition
and its statically linked cryptographic provider. Their applicable licence
texts are distributed in the packaged app's `third-party-licenses` directory.

- `SQLCipher.txt` — SQLCipher Community Edition BSD-style license.
- `sqlcipher3.txt` — Python binding license.
- `Apache-2.0.txt` — Apache License 2.0 used by OpenSSL 3.

SQLCipher and SQLCipher Community Edition are provided by Zetetic LLC.
Inclusion does not imply endorsement by Zetetic.

The packaged application also generates a dependency licence inventory for
the bundled Python runtime. Frontend emoji assets have a separate inventory in
`frontend/public/emoji/THIRD-PARTY-LICENSES.md` and source credits in
`frontend/public/emoji/finance/CREDITS.md`. The repository copy of the OpenSSL
source licence is at `backend/scripts/openssl-4.0.1-LICENSE.txt`.

Production JavaScript dependencies are inventoried with their bundled licence
and notice texts in `desktop/NODE_RUNTIME_NOTICES.md` and
`frontend/public/THIRD_PARTY_SOFTWARE_NOTICES.md`. Both files are generated
from the exact package locks and verified by release health.

These third-party components and assets are not covered by BreakTwenty's
source-available licence where their own licence terms apply. This notice is
informational; the identified third-party licence texts control those materials.
