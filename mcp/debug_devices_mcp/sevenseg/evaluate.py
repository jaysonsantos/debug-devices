"""The agreement of the local decoder with the vision model over the compare-mode dataset.

Each field (digits, point, sign, unit, mode) counts only the frames that both read. `redecode` decodes the saved LCD
images again with the layout of the current profile, so a region change can be checked without new vision calls. The
LCD images are already warped: a change of the LCD corners needs new frames. An entry without an image keeps its
saved local reading.
"""

import cv2
from pydantic import BaseModel

from debug_devices_mcp.sevenseg.compare import COMPARED_FIELDS, Field, field_values
from debug_devices_mcp.sevenseg.dataset import Dataset, DatasetEntry
from debug_devices_mcp.sevenseg.decode import decode_lcd, fit_lcd
from debug_devices_mcp.sevenseg.profile import MeterProfile


class FieldAgreement(BaseModel):
    field: Field
    compared: int
    agreed: int

    @property
    def rate(self) -> float | None:
        return self.agreed / self.compared if self.compared else None


class Disagreement(BaseModel):
    entry_id: str
    field: Field
    vision: str
    local: str
    vision_text: str
    local_text: str


class Evaluation(BaseModel):
    entries: int
    both_readable: int
    fields: list[FieldAgreement]
    # Every compared field agrees (both readable).
    all_agree: int
    disagreements: list[Disagreement]


def evaluate(pairs: list[DatasetEntry]) -> Evaluation:
    counts = {name: [0, 0] for name in (Field.READABLE, *COMPARED_FIELDS)}
    disagreements = []
    both = 0
    all_agree = 0
    for entry in pairs:
        values = field_values(entry.vision, entry.local)
        if entry.vision.readable and entry.local.readable:
            both += 1
            all_agree += all(seen == read for seen, read in values.values())
        for name, (seen, read) in values.items():
            counts[name][0] += 1
            counts[name][1] += seen == read
            if seen != read:
                disagreements.append(
                    Disagreement(
                        entry_id=entry.entry_id,
                        field=name,
                        vision=seen,
                        local=read,
                        vision_text=f"{entry.vision.display_text} {entry.vision.unit} {entry.vision.mode}",
                        local_text=f"{entry.local.display_text} {entry.local.unit} {entry.local.mode}",
                    )
                )
    return Evaluation(
        entries=len(pairs),
        both_readable=both,
        fields=[
            FieldAgreement(field=name, compared=compared, agreed=agreed) for name, (compared, agreed) in counts.items()
        ],
        all_agree=all_agree,
        disagreements=disagreements,
    )


def load_entries(dataset: Dataset, profile: MeterProfile | None = None) -> tuple[list[DatasetEntry], int]:
    """The saved entries, and how many were decoded again (with a profile: each saved LCD image)."""
    entries = []
    redecoded = 0
    for saved, image_path in dataset.entries():
        lcd = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE) if profile is not None and image_path else None
        if profile is not None and lcd is not None:
            decoded = decode_lcd(fit_lcd(lcd, profile.layout), profile.layout)
            entries.append(saved.model_copy(update={"local": decoded.reading, "regions": decoded.regions}))
            redecoded += 1
        else:
            entries.append(saved)
    return entries, redecoded


def report(evaluation: Evaluation, max_disagreements: int) -> str:
    lines = [f"entries: {evaluation.entries}, both readable: {evaluation.both_readable}"]
    if evaluation.both_readable:
        share = evaluation.all_agree / evaluation.both_readable
        lines.append(f"all fields agree: {evaluation.all_agree}/{evaluation.both_readable} ({share:.0%})")
    for item in evaluation.fields:
        rate = "-" if item.rate is None else f"{item.rate:.0%}"
        lines.append(f"  {item.field:<9} {item.agreed}/{item.compared} ({rate})")
    shown = evaluation.disagreements[:max_disagreements]
    if shown:
        lines.append(f"disagreements ({len(shown)} of {len(evaluation.disagreements)}):")
        lines += [
            f"  {item.entry_id} {item.field}: vision {item.vision!r} ({item.vision_text}), "
            f"local {item.local!r} ({item.local_text})"
            for item in shown
        ]
    return "\n".join(lines)
