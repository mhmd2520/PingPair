# Third-party licenses

PingPair bundles and depends on the third-party components listed below. Each is
the property of its respective authors and is used under the license shown. This
file is a hand-curated summary; consult each project for its full license text.

PingPair itself is proprietary — © 2026 Mohamed Khaled. See the `LICENSE` file in
the repository root.

---

## Bundled command-line tools

| Component | Version | License | Source |
|---|---|---|---|
| **fping** | 5.5 | fping license (BSD-style) | https://github.com/schweikert/fping |
| **iperf3** | 3.21 | BSD 3-Clause (ESnet / Lawrence Berkeley National Laboratory) | https://github.com/esnet/iperf |

Both Windows binaries are Cygwin builds and ship the runtime DLLs they need:

| Component | License | Notes |
|---|---|---|
| **Cygwin runtime** (`cygwin1.dll` and friends) | LGPLv3+ **with the Cygwin runtime linking exception** (since 2016) | The exception permits distributing the DLL alongside the tools above with no copyleft obligation on PingPair itself. |
| **OpenSSL** (`cygcrypto-3.dll`, OpenSSL 3.x) | Apache License 2.0 | Linked by the iperf3 3.21 Cygwin build. |

---

## GUI toolkit

| Component | License | Source |
|---|---|---|
| **PySide6 / Qt 6** | LGPL v3 | https://www.qt.io/ — used as a dynamically-linked library so the LGPL relinking right is preserved. |

---

## Bundled font

| Component | License | Source |
|---|---|---|
| **Inter** | SIL Open Font License 1.1 | https://rsms.me/inter/ — full text shipped at `src/pingpair/resources/fonts/LICENSE.txt`. |

---

## Python dependencies

| Package | License | Purpose |
|---|---|---|
| **pyqtgraph** | MIT | Live throughput / latency charts. |
| **python-docx** | MIT | Word (`.docx`) report generation. |
| **openpyxl** | MIT | Excel (`.xlsx`) report generation. |
| **reportlab** | BSD 3-Clause | PDF report generation. |
| **matplotlib** | Matplotlib License (BSD-style, PSF-based) | Embedded report charts (Agg backend). |
| **pydantic** | MIT | Config validation. |
| **psutil** | BSD 3-Clause | Network-adapter enumeration. |
| **tabulate** | MIT | Plain-text table formatting. |

---

*If you redistribute PingPair, ship this notice with it.*

---

## Corresponding-source offer (LGPL / Cygwin components)

PingPair redistributes, **unmodified**, the Cygwin runtime DLLs
(`cygwin1.dll`, `cygz.dll`, `cygcrypto-3.dll`) and the Qt 6 / PySide6 libraries.
The complete corresponding source code for these is available from their
upstream projects — Cygwin: <https://cygwin.com/> and the Cygwin source mirror;
Qt: <https://download.qt.io/>. You may also obtain the corresponding source for
any LGPL/GPL component shipped with a given PingPair build from the project
author (<https://www.mhmd2520.com>) on request for three years from the date you
received that build. Qt/PySide6 is dynamically linked, so the LGPL relinking
right is preserved.

---

## Full license texts

### fping (developed by Stanford University)

```
Redistribution and use in source and binary forms are permitted
provided that the above copyright notice and this paragraph are
duplicated in all such forms and that any documentation,
advertising materials, and other materials related to such
distribution and use acknowledge that the software was developed
by Stanford University.  The name of the University may not be used
to endorse or promote products derived from this software without
specific prior written permission.
THIS SOFTWARE IS PROVIDED ``AS IS'' AND WITHOUT ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, WITHOUT LIMITATION, THE IMPLIED
WARRANTIES OF MERCHANTIBILITY AND FITNESS FOR A PARTICULAR PURPOSE.

Original author:  Roland Schemers, Stanford University
Copyright (c) 1992, 1994, 1997 Board of Trustees, Leland Stanford Jr. University
```

*fping was developed by Stanford University.*

### iperf3 (ESnet / Lawrence Berkeley National Laboratory) — BSD 3-Clause

```
iperf, Copyright (c) 2014-2026, The Regents of the University of California,
through Lawrence Berkeley National Laboratory (subject to receipt of any
required approvals from the U.S. Dept. of Energy). All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

(1) Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

(2) Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

(3) Neither the name of the University of California, Lawrence Berkeley
National Laboratory, U.S. Dept. of Energy nor the names of its contributors
may be used to endorse or promote products derived from this software without
specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. ...
```

The iperf3 source distribution's `LICENSE` file also covers bundled
sub-components (cJSON, BSD `queue.h`, the Illinois "units" code, and
`portable_endian.h`); shipping iperf3's own `LICENSE` verbatim satisfies all of
them.

### Cygwin runtime — LGPLv3+ with the Cygwin linking exception

```
As a special exception, the copyright holders of the Cygwin library grant you
additional permission to link libcygwin.a, crt0.o, and gcrt0.o with independent
modules to produce an executable, and to convey the resulting executable under
terms of your choice, without any need to comply with the conditions of LGPLv3
section 4.  An independent module is a module which is not itself based on the
Cygwin library.
```

The full LGPLv3 (and the GPLv3 it incorporates by reference) and the Apache
License 2.0 (for OpenSSL 3.x in `cygcrypto-3.dll`) apply to those components as
stated above; their canonical texts are at <https://www.gnu.org/licenses/lgpl-3.0.txt>,
<https://www.gnu.org/licenses/gpl-3.0.txt>, and
<https://www.apache.org/licenses/LICENSE-2.0.txt>.

*This is a good-faith compliance summary, not legal advice.*
