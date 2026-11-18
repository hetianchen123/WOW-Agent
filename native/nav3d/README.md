# WowAgent navigation backend

The C ABI in `bridge.cpp` is WowAgent code. It uses unmodified Recast and Detour
sources vendored under `vendor/recastnavigation`, version **1.6.0**, commit
`6dc1667f580357e8a2154c28b7867bea7e8ad3a7` (annotated tag object
`b4554541b658630816dba41466eb1cefb624519e`).

Upstream: https://github.com/recastnavigation/recastnavigation

Only `Recast/{Include,Source}`, `Detour/{Include,Source}` and `License.txt` are
included. Preserve their zlib copyright/license notices. No TrinityCore or
OpenMW code is copied. Their architectures are research references only.

Build: `.venv/bin/python -m bot.nav3d_cli backend`. Requires a C++ compiler,
not CMake, network access, a game client or an API key. Generated libraries and
maps live under ignored `data/nav3d/`. The Python wrapper converts Z-up scene
coordinates to Recast's Y-up coordinates using `(x, z, -y)`.

The v1 backend intentionally builds one bounded local region (up to four million
horizontal voxel cells), not an entire continent. It rejects incomplete paths,
exhausted query buffers and excessive endpoint snapping. Detour caches are
local build artifacts, not a portable interchange format.
