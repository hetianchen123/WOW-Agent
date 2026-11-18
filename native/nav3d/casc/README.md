# Local client resource adapter

`decode.cjs` loads binary readers from an **unmodified** checkout of
[Kruithne/wow.export](https://github.com/Kruithne/wow.export/tree/81cc25e4c500d2153c503b381eba82a906f52ba8)
(MIT). Its headless module adapter replaces UI, texture and network dependencies.
The adapter itself handles the retail WMO `MOVX`/`MPY2` chunks, which the pinned
reader does not expose. M2 uses the reader's collision header method, never the
render mesh or animation pipeline. Unknown/missing resources fail extraction.

[CascLib](https://github.com/ladislav-zezula/CascLib/tree/6b889ec8f421cbc9baccec296c582362ab917887)
is MIT and built outside Git. `readonly.patch` changes only two metadata probes
from read/write to read-only. This is necessary for a genuinely read-only game
installation and prevents requesting write access just to inspect `.build.info`.
No `CASC_FEATURE_ALLOW_DOWNLOAD` or `CASC_OVERCOME_ENCRYPTED` flag is enabled.
File reads request `CASC_STRICT_DATA_CHECK`; cached decoded bytes have checksums.

[WoWDBDefs Map.dbd](https://github.com/wowdev/WoWDBDefs/blob/5ebf2124996b538d6784584e69db6fe8d6bd8d06/definitions/Map.dbd)
is pinned by both commit and SHA-256. Map identity and WDT FileDataID come from
**the local build's Map.db2**, not from the listfile's filename or the UI map ID.

The WMO flag interpretation was cross-checked against
[TrinityCore's extractor](https://github.com/TrinityCore/TrinityCore/blob/master/src/tools/vmap4_extractor/wmo.cpp).
No GPL implementation was copied. Placement transforms follow wow.export's MIT
Blender importer. Unchanged upstream source retains its upstream license.

Bootstrap (requires Git, Node, a compiler and CMake):

```sh
.venv/bin/python -m bot.nav3d.bootstrap --cmake /path/to/cmake
```

CMake 3.31.6 was used in local validation. macOS output is
`data/nav3d/deps/CascLib-build/casc.framework/casc`; use the generated shared
library path on other systems. No system-wide library installation occurs.
Dependencies, caches, extracted client assets and builds remain under ignored
`data/nav3d/`. Do not commit or redistribute client assets.
