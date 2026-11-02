"""Smart attribute extraction (Smart Matching).

Turns a raw title (+ optional characteristics text) into a canonical config
group so that prices are only compared within one configuration bucket —
never PS4 Fat vs PS4 Pro, never 128GB vs 256GB.

Output: :class:`NormalizedLot` holding a normalized model/revision, a
normalized storage (or VRAM) capacity in GB, and a kit assessment used as a
bonus/penalty during profit calculation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# --- capacity -> canonical GB -------------------------------------------------
_CAPACITY_GB = {
    8: 8, 6: 6, 12: 12, 16: 16, 24: 24,
    64: 64, 120: 128, 125: 128, 128: 128,
    240: 256, 250: 256, 256: 256,
    480: 512, 500: 512, 512: 512,
    1024: 1024, 1000: 1024, 1: 1024,
    2048: 2048, 2000: 2048, 2: 2048,
}

# --- GPU family regexes (model + vram is the grouping for GPUs) ---------------
_GPU_RE = re.compile(
    r"\b(rtx|gtx|radeon|rx|quadro|arc)\s*([a-z\s]*?)(\d{2,4})",
    re.IGNORECASE,
)

# Only applied AFTER a GPU family is detected, so a bare "12gb"/"12 гб"
# alongside the model is safely interpreted as VRAM.
_GPU_VRAM_RE = re.compile(r"\b(\d{1,3})\s*[гg][bб]\b", re.IGNORECASE)

# --- console family regexes ---------------------------------------------------
_PS_RE = re.compile(r"\b(?:sony\s+)?(?:playstation|ps(?!:))\s*([45])(?:\s*(pro|slim|fat|slima|s))?",
                    re.IGNORECASE)
_XBOX_RE = re.compile(r"\bxbox\s*((?:one|series)?\s*[xs]?)\s*(\d{1,3})?", re.IGNORECASE)

# --- storage capacity regexes ---------------------------------------------------
_STORAGE_RE = re.compile(
    r"(?:^|\s)(?:nvme|ssd|hdd|жёсткий|жесткий|диск|кз|nand|emmc)?"
    r"\s*(\d{1,4})\s*(тб|tb|гб|gb|гигабайт|терабайт)",
    re.IGNORECASE,
)

# --- kit / completeness keywords ----------------------------------------------
_KIT_KEYWORDS: list[tuple[str, int, re.Pattern]] = [
    ("полный комплект", 900, re.compile(r"полн\w*\s*комплект|full\s*box", re.I)),
    ("2 геймпада", 800, re.compile(r"\b2\s*геймпад|\b2\s*джойсти|\btwo\s*pad", re.I)),
    ("игры", 500, re.compile(r"\bигр\w*|\bgames?\b", re.I)),
    ("коробка", 400, re.compile(r"\bкоробк|\bbox\b", re.I)),
    ("кабель/зарядка", 150, re.compile(r"\bзаряд\w*|\bкабель|\bкабел\w*|\bкаб\w*", re.I)),
]

_KIT_PENALTY: list[tuple[str, int, re.Pattern]] = [
    ("без комплекта", 1500, re.compile(r"без\s*комплект|без\s*насад\w*", re.I)),
    ("тушка", 1200, re.compile(r"\bтушк\w*|\bтушка\b|console only|без\s*провод\w*", re.I)),
]

_NO_KIT_HINT = re.compile(
    r"\b(без\s*комплект|тушк\w*|без\s*провод\w*|без\s*перифер\w*)", re.IGNORECASE
)


@dataclass
class NormalizedLot:
    title: str
    model: str = "unknown"            # canonical family (e.g. "ps4", "rtx_3060")
    revision: str = ""                # e.g. "pro" | "slim" | "fat"
    memory_gb: Optional[int] = None   # storage or vram capacity
    capacity_label: str = ""          # "1TB" | "128GB" | "12GB"
    kit_penalty: int = 0              # rubles deducted for poor kit
    kit_bonus: int = 0                # rubles added for a rich kit
    kit_label: str = "не указан"
    notes: list[str] = field(default_factory=list)

    @property
    def assembly(self) -> str:
        """Human readable config, e.g. 'PS4 Pro / 1TB [2 геймпада]'."""
        parts = []
        family = self.model.upper().replace("_", " ")
        rev = self.revision.capitalize()
        if rev in {"Pro", "Slim", "Fat"}:
            parts.append(f"{family} {rev}")
        else:
            parts.append(family)
        if self.capacity_label:
            parts.append(self.capacity_label)
        label = " / ".join(parts)
        if self.kit_label and self.kit_label != "не указан":
            label += f" [{self.kit_label}]"
        return label or "Конфигурация не определена"

    @property
    def market_key(self) -> str:
        """Strict grouping key: same model+revision, same capacity."""
        key = "::".join([self.model, self.revision])
        if self.memory_gb is not None:
            key += f"::{self.memory_gb}"
        return key


def _normalize_capacity(raw: int) -> Optional[int]:
    return _CAPACITY_GB.get(raw)


def _parse_storage_gb(text: str) -> Optional[int]:
    """Return capacity in GB from storage patterns, or None."""
    best: Optional[int] = None
    for match in _STORAGE_RE.finditer(text):
        num_raw, unit_raw = match.group(1), match.group(2)
        try:
            num = int(num_raw)
        except ValueError:
            continue
        if unit_raw.lower() in {"тб", "tb", "терабайт"}:
            num *= 1024
        canon = _normalize_capacity(num)
        if canon is None:
            continue
        best = canon  # last (largest) wins
    return best


def _parse_gpu_vram(text: str) -> Optional[int]:
    m = _GPU_VRAM_RE.search(text)
    if m:
        canon = _normalize_capacity(int(m.group(1)))
        if canon:
            return canon
    return None


def _detect_gpu(text: str) -> tuple[str, str]:
    for m in _GPU_RE.finditer(text):
        series = m.group(1).lower()
        number = m.group(3)
        model = f"{series}_{number}"
        return model, ""
    return "", ""


def _detect_console(text: str) -> tuple[str, str]:
    m = _PS_RE.search(text)
    if m:
        gen = m.group(1)
        rev = (m.group(2) or "fat" if gen == "4" else m.group(2) or "")
        rev = rev.lower()
        if gen == "4" and rev not in {"pro", "slim", "fat"}:
            rev = "fat"
        return "ps" + gen, rev
    m = _XBOX_RE.search(text)
    if m:
        tag = m.group(1) or "xbox"
        return "xbox_" + tag.strip().replace(" ", "_"), ""
    return "", ""


def _capacity_label(gb: Optional[int]) -> str:
    if gb is None:
        return ""
    if gb >= 1024:
        return f"{gb // 1024}TB"
    return f"{gb}GB"


def _analyze_kit(text: str) -> tuple[int, str, int]:
    """Return (bonus, label, penalty) based on completeness keywords."""
    # A bare-console hint is a penalty regardless of any listed extras.
    for label, penalty, pat in _KIT_PENALTY:
        if pat.search(text):
            return -penalty, label, penalty

    if _NO_KIT_HINT.search(text):
        return -1200, "без комплекта", 1200

    total_bonus = 0
    labels: list[str] = []
    for label, value, pat in _KIT_KEYWORDS:
        if pat.search(text):
            total_bonus += value
            labels.append(label)
    if labels:
        return total_bonus, "+".join(labels), 0
    return 0, "не указан", 0


def normalize_lot(title: str, extra: str = "", *, model_hint: Optional[str] = None) -> NormalizedLot:
    """Normalize a listing title (+ optional characteristics text).

    ``model_hint`` may be a parser-extracted model (from structured params)
    to disambiguate when the title alone is ambiguous.
    """
    text = f"{title} {extra}".strip()
    gpu_model, gpu_rev = _detect_gpu(text)
    ps_model, ps_rev = _detect_console(text)

    lot = NormalizedLot(title=title.strip())

    # Disambiguation: a detected GPU takes precedence over a console pattern
    # if the title explicitly mentions a GPU family.
    if gpu_model:
        lot.model = gpu_model
        lot.revision = gpu_rev or ""
        vram = _parse_gpu_vram(text)
        if vram is not None:
            lot.memory_gb = vram
            lot.capacity_label = _capacity_label(vram)
        else:
            # fall back to storage-like capacity anyway
            storage = _parse_storage_gb(text)
            if storage is not None:
                lot.memory_gb = storage
                lot.capacity_label = _capacity_label(storage)
    elif ps_model or (model_hint or "").startswith("ps"):
        lot.model = ps_model or model_hint
        lot.revision = ps_rev or (model_hint and model_hint.split(":")[-1]) or ""
        storage = _parse_storage_gb(text)
        if storage is not None:
            lot.memory_gb = storage
            lot.capacity_label = _capacity_label(storage)
    else:
        # generic device: use any storage hint
        storage = _parse_storage_gb(text)
        if storage is not None:
            lot.memory_gb = storage
            lot.capacity_label = _capacity_label(storage)
        if model_hint:
            lot.model = model_hint

    if lot.model == "unknown" and not model_hint:
        lot.notes.append("модель не определена")

    kit_bonus, kit_label, kit_penalty = _analyze_kit(text)
    lot.kit_bonus = kit_bonus
    lot.kit_penalty = kit_penalty
    lot.kit_label = kit_label
    return lot