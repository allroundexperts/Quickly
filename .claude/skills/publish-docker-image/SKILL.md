---
name: publish-docker-image
description: Build and publish the multi-arch Quickly image to Docker Hub as allroundexperts/quickly - the image Railway and other PaaS deployments pull. Use when asked to cut a release image, publish to Docker Hub, or generate the Railway image.
---

# Publish the Quickly image to Docker Hub

Builds `Dockerfile` for `linux/amd64` + `linux/arm64` and pushes to
**`allroundexperts/quickly`**. Railway only runs amd64, but the repo publishes
both, so keep both unless asked otherwise.

## Preferred path: GitHub Actions

`.github/workflows/release.yml` builds each arch on a native runner, which is
faster and avoids all emulation problems. Pushing a `v*` tag triggers it; the
`workflow_dispatch` form takes a `push_image` boolean for a dry run.

**This requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repo secrets.** As of
the last run they were not set on `allroundexperts/Quickly` — check with
`gh secret list` before assuming CI works, or the Docker Hub login step fails.

## Local path (no Docker installed by default on this Mac)

### 1. Runtime

There is no Docker Desktop here. Colima provides the daemon:

```bash
brew install colima docker docker-buildx
mkdir -p ~/.docker   # add "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"] to config.json
colima start --cpu 6 --memory 12 --disk 80 --vm-type vz --vz-rosetta
```

`--vz-rosetta` gives hardware-assisted x86 translation instead of QEMU.

### 2. Builder

The default `docker` driver cannot do multi-platform builds, and buildx's
platform probe **under-reports** — it lists only `linux/arm64, linux/386` even
though amd64 containers run fine. Verify emulation directly rather than trusting
the probe:

```bash
docker run --rm --platform linux/amd64 alpine:3.20 uname -m   # must print x86_64
```

Then force the platforms onto the builder explicitly:

```bash
docker buildx create --name quickly-builder --driver docker-container \
  --platform linux/amd64,linux/arm64 --bootstrap --use
```

### 3. Login

`docker login` needs a real TTY. It fails from inside Claude Code, including via
the `!` prefix (`cannot perform an interactive login from a non-TTY device`).
Ask the user to run `docker login -u allroundexperts` in Terminal.app and paste a
Docker Hub access token. It writes `~/.docker/config.json`, which the CLI here
then picks up. Verify by reading only the **key names** under `auths` — never
print the file, it contains the credential.

### 4. Build and push

```bash
docker buildx build --builder quickly-builder \
  --platform linux/amd64,linux/arm64 \
  -t allroundexperts/quickly:vX.Y.Z -t allroundexperts/quickly:latest \
  --push --progress plain .
```

Version: patch-bump the newest tag on Docker Hub, which may be ahead of or behind
local git tags. Check both:

```bash
curl -s "https://hub.docker.com/v2/repositories/allroundexperts/quickly/tags?page_size=10&ordering=last_updated"
git tag --sort=-v:refname | head -5
```

## Gotchas that have actually bitten

- **Never pipe the build through `tail`.** The pipeline's exit status is `tail`'s,
  so a failed build reports `exit code 0`. It also buffers, hiding all progress.
  Redirect to a log file and read it incrementally instead.
- **esbuild crashes under QEMU.** If the frontend stage is ever built emulated, it
  dies mid-bundle with `[vite:esbuild] The service was stopped`. `Dockerfile:4`
  pins that stage to `--platform=$BUILDPLATFORM` so it builds natively; its output
  is static JS/CSS and therefore arch-independent. Do not remove that pin.
- **`pip install` is the slow stage**, not the frontend — `any-llm-sdk[all]`
  pulls a very large dependency tree, emulated, once per arch.
- `Dockerfile` hardcodes `ENV QUICKLY_PREBUILT_IMAGE=1`, which hides the
  CNAME custom-domain UI. That is correct for a published image; leave it.

## Repo context

This is a fork of `AbdelftahZowail/Quickly`. Upstream publishes to the `azowail`
Docker Hub namespace, which this account cannot push to. Registry references in
`release.yml` point at `allroundexperts`; the `github.com/azowail/...` URLs in the
release body are upstream source links and are intentionally left alone.
