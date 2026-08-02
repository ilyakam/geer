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
distribution manifests are the canonical immutable inputs.
