# Third-Party Notices

Geer's MIT license covers only Geer's original code and documentation. The
installer also contains a frozen Python interpreter and open-source Python
packages resolved by `uv.lock`; the downloadable inference runtime contains its
own pinned Python interpreter, oMLX, and resolved dependencies. Those
components remain subject to their respective licenses.

`tools/build_release.py` generates a versioned package inventory and copies
available license, notice, authorship, and copying files into
`third-party-licenses/` inside the installer payload. Model licenses, notices,
recipes, and provenance are included separately under `model-cards/`,
`model-recipes/`, and `model-distributions/`.

The dependency names and versions in generated inventories are informational;
`uv.lock`, the runtime constants in `src/geer/runtime.py`, and the model
distribution and download manifests are the canonical immutable inputs.

## Pi installation

Geer setup downloads Pi 0.99.1 directly from its
[official upstream release](https://github.com/earendil-works/pi/releases/tag/v0.99.1).
The installer ships Geer's adapter and the integrity/provenance manifest in
`packaging/pi-runtime.json`; it does not redistribute the standalone Pi binary.
The manifest pins source revision `d86654abb8862e201933517d6f1fce9f88dd117f` and
links the release's corresponding source archive.

Pi's original code is MIT licensed, copyright Mario Zechner 2025. See its
[license at the pinned revision](https://github.com/earendil-works/pi/blob/d86654abb8862e201933517d6f1fce9f88dd117f/LICENSE).
The upstream standalone distribution embeds Bun 1.3.14 and other dependencies
with their own terms. Bun's
[license and third-party notices](https://github.com/oven-sh/bun/blob/bun-v1.3.14/LICENSE.md)
include JavaScriptCore/WebKit and other linked components. Geer's MIT license
does not replace those terms.

## T3 Code installation

Geer setup downloads T3 Code 0.0.44 from the
[official upstream release](https://github.com/pingdotgg/t3code/releases/tag/v0.0.44)
when no compatible installation is available. The installer includes only the
integrity and provenance manifest in `packaging/t3-desktop.json`; setup installs
the official signed app unchanged.

The manifest pins source revision `451afcb22d93f06cb24f9bc16703404564952553`
and the Apple Silicon zip
[`T3-Code-0.0.44-arm64.zip`](https://github.com/pingdotgg/t3code/releases/download/v0.0.44/T3-Code-0.0.44-arm64.zip).
The zip is 137,613,348 bytes with SHA-256
`480b5cd8ebcee4f4f43e108d309d91fe7c879931d8aae8a67b71cfd8a07ce0df`.
The app's identifier is `com.t3tools.t3code`, signed by T3 Tools, Inc. with Apple
Developer team ID `ARK85ZXQ4Z`.

T3 Code's original code is MIT licensed, copyright T3 Tools Inc. 2026. Its
[license at the pinned revision](https://github.com/pingdotgg/t3code/blob/451afcb22d93f06cb24f9bc16703404564952553/LICENSE)
and [source](https://github.com/pingdotgg/t3code/tree/451afcb22d93f06cb24f9bc16703404564952553)
remain authoritative. The upstream app bundles Electron 44.4.2, Chromium,
Node.js, and other dependencies with their own terms. The app also includes
license files for `@clerk/electron-passkeys`, its Darwin arm64 native module,
`@napi-rs/keyring`, and `node-pty`. Geer preserves these upstream licenses,
notices, and authorship without changing the signed bundle. Geer's MIT license
does not relicense T3 Code or its dependencies.
