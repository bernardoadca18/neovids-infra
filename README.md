# NeoVids infrastructure

Version-controlled operational layer for the five NeoVids service repositories.
It owns only cross-service Docker orchestration, the root operational CLI,
Compose overrides and operational documentation.

See [`infra/README.md`](infra/README.md) for architecture, storage contracts,
commands and troubleshooting.

From the parent NeoVids workspace, the compatibility symlinks preserve the
normal workflow:

```bash
./scripts/neovids doctor
./scripts/neovids up
./scripts/neovids health
./scripts/neovids down
```

They are optional because `./neovids-infra/scripts/neovids` works directly. To
recreate the convenient paths in a workspace where they do not exist, run from
the parent directory:

```bash
ln -s neovids-infra/scripts scripts
ln -s neovids-infra/infra infra
```
