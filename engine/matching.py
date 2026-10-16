"""Title cleaning and comp relevance filtering."""
import re

NOISE = {
    "mint", "tested", "obo", "l@@k", "look", "wow", "rare", "nice", "great", "clean",
    "working", "works", "excellent", "beautiful", "amazing", "free", "shipping", "fast",
    "ship", "read", "description", "as-is", "as", "is", "lqqk", "nr", "no", "reserve",
    "vintage", "used", "new", "the", "a", "an", "and", "with", "for", "in", "of", "w/",
    "only", "must", "see", "pics", "pictures", "good", "condition", "great", "sale",
    "super", "very", "lot", "of",
}
MAX_QUERY_TOKENS = 7


def _tokens(title: str) -> list[str]:
    title = re.sub(r"[\(\)\[\]\{\}!*,;:|~]", " ", title.lower())
    title = title.replace("&", " ")
    return [t.strip(".-_/") for t in title.split() if t.strip(".-_/")]


def clean_title(title: str) -> str:
    """Strip seller fluff, keeping brand/model tokens (order preserved)."""
    out: list[str] = []
    for t in _tokens(title):
        if t in NOISE or re.fullmatch(r"[!@#$%^&*_=+-]+", t) or re.fullmatch(r"l[@q]+k", t):
            continue
        if t not in out:
            out.append(t)
    return " ".join(out)


def build_query(title: str) -> str:
    return " ".join(clean_title(title).split()[:MAX_QUERY_TOKENS])


def is_relevant(query: str, comp_title: str, min_overlap: float = 0.6) -> bool:
    """Keep a comp only if it shares most query tokens (drops accessories/parts hits)."""
    q = set(query.split())
    if not q:
        return False
    c = set(_tokens(comp_title))
    if (q & c) == set():
        return False
    return len(q & c) / len(q) >= min_overlap
