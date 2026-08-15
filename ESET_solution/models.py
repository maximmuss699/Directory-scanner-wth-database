from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


EntryRecord = Tuple[str, str, str, str, int, int, int, int, Optional[bytes]]


@dataclass(frozen=True)
class Change:
    change_type: str
    path: str
    previous_path: Optional[str]


@dataclass(frozen=True)
class ScanResult:
    entry_count: int
    change_counts: Dict[str, int]
    changes: List[Change]
