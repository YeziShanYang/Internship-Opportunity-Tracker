"""JSON codec for the stage artifacts, hand-rolled and about seventy lines.

Deliberately not pydantic. That is four packages becoming six, one of them a compiled
wheel, in order to validate files this same process wrote two hundred milliseconds
earlier. Deliberately not bare `dataclasses.asdict` either: it has no inverse, and it
turns a `tuple` into a `list`, which breaks equality on the `frozen=True` shapes and so
breaks the round-trip test that is the only thing making this module trustworthy.

Three properties earn their place:

* **A version mismatch is a hard error.** A stale `.run/` written before a field was
  renamed must fail loudly. Reading it as a partial answer is the same failure as a
  source that goes quiet instead of failing (spec 10.1) -- and here it would be worse,
  because a half-decoded artifact looks exactly like a day on which nothing changed.
* **A kind mismatch is a hard error too.** The artifacts are small files in one
  directory with similar shapes; `run.py render` handed `enriched.json` should say so
  rather than decode it into an empty digest.
* **Output is stable.** `sort_keys=True` with a fixed indent means field order can never
  produce a whole-file diff, and `ensure_ascii=False` keeps the emoji headings and the
  homoglyph canaries legible in the file rather than as `\\uA4DF` escapes.
"""
from __future__ import annotations

import dataclasses
import json
import types
import typing

# Bump when any artifact shape changes in a way that is not additive-with-a-default.
# Every artifact carries it, and reading an older one is an error rather than a guess.
VERSION = 3


class ArtifactError(RuntimeError):
    """A stored artifact cannot be trusted: wrong version, wrong kind, or wrong shape."""


def encode(value):
    """A dataclass tree down to JSON-native values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: encode(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ArtifactError(
        f"{type(value).__name__} has no JSON form; artifacts carry data, not behaviour"
    )


def decode(hint, value):
    """JSON-native values back up into `hint`, which may be a dataclass or a generic."""
    origin = typing.get_origin(hint)
    if origin is None:
        if dataclasses.is_dataclass(hint):
            if not isinstance(value, dict):
                raise ArtifactError(f"expected an object for {hint.__name__}, got {type(value).__name__}")
            hints = typing.get_type_hints(hint)
            missing = [f.name for f in dataclasses.fields(hint) if f.name not in value]
            if missing:
                raise ArtifactError(
                    f"{hint.__name__} is missing {missing} -- the artifact was written by "
                    f"a different shape than this code expects, so bump codec.VERSION "
                    f"rather than filling in defaults"
                )
            return hint(**{
                field.name: decode(hints[field.name], value[field.name])
                for field in dataclasses.fields(hint)
            })
        return value  # str, int, float, bool, typing.Any
    args = typing.get_args(hint)
    if origin is list:
        return [decode(args[0], item) for item in value]
    if origin is tuple:
        # Only the homogeneous `tuple[X, ...]` form is used, and it must come back as a
        # tuple: a list would compare unequal on every frozen dataclass that holds one.
        return tuple(decode(args[0], item) for item in value)
    if origin is dict:
        return {key: decode(args[1], item) for key, item in value.items()}
    if origin in (types.UnionType, typing.Union):
        if value is None:
            return None
        real = [arg for arg in args if arg is not type(None)]
        return decode(real[0], value)
    return value


def dumps(kind: str, obj) -> str:
    """One artifact as text, with its envelope. Ends in a newline, like every other
    file this repo writes, so `git diff` never reports "\\ No newline at end of file"."""
    return json.dumps(
        {"version": VERSION, "kind": kind, "payload": encode(obj)},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def loads(kind: str, cls, text: str):
    """The inverse of `dumps`. Raises ArtifactError rather than returning a partial."""
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{kind}: not valid JSON ({exc})") from exc
    if not isinstance(envelope, dict):
        raise ArtifactError(f"{kind}: expected an envelope object")
    found = envelope.get("version")
    if found != VERSION:
        raise ArtifactError(
            f"{kind}: artifact is version {found!r} but this code writes version "
            f"{VERSION}. Re-run the stage that produces it; a stale artifact must not "
            f"be read as a quiet day."
        )
    if envelope.get("kind") != kind:
        raise ArtifactError(
            f"expected a {kind!r} artifact, found {envelope.get('kind')!r}"
        )
    if "payload" not in envelope:
        raise ArtifactError(f"{kind}: envelope carries no payload")
    return decode(cls, envelope["payload"])
