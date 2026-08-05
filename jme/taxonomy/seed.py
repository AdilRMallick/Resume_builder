"""Idempotent loader for data/taxonomy/skills.yaml into canonical_skill + skill_alias.

The YAML file is the source of truth. Running the seeder twice in a row is a no-op;
running it after editing the YAML reconciles exactly the added and removed aliases.

Two deliberate hard failures:

  * an alias claimed by two different skills aborts the load. Ambiguity here does not
    degrade gracefully, it silently mis-attributes requirement counts forever.
  * a skill whose name or alias normalizes to the empty string aborts the load.

One deliberate *non*-failure: a canonical skill that exists in the database but has been
removed from the YAML is reported as an orphan and left alone. `posting_requirement`,
`evidence_skill` and `match_citation` rows point at it; deleting it would rewrite
history to make an editing mistake look like a fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jme.logging import get_logger
from jme.models import CanonicalSkill, SkillAlias, SkillCategory
from jme.taxonomy.normalize import normalize
from jme.taxonomy.resolver import invalidate_cache

__all__ = [
    "DEFAULT_SEED_PATH",
    "MAX_SKILLS",
    "MIN_SKILLS",
    "SeedError",
    "SeedReport",
    "SkillSpec",
    "check_size",
    "load_specs",
    "seed",
    "validate_specs",
]

log = get_logger(__name__)

DEFAULT_SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "taxonomy" / "skills.yaml"

#: ARCHITECTURE.md section 3: "roughly 150 to 250 entries"
MIN_SKILLS = 150
MAX_SKILLS = 250

_ALLOWED_KEYS = {"name", "category", "is_actionable", "aliases", "notes"}


class SeedError(RuntimeError):
    """The seed file is not loadable. Always a data bug, never a runtime condition."""


@dataclass(frozen=True)
class SkillSpec:
    name: str
    category: SkillCategory
    is_actionable: bool = True
    aliases: tuple[str, ...] = ()
    notes: str | None = None

    @property
    def name_norm(self) -> str:
        return normalize(self.name)

    def desired_aliases(self) -> dict[str, str]:
        """norm -> display text, including the auto-generated identity alias."""
        out: dict[str, str] = {}
        for display in (self.name, *self.aliases):
            norm = normalize(display)
            if not norm:
                raise SeedError(f"{self.name!r}: alias {display!r} normalizes to the empty string")
            if len(norm) > 128:
                raise SeedError(f"{self.name!r}: alias {display!r} exceeds 128 chars normalized")
            out.setdefault(norm, str(display))
        return out


@dataclass
class SeedReport:
    skills_created: int = 0
    skills_updated: int = 0
    skills_unchanged: int = 0
    aliases_added: int = 0
    aliases_removed: int = 0
    aliases_retitled: int = 0
    orphan_skills: tuple[str, ...] = ()
    dry_run: bool = False
    changes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.skills_created
            or self.skills_updated
            or self.aliases_added
            or self.aliases_removed
            or self.aliases_retitled
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "skills_created": self.skills_created,
            "skills_updated": self.skills_updated,
            "skills_unchanged": self.skills_unchanged,
            "aliases_added": self.aliases_added,
            "aliases_removed": self.aliases_removed,
            "aliases_retitled": self.aliases_retitled,
            "orphan_skills": list(self.orphan_skills),
            "dry_run": self.dry_run,
        }


# --------------------------------------------------------------------------------------
# loading and validation
# --------------------------------------------------------------------------------------


def load_specs(path: str | Path | None = None) -> list[SkillSpec]:
    """Parse and validate the YAML seed file."""
    seed_path = Path(path) if path is not None else DEFAULT_SEED_PATH
    if not seed_path.exists():
        raise SeedError(f"seed file not found: {seed_path}")
    with seed_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "skills" not in raw:
        raise SeedError(f"{seed_path}: expected a mapping with a top-level 'skills' key")
    entries = raw["skills"]
    if not isinstance(entries, list) or not entries:
        raise SeedError(f"{seed_path}: 'skills' must be a non-empty list")

    specs: list[SkillSpec] = []
    for i, entry in enumerate(entries):
        specs.append(_parse_entry(entry, i, seed_path))
    validate_specs(specs)
    return specs


def _parse_entry(entry: object, index: int, seed_path: Path) -> SkillSpec:
    where = f"{seed_path.name}[{index}]"
    if not isinstance(entry, dict):
        raise SeedError(f"{where}: expected a mapping, got {type(entry).__name__}")
    unknown = set(entry) - _ALLOWED_KEYS
    if unknown:
        raise SeedError(f"{where}: unknown key(s) {sorted(unknown)}")

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SeedError(f"{where}: 'name' must be a non-empty string")
    name = name.strip()
    if len(name) > 128:
        raise SeedError(f"{where}: name {name!r} exceeds 128 chars")

    category_raw = entry.get("category")
    if not isinstance(category_raw, str):
        raise SeedError(f"{where} ({name}): 'category' is required")
    try:
        category = SkillCategory(category_raw.strip().lower())
    except ValueError as exc:
        valid = ", ".join(c.value for c in SkillCategory)
        raise SeedError(f"{where} ({name}): invalid category {category_raw!r}; expected {valid}") from exc

    is_actionable = entry.get("is_actionable", True)
    if not isinstance(is_actionable, bool):
        raise SeedError(f"{where} ({name}): 'is_actionable' must be true or false")

    aliases_raw = entry.get("aliases", []) or []
    if not isinstance(aliases_raw, list):
        raise SeedError(f"{where} ({name}): 'aliases' must be a list")
    aliases: list[str] = []
    for alias in aliases_raw:
        if isinstance(alias, bool) or not isinstance(alias, str | int | float):
            raise SeedError(f"{where} ({name}): alias {alias!r} must be a string")
        text = str(alias).strip()
        if not text:
            raise SeedError(f"{where} ({name}): empty alias")
        aliases.append(text)

    notes = entry.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise SeedError(f"{where} ({name}): 'notes' must be a string")

    spec = SkillSpec(
        name=name,
        category=category,
        is_actionable=is_actionable,
        aliases=tuple(aliases),
        notes=notes,
    )
    spec.desired_aliases()  # surfaces empty/oversized aliases at parse time
    return spec


def validate_specs(specs: Sequence[SkillSpec]) -> None:
    """Duplicate names and ambiguous aliases are data bugs worth crashing on."""
    seen_names: dict[str, str] = {}
    dupes: list[str] = []
    for spec in specs:
        key = spec.name_norm
        if key in seen_names:
            dupes.append(f"{spec.name!r} duplicates {seen_names[key]!r}")
        else:
            seen_names[key] = spec.name
    if dupes:
        raise SeedError("duplicate skill names: " + "; ".join(sorted(dupes)))

    owner: dict[str, str] = {}
    clashes: list[str] = []
    for spec in specs:
        for norm in spec.desired_aliases():
            prior = owner.get(norm)
            if prior is not None and prior != spec.name:
                clashes.append(f"{norm!r} claimed by both {prior!r} and {spec.name!r}")
            else:
                owner[norm] = spec.name
    if clashes:
        raise SeedError("ambiguous aliases: " + "; ".join(sorted(clashes)))


def check_size(specs: Sequence[SkillSpec]) -> None:
    """Guard the curation target from ARCHITECTURE.md section 3."""
    if not MIN_SKILLS <= len(specs) <= MAX_SKILLS:
        raise SeedError(
            f"taxonomy has {len(specs)} skills; expected between {MIN_SKILLS} and {MAX_SKILLS}"
        )


# --------------------------------------------------------------------------------------
# seeding
# --------------------------------------------------------------------------------------


def seed(
    session: Session,
    specs: Iterable[SkillSpec] | None = None,
    *,
    path: str | Path | None = None,
    dry_run: bool = False,
) -> SeedReport:
    """Load or refresh the taxonomy. Idempotent: a second run reports zero changes."""
    spec_list = list(specs) if specs is not None else load_specs(path)
    validate_specs(spec_list)

    report = SeedReport(dry_run=dry_run)

    existing_skills = {s.name: s for s in session.scalars(select(CanonicalSkill)).all()}
    spec_names = {s.name for s in spec_list}
    report.orphan_skills = tuple(sorted(set(existing_skills) - spec_names))

    # --- skills -------------------------------------------------------------------
    created: list[CanonicalSkill] = []
    for spec in spec_list:
        row = existing_skills.get(spec.name)
        if row is None:
            report.skills_created += 1
            report.changes.append(f"+ skill {spec.name} ({spec.category.value})")
            if not dry_run:
                row = CanonicalSkill(
                    name=spec.name,
                    category=spec.category,
                    is_actionable=spec.is_actionable,
                    notes=spec.notes,
                )
                session.add(row)
                created.append(row)
                existing_skills[spec.name] = row
            continue
        diffs = []
        if row.category != spec.category:
            diffs.append(f"category {row.category.value} -> {spec.category.value}")
        if row.is_actionable != spec.is_actionable:
            diffs.append(f"is_actionable {row.is_actionable} -> {spec.is_actionable}")
        if (row.notes or None) != (spec.notes or None):
            diffs.append("notes")
        if diffs:
            report.skills_updated += 1
            report.changes.append(f"~ skill {spec.name}: {', '.join(diffs)}")
            if not dry_run:
                row.category = spec.category
                row.is_actionable = spec.is_actionable
                row.notes = spec.notes
        else:
            report.skills_unchanged += 1

    if created:
        session.flush()  # ids required before inserting aliases

    # --- aliases ------------------------------------------------------------------
    skill_ids = {name: row.id for name, row in existing_skills.items() if row.id is not None}
    existing_aliases = {
        alias.alias_norm: alias for alias in session.scalars(select(SkillAlias)).all()
    }
    managed_ids = {skill_ids[s.name] for s in spec_list if s.name in skill_ids}

    desired: dict[str, tuple[str, str]] = {}  # norm -> (display, skill name)
    for spec in spec_list:
        for norm, display in spec.desired_aliases().items():
            desired[norm] = (display, spec.name)

    to_delete: list[SkillAlias] = []
    for norm, alias in existing_aliases.items():
        want = desired.get(norm)
        if want is not None:
            # the YAML claims this alias; drop the row only if it points at the wrong skill
            if skill_ids.get(want[1]) != alias.canonical_skill_id:
                to_delete.append(alias)
                report.aliases_removed += 1
                report.changes.append(f"- alias {norm!r} (moving to {want[1]})")
            continue
        if alias.canonical_skill_id in managed_ids:
            # a managed skill lost this alias in the YAML
            to_delete.append(alias)
            report.aliases_removed += 1
            report.changes.append(f"- alias {norm!r}")
        # otherwise it belongs to an orphan skill; leave it alone

    if to_delete and not dry_run:
        session.execute(
            delete(SkillAlias).where(SkillAlias.id.in_([a.id for a in to_delete]))
        )
        session.flush()
        for alias in to_delete:
            existing_aliases.pop(alias.alias_norm, None)

    stale_norms = {a.alias_norm for a in to_delete}
    for norm, (display, skill_name) in sorted(desired.items()):
        current = existing_aliases.get(norm)
        if current is not None and norm not in stale_norms:
            if current.alias != display:
                report.aliases_retitled += 1
                report.changes.append(f"~ alias {norm!r} display {current.alias!r} -> {display!r}")
                if not dry_run:
                    current.alias = display
            continue
        report.aliases_added += 1
        report.changes.append(f"+ alias {norm!r} -> {skill_name}")
        if not dry_run:
            skill_id = skill_ids.get(skill_name)
            if skill_id is None:  # pragma: no cover - only reachable if flush failed
                raise SeedError(f"no id for skill {skill_name!r}")
            session.add(SkillAlias(canonical_skill_id=skill_id, alias=display, alias_norm=norm))

    if not dry_run:
        session.flush()
        invalidate_cache()

    log.info("taxonomy.seeded", **report.as_dict())
    return report
