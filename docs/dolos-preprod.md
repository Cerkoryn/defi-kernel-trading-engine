# Dolos on TrueNAS: Preprod

One app, one Compose file, one data directory. The pinned official image uses
storage v4 and synchronizes from the Preprod relay, starting at genesis when the
directory is empty. This rebuild may take days. Restarts resume the database;
there is no snapshot download, bootstrap marker or automatic data deletion.

## Replace the existing installation

Stop the trading engine and stop `dolos-preprod` in TrueNAS Apps. Keep the existing
dataset `/mnt/Business/Crypto/dolos-preprod` and its `data/` directory. This procedure
discards the old Dolos database and failed imports; wallet keys and the engine's
journal on the workstation are unrelated and must remain intact.

Before deleting anything, run these in the **TrueNAS shell**:

```bash
sudo docker pull ghcr.io/txpipe/dolos:2.0.0-alpha.0@sha256:c91ea985f77542edc13b1cb5bd2f002c2df20f1669a517d72a3bf6663933aa59
python3 -c 'import socket; socket.create_connection(("preprod-node.world.dev.cardano.org", 30000), timeout=10).close(); print("Relay reachable")'
sudo ls -lah /mnt/Business/Crypto/dolos-preprod
sudo df -h /mnt/Business/Crypto/dolos-preprod/data
```

Do not proceed if the pull or connectivity check fails. Allow at least 100 GiB of
free provisioning headroom after cleanup and monitor growth; this is a planning
budget, not a measured final database size.

Run this **once**, with the app stopped. It clears `data/` while preserving the
directory and its permissions, and removes the named leftovers from the previous
failed import. It refuses a running app, a redirected data path or nested mounts.

```bash
sudo bash <<'SH'
set -euo pipefail
dolos_root=/mnt/Business/Crypto/dolos-preprod
test -d "$dolos_root/data"
test "$(realpath -e -- "$dolos_root/data")" = "$dolos_root/data"
dolos_running=$(docker ps -q --filter label=com.docker.compose.project=ix-dolos-preprod)
if [ -n "$dolos_running" ]; then
  echo 'Stop dolos-preprod in TrueNAS Apps before cleanup.' >&2
  exit 1
fi
dolos_mounts=$(findmnt -rn -o TARGET)
if awk -v prefix="$dolos_root/" 'index($0, prefix) == 1 {found=1} END {exit !found}' <<< "$dolos_mounts"; then
  echo 'Nested mount detected; inspect the dataset layout before cleanup.' >&2
  exit 1
fi
find "$dolos_root/data" -xdev -mindepth 1 -delete
rm -rf --one-file-system -- "$dolos_root/state" "$dolos_root/.bootstrap-in-progress" "$dolos_root/.dolos-snapshot-tmp"
rm -f -- "$dolos_root/.bootstrap-complete" "$dolos_root/bootstrap.log"
SH
```

Edit the existing TrueNAS app and replace its YAML with the entire
[compose.yaml](../deploy/dolos-preprod/compose.yaml), then start it. The existing
source path, app name and ports are already configured. Do not create another app
or data directory. Once the old container has been replaced, remove its unused
image with `sudo docker image rm ghcr.io/txpipe/dolos:v1.6.0`; do not force removal
or run global Docker pruning.

## Fresh installations and operation

For a fresh install, create the same empty `data/` directory with read/write access
for Apps UID/GID **568:568** and permission to traverse its parents. Use TrueNAS
**Apps → Discover → ⋮ → Install via YAML**, with app name `dolos-preprod` and the
same Compose file. Existing installations already have these permissions.

Configuration lives in the YAML and is materialized into `/tmp` at startup. The
container keeps its root filesystem read-only, runs without extra capabilities,
and is limited to two CPUs/four GiB. Rotating app logs are capped at three 10-MiB
files. Watch the app logs, disk growth and memory usage during synchronization.
A running container or a successful health response does not prove synchronization.

The APIs bind to **192.168.0.3:13000** (MiniBlockfrost) and **192.168.0.3:11442**
(MiniKupo). Keep them private to the LAN. No wallet key or Koios API key belongs on
the NAS. A failed startup should be diagnosed from app logs; never clear data as
part of a routine restart. The NAS startup, rebuild duration and resource needs
still require live verification.

## Qualify and reconnect the engine

The [previous governance discrepancy](upstream-contributions.md#dolos-governance)
is why the database is being rebuilt. `examples/preprod-dolos.toml` deliberately
keeps ledger queries, evaluation and submission on Koios until the rebuilt state
passes qualification. Keep `KOIOS_API_KEY` in the workstation environment.

Once synchronized, first compare **Dolos's own** parameters with Koios at a common
canonical block. Compare all consumed parameters, not just the three known
mismatches. The interim configuration's `provider-check` uses Koios ledger state
and therefore cannot establish Dolos parameter correctness by itself.

After that comparison passes, set `ledger = "nas"` in the existing capability
section and run the checks with the public wallet address, historical transaction
hashes and strategy, using `provider-check --address … --transaction … --strategy …
--compare-koios`. Its exact ledger/evaluator comparison must pass. Verify wallet
outputs, historical evidence and venue discovery before running shadow mode:

```bash
.venv/bin/kernel --config examples/preprod-dolos.toml arbitrage \
  --manifest state/preprod-e0e0961e12306509/mvp-test/wallet/wallet.json \
  --strategy examples/preprod-arbitrage-soak.json
```

Run shadow for at least one hour, including an app restart. Check synchronization,
provider errors, logging and reconciliation. Only after these checks pass, set
`submission = "nas"` and run the same command with `--execute --iterations 1`.
This authorizes at most one qualifying trade under the existing strategy limits.
If none qualifies, leave submission qualification pending. If a transaction is
pending, restart without `--execute` to reconcile it; do not rebuild or resubmit it
manually. Confirm final accounting before allowing unattended execution.

Record results in the existing qualification evidence. Keep unsigned evaluation on
Koios; the governance upgrade does not establish an unsigned Dolos evaluator.
If parameters still disagree, restore the explicit Koios ledger/submission bindings
and investigate upstream rather than overriding values.

Sources: [official release](https://github.com/txpipe/dolos/releases/tag/v2.0.0-alpha.0),
[relay initialization](https://github.com/txpipe/dolos/blob/v2.0.0-alpha.0/src/bin/dolos/bootstrap/relay.rs),
[TrueNAS YAML installation](https://apps.truenas.com/managing-apps/installing-custom-apps/).
Dolos follows a trusted relay rather than independently validating consensus.
