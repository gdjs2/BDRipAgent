"""Movie language identity, independent from inherited source-track flags."""

from shared.languages import language_tag


def canonical_languages(values):
    result = []
    for value in values or []:
        try:
            code = language_tag(value)
        except (ValueError, TypeError, AttributeError):
            continue
        if code not in result:
            result.append(code)
    return result


def movie_languages(analysis, shared=None, metadata=None):
    """Use confirmed source choices, explicit movie metadata, then discovery evidence."""
    shared = shared or {}
    confirmed = canonical_languages(shared.get("original_languages"))
    if confirmed:
        return confirmed
    if "original_languages" not in shared:
        confirmed = canonical_languages(analysis.get("original_languages"))
        if confirmed:
            return confirmed
    explicit = canonical_languages(analysis.get("subtitle_discovery_policy", {}).get("original_languages"))
    if explicit:
        return explicit
    report = shared.get("subtitle_discovery") or analysis.get("subtitle_discovery") or {}
    return canonical_languages(report.get("original_languages")) or canonical_languages(
        (metadata or {}).get("original_languages")
    )


def language_identity(code):
    language = language_tag(code).split("-")[0]
    # CLDR treats the Mandarin code as an alias of the usual Chinese tag.
    # Cantonese remains yue; do not merge all Chinese macrolanguage members.
    return "zh" if language == "cmn" else language


def is_original(track, originals):
    if track.get("kind") == "video":
        return True
    try:
        # Scripts and territories describe variants, not dubbed languages. Keep
        # distinct languages (e.g. Cantonese yue and Mandarin zh) distinct.
        language = language_identity(track.get("language", "und"))
    except (ValueError, TypeError, AttributeError):
        return False
    return any(language == language_identity(code) for code in canonical_languages(originals))
