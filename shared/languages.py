"""Canonical language tags for content-derived subtitle labels and mux metadata."""

from langcodes import Language, standardize_tag


def language_tag(value, *, allow_unknown=False):
    """Accept registered language/script/region tags, not private or unspecified labels."""
    code = standardize_tag(value)
    language = Language.get(code)
    if (
        not language.is_valid()
        or language.extensions
        or language.private
        or language.extlangs
        or language.language in (None, "und", "mul", "zxx")
        or language.language.startswith("x-")
        or "qaa" <= language.language <= "qtz"
        or language.script in ("Zzzz", "Zyyy", "Zinh")
        or (language.script and "Qaaa" <= language.script <= "Qabx")
        or language.territory in ("AA", "ZZ")
        or (language.territory and ("QM" <= language.territory <= "QZ" or "XA" <= language.territory <= "XZ"))
    ):
        if allow_unknown and code == "und":
            return code
        raise ValueError(f"Invalid or undetermined language code: {value!r}")
    return code
