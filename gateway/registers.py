"""Register map loader: shared/registers.yaml -> lookup by (kind, addr)."""
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "shared" / "registers.yaml"
KINDS = {
    "coils": "coil",
    "discrete_inputs": "discrete_input",
    "input_registers": "input_register",
    "holding_registers": "holding_register",
}
READ_ONLY = {"discrete_input", "input_register"}


@dataclass(frozen=True)
class Register:
    kind: str
    name: str
    addr: int
    scale: int = 1
    default: int | None = None
    min: int | None = None
    max: int | None = None
    writer: str | None = None
    desc: str = ""

    @property
    def twin_only(self) -> bool:
        return self.writer == "twin"

    @property
    def writable(self) -> bool:
        return self.kind not in READ_ONLY


class RegisterMap:
    def __init__(self, data: dict):
        self.unit_id = data["unit_id"]
        self.ports = data["ports"]
        self._by_key: dict[tuple[str, int], Register] = {}
        self._by_name: dict[str, Register] = {}
        for section, kind in KINDS.items():
            for name, f in (data.get(section) or {}).items():
                reg = Register(
                    kind=kind, name=name, addr=f["addr"], scale=f.get("scale", 1),
                    default=f.get("default"), min=f.get("min"), max=f.get("max"),
                    writer=f.get("writer"), desc=f.get("desc", ""),
                )
                self._by_key[(kind, reg.addr)] = reg
                self._by_name[name] = reg

    def lookup(self, kind: str, addr: int) -> Register | None:
        return self._by_key.get((kind, addr))

    def by_name(self, name: str) -> Register | None:
        return self._by_name.get(name)

    def defaults(self, kind: str) -> dict[int, int]:
        return {r.addr: r.default for (k, _), r in self._by_key.items()
                if k == kind and r.default is not None}


def load_registers(path: Path | str = DEFAULT_PATH) -> RegisterMap:
    with open(path, encoding="utf-8") as f:
        return RegisterMap(yaml.safe_load(f))
