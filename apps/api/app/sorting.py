"""Controlled list sorting for paginated list endpoints (V1.1 / T95).

Each paginated endpoint declares a :class:`SortSpec` — the whitelist of fields
a client may sort by plus the documented default. An unknown field is rejected
with 422 (``validation_error``) so a client typo fails loudly instead of
silently falling back to server order. Multi-field sorts are supported
(comma-separated, ``-`` prefix for descending), and the primary key is
implicitly appended as the final tiebreaker: offset pagination over a
non-unique sort key otherwise skips or repeats rows across pages.

``resolve`` returns ``None`` when the client did not ask for a sort — routes
then leave the repository's own default ordering untouched, which keeps the
no-parameter response byte-for-byte identical to the pre-T95 behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass


class SortValidationError(ValueError):
    """Raised when a client asks for a field outside the declared whitelist."""


@dataclass(frozen=True)
class SortSpec:
    """A list endpoint's sortable-field contract.

    ``allowed`` is the whitelist (exact field names, ``id`` excluded — the
    primary key is a tiebreaker, not a headline sort). ``default`` is the
    documented default expressed in the same wire syntax (e.g. ``-created_at``),
    kept for documentation and for clients that want to pass it explicitly.
    ``pk_desc`` is the direction the implicit primary-key tiebreaker takes,
    matching the endpoint's legacy default tie direction.
    """

    allowed: tuple[str, ...]
    default: str
    pk: str = "id"
    pk_desc: bool = False

    def resolve(self, raw: str | None) -> tuple[tuple[str, bool], ...] | None:
        """Parse the ``sort`` query value into ``(field, descending)`` clauses.

        ``None`` (parameter absent) resolves to ``None`` — the caller keeps its
        existing default ordering. Anything unparsable or outside the whitelist
        raises :class:`SortValidationError`, which the route maps to 422.

        The implicit primary-key tiebreaker uses ``pk_desc`` — matching the
        direction the endpoint's legacy default used — so passing the documented
        ``default`` string explicitly reproduces the no-parameter order exactly,
        ties included.
        """
        if raw is None:
            return None
        clauses: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for token in raw.split(","):
            field = token.strip()
            descending = field.startswith("-")
            if descending:
                field = field[1:]
            if not field:
                raise SortValidationError("sort contains an empty field")
            if field == self.pk:
                raise SortValidationError(
                    f"{self.pk!r} is the implicit tiebreaker and is always appended"
                )
            if field not in self.allowed:
                raise SortValidationError(
                    f"field {field!r} is not sortable; allowed fields: "
                    + ", ".join(self.allowed)
                )
            if field in seen:
                raise SortValidationError(f"field {field!r} appears more than once")
            seen.add(field)
            clauses.append((field, descending))
        clauses.append((self.pk, self.pk_desc))
        return tuple(clauses)


# The endpoint sort contracts (V1.1 / T95). Each paginated list endpoint binds
# one of these; the whitelist mirrors fields a listing UI plausibly orders by,
# and the default documents the pre-existing server order in the same wire
# syntax. ``id`` is deliberately excluded from ``allowed``: it is the implicit
# tiebreaker, not a headline sort. ``pk_desc`` mirrors the primary-key
# direction each endpoint's legacy default already used, so an explicit
# ``sort=-created_at`` is indistinguishable from no parameter at all.
AUDIT_LOG_SORT = SortSpec(
    allowed=("created_at", "actor_id", "action", "entity_type", "project_id"),
    default="-created_at",
    pk_desc=True,
)
IDEMPOTENCY_KEY_SORT = SortSpec(
    allowed=("created_at", "state", "actor_id"),
    default="-created_at",
    pk_desc=True,
)
API_KEY_SORT = SortSpec(
    allowed=("created_at", "name", "status", "actor_id"),
    default="-created_at",
    pk_desc=True,
)
WEBHOOK_SUBSCRIPTION_SORT = SortSpec(
    allowed=("created_at", "name", "status"),
    default="created_at",
)
WEBHOOK_DELIVERY_SORT = SortSpec(
    allowed=("created_at", "status"),
    default="-created_at",
    pk_desc=True,
)
EXTERNAL_INTEGRATION_SORT = SortSpec(
    allowed=("created_at", "name", "status"),
    default="created_at",
)
