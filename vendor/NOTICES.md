# Third-party source code in `vendor/`

This directory contains source-only checkouts of upstream projects used as
reference implementations and (in some cases) Python imports.  No
pre-compiled binaries are shipped.  Each tree retains its upstream
license file (`COPYING` / `COPYING.LESSER`); this NOTICES file summarises
what's here and the licensing implications for redistribution.

| Tree                              | License                  | Upstream                                                |
|-----------------------------------|--------------------------|---------------------------------------------------------|
| `iOSbackup-master/`               | LGPL                     | https://github.com/avibrazil/iOSbackup                  |
| `libimobiledevice-master/`        | **GPL v2**               | https://github.com/libimobiledevice/libimobiledevice    |
| `libimobiledevice-glue-1.3.2/`    | LGPL v2.1                | https://github.com/libimobiledevice/libimobiledevice-glue |
| `libplist-2.7.0/`                 | **GPL v2** (lib: LGPL)   | https://github.com/libimobiledevice/libplist            |
| `libusbmuxd-2.1.1/`               | LGPL v2.1                | https://github.com/libimobiledevice/libusbmuxd          |
| `libirecovery-1.3.1/`             | LGPL v2.1                | https://github.com/libimobiledevice/libirecovery        |
| `ideviceinstaller-1.2.0/`         | **GPL v2**               | https://github.com/libimobiledevice/ideviceinstaller    |

## Licensing implications

Three of the seven trees (`libimobiledevice-master`, `libplist-2.7.0`,
`ideviceinstaller-1.2.0`) are **GPL v2**.  GPL v2 is a strong copyleft
license: any redistribution of a derivative work that bundles GPL v2
sources must itself be licensed under GPL v2 (or a compatible license).

For PhoneTransfer this means:

- The project as a whole **cannot be redistributed under a permissive
  license (MIT, BSD, Apache-2.0)** while these GPL v2 source trees are
  bundled in `vendor/`.
- If a permissive license is desired for the PhoneTransfer Python code,
  the GPL v2 trees must either:
    1. be moved out of the repo (e.g. fetched on demand by a build
       script, in which case they are not "distributed" with
       PhoneTransfer), or
    2. be replaced with the LGPL-licensed runtime libraries from the
       `libimobiledevice` project that are equivalent for our needs
       (we only call into these via `pymobiledevice3`, which is a
       separate pure-Python re-implementation — the C sources here are
       reference-only).

If PhoneTransfer adopts GPL v2 (or v3, if upstream allows the upgrade),
no further action is required for these trees beyond keeping each
tree's `COPYING` file intact.

## Why these are bundled

The C libraries are kept as **reference**, not as compiled binaries:
the active iOS device protocol stack is `pymobiledevice3` (a pure-Python
re-implementation, MIT-licensed) plus `iphone_backup_decrypt` and
`iOSbackup` for the backup-file format.  The C trees were originally
imported when validating wire protocol details against the canonical
implementation; they are not invoked at runtime.

## Action items

- Decide on PhoneTransfer's project license (see `LICENSE` at repo
  root, currently absent — see audit item #2).
- If a permissive license is chosen, remove `libimobiledevice-master/`,
  `libplist-2.7.0/`, and `ideviceinstaller-1.2.0/` from the tree.
- Add a `vendor/README.md` if any of these trees turn out to be
  load-bearing at runtime (none currently are; verify via grep before
  removing).
