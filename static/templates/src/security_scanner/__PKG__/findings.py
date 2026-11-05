from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str        # high / medium / low
    path: str
    line: int
    message: str

    def fingerprint(self):
        """Stable across unrelated edits: no line number."""
        return f"{self.rule}:{self.path}:{self.message}"

    def as_dict(self):
        return asdict(self)
