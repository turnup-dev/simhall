# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Hasanat Kazmi
"""simhall: a virtual GPU hall for testing bring-up tooling without hardware.

```python
from simhall import HallSpec, Hall, Fault, Action
hall = Hall.generate(HallSpec(site="hall-b"))
hall.inject(Fault(kind="wrong_port", cable="CBL-12-07-03", to_device="leaf4", to_interface="swp22"))
```
"""

from __future__ import annotations

__version__ = "0.1.0"

from simhall.actions import Action
from simhall.faults import Fault
from simhall.hall import Conflict, Hall, SimError, UnknownObject
from simhall.models import CableSpec, Endpoint, HallSpec

__all__ = [
    "Action",
    "CableSpec",
    "Conflict",
    "Endpoint",
    "Fault",
    "Hall",
    "HallSpec",
    "SimError",
    "UnknownObject",
    "__version__",
]
