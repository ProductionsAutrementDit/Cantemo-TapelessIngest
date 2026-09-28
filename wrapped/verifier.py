"""Post-apply checks. An empty list means the item is what the plan says."""

from typing import List

from portal.plugins.TapelessIngest.wrapped import fields


def verify_item(row, gateway) -> List[str]:
    plan, rollback = row.plan, row.rollback
    problems: List[str] = []
    shapes = gateway.original_shapes(row.item_id)
    if len(shapes) != 1:
        return [f"{len(shapes)} original shapes, expected 1"]
    (shape,) = shapes
    if shape.shape_id != plan["new_shape_id"]:
        problems.append(
            f"original shape is {shape.shape_id}, not {plan['new_shape_id']}"
        )
    expected = {o["file_id"] for o in plan["originals"]}
    if shape.file_ids() != expected:
        problems.append(
            f"original shape files {sorted(shape.file_ids())} != {sorted(expected)}"
        )
    by_file = {o["file_id"]: o for o in plan["originals"]}
    for component in shape.components:
        original = by_file.get(component.files[0].file_id) if component.files else None
        if not original or not original.get("entry"):
            continue
        written = gateway.component_metadata(
            row.item_id, shape.shape_id, component.component_id
        ).get(fields.EXTERNAL_ID_FIELD)
        if written != original["entry"]["handle"]:
            problems.append(f"component {component.component_id} handle is {written!r}")
    lowres = gateway.shape_ids(row.item_id, fields.LOWRES_TAG)
    if set(lowres) != set(rollback.get("lowres_shape_ids", [])):
        problems.append(f"lowres shapes {lowres} != {rollback.get('lowres_shape_ids')}")
    duration = gateway.item_fields(row.item_id, [fields.DURATION_FIELD])
    before = rollback.get("item_fields", {}).get(fields.DURATION_FIELD)
    if duration.get(fields.DURATION_FIELD) != before:
        problems.append(
            f"durationSeconds {duration.get(fields.DURATION_FIELD)} != {before}"
        )
    return problems
