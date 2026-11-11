"""Identity confidence: is the item what the listing CLAIMS it is?

Extracts the claimed identity from the title (brand, model codes, chip, RAM, storage, screen size, lens mount, focal length) and
checks it against the description and any structured spec fields. A listing is only `verified` (high confidence) when the title names a
specific model, nothing contradicts it, and a second source (description or spec fields) corroborates it. Everything else is named:

  contradicted    the description or spec fields name a different model or spec than the title
  mixed           another item's model/brand is mentioned (bundle or lot contents)
  unverifiable    the title names no model code, so there is nothing to match sold comps against
  medium          a model code in the title but no second source: claimed, not confirmed (set REQUIRE_CORROBORATION=False to accept)
"""
from __future__ import annotations

import re

from engine.title_cleaner import extract_brand

REQUIRE_CORROBORATION = True   # hard bar: a title alone is not enough to price an item at a specific model

_CODE = re.compile(r"\b([A-Za-z]{1,5})[- ]?(\d{2,5})([A-Za-z0-9]{0,3})\b")
_NOT_CODE_PREFIX = {"gb", "tb", "mb", "mm", "hz", "mhz", "ghz", "ct", "gen", "ddr", "usb", "hdmi", "ram", "ssd", "hdd", "wifi", "led", "lcd", "oled",
                    "ps", "xl", "rtx", "gtx", "core", "ryzen", "intel", "amd", "inch", "fps", "mp"}
_CHIP = re.compile(r"\bm([1-4])(?:\s*(pro|max|ultra))?\b", re.I)
_SCREEN = re.compile(r"\b(11|12\.9|13(?:\.3|\.6)?|14(?:\.2)?|15(?:\.3|\.6)?|16(?:\.2)?|17)\s?(?:\"|”|inch|in\b|-inch)", re.I)
_STORAGE = re.compile(r"\b(\d{2,4})\s?(gb|tb)\b", re.I)
_RAM = re.compile(r"\b(8|12|16|24|32|64)\s?gb\s*(?:ram|memory|unified)|\b(?:ram|memory)[: ]+(8|12|16|24|32|64)\s?gb", re.I)
_MOUNT = re.compile(r"\b(EF-S|EF-M|EF|RF-S|RF|FE|E[- ]mount|F[- ]mount|Z[- ]mount|X[- ]mount|M42|M43|MFT|micro four thirds|L[- ]mount|A[- ]mount|K[- ]mount|PL)\b", re.I)
_FOCAL = re.compile(r"\b(\d{2,3})\s?mm\b", re.I)
_BUNDLE = re.compile(r"\b(also includes?|comes with|included|includes|bundle|together with|plus|along with|as well as|and a)\b", re.I)


def _codes(text: str) -> set[str]:
    out = set()
    for pre, digits, suf in _CODE.findall(text or ""):
        if pre.lower() in _NOT_CODE_PREFIX or re.fullmatch(r"(19|20)\d\d", digits) and not pre:
            continue
        out.add(f"{pre.upper()}{digits}{suf.upper()}")
    return out


# products identified by a name and generation number rather than a model code: "iPhone 13 Pro", "Galaxy S22", "PlayStation 5"
_NAME = re.compile(r"\b(iphone|ipad|galaxy(?:\s+(?:s|a|z|note|tab))?|pixel|playstation|ps|xbox(?:\s+series)?|macbook\s+(?:pro|air)|airpods|apple\s+watch|switch)"
                   r"\s*(\d{1,2})\s*(pro\s*max|pro|max|plus|mini|ultra|se|xl|fe)?\b", re.I)


def _names(text: str) -> set[str]:
    out = set()
    for fam, num, suf in _NAME.findall(text or ""):
        out.add("~" + re.sub(r"\s+", "", fam).upper() + num + re.sub(r"\s+", "", suf).upper())
    return out


def _prefix(code: str) -> str:
    return re.match(r"~?[A-Z]+", code).group(0)


def _storage(text: str) -> int | None:
    best = 0
    for n, u in _STORAGE.findall(text or ""):
        v = int(n) * (1000 if u.lower() == "tb" else 1)
        if v >= 64:
            best = max(best, v)
    return best or None


def claims(text: str) -> dict:
    """The identity a piece of text claims. Missing facts are None; nothing is guessed."""
    chip = _CHIP.search(text or "")
    ram = _RAM.search(text or "")
    sc = _SCREEN.search(text or "")
    mount = _MOUNT.search(text or "")
    focal = _FOCAL.search(text or "")
    return {"brand": extract_brand(text or ""), "codes": sorted(_codes(text) | _names(text)),
            "chip": (f"m{chip.group(1)}" + (f" {chip.group(2).lower()}" if chip.group(2) else "")) if chip else None,
            "ram_gb": int(next(g for g in ram.groups() if g)) if ram else None, "storage_gb": _storage(text),
            "screen_in": sc.group(1).split(".")[0] if sc else None,
            "mount": re.sub(r"[- ]", "", mount.group(1)).upper() if mount else None, "focal_mm": int(focal.group(1)) if focal else None}


def parse_specs(raw) -> dict:
    """Optional structured spec fields from the importer: a dict, a JSON string or 'key=value;key=value'."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k).lower(): v for k, v in raw.items()}
    import json
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k).lower(): v for k, v in d.items()}
    except (TypeError, ValueError):
        pass
    return {k.strip().lower(): v.strip() for k, _, v in (p.partition("=") for p in str(raw).split(";")) if k.strip()}


def check(title: str, description: str = "", model_number: str = "", specs=None) -> dict:
    """Identity report: {'confidence': high|medium|low, 'verified': bool, 'claimed': {...}, 'contradictions': [...], 'risks': [...],
    'corroborated_by': [...], 'reason': text naming why it is not verified, 'next_step': what would resolve it}."""
    t, d = claims(title), claims(description)
    sp = parse_specs(specs)
    mn = (model_number or sp.get("model") or sp.get("model_number") or "").strip()
    mn_codes = _codes(mn) if mn else set()
    contradictions: list[str] = []
    risks: list[str] = []
    corro: list[str] = []
    tcodes, dcodes = set(t["codes"]), set(d["codes"]) | mn_codes

    # --- model numbers: SR7002 and SR7002U1B are the same model (a regional/variant suffix), SR7002 and SR6008 are not
    def related(x: str, y: str) -> bool:
        if x.startswith("~") or y.startswith("~"):  # a named generation ("iPhone 13") must match exactly: 13 and 13 Pro are different models
            return x == y
        return x.startswith(y) or y.startswith(x)

    tb = t["brand"]
    other_brand = extract_brand(re.sub(re.escape(tb), " ", description or "", flags=re.I)) if tb and description else (d["brand"] if description else None)
    if other_brand == tb:
        other_brand = None

    same = {tc for tc in tcodes if any(related(tc, dc) for dc in dcodes)}
    if same:
        corro.append("description/model field repeats model " + ", ".join(sorted(x.lstrip("~") for x in same)))
    for c in sorted(dcodes):
        if any(related(c, tc) for tc in tcodes):
            continue
        near = [tc for tc in tcodes if _prefix(tc) == _prefix(c)]
        if near:
            contradictions.append(f"title says model {near[0].lstrip('~')} but the description/model field says {c.lstrip('~')}")
        elif tcodes and other_brand:
            risks.append(f"description also names {c} ({other_brand}): bundle contents or a different item mixed in")
    # --- specs
    for key, label in (("chip", "chip"), ("ram_gb", "RAM"), ("storage_gb", "storage"), ("screen_in", "screen size"), ("mount", "lens mount"), ("focal_mm", "focal length")):
        tv = t[key]
        dv = d[key]
        sv = sp.get(key.replace("_gb", "").replace("_in", "")) or sp.get(key)
        if sv not in (None, ""):
            sv_norm = str(sv).lower().replace("gb", "").replace('"', "").replace(" ", "")
            tv_norm = str(tv).lower().replace(" ", "") if tv is not None else None
            if tv is not None and sv_norm != tv_norm and not (tv_norm and sv_norm.startswith(tv_norm)):
                contradictions.append(f"title says {label} {tv} but the spec field says {sv}")
            elif tv is not None:
                corro.append(f"spec field confirms {label} {tv}")
        if tv is not None and dv is not None:
            if str(tv) != str(dv):
                contradictions.append(f"title says {label} {tv} but the description says {dv}")
            else:
                corro.append(f"description confirms {label} {tv}")
    # --- other signals buried in the text
    import catalog
    import repair
    kind = catalog.style_of(title, "electronics")[1]  # a stated defect only counts as repairable if this item type has a known fix
    dc = repair.classify("", description or "", kind)["class"]
    tc = repair.classify(title, "", kind)["class"]
    if dc in ("parts_only", "untested_unknown", "repairable") and tc in ("working", "cosmetic"):
        risks.append(f"damage/untested status appears only in the description ({dc.replace('_', ' ')})")
    if description and _BUNDLE.search(description) and len(dcodes - tcodes) >= 1 and not any("bundle contents" in r for r in risks):
        risks.append("description lists other items with the one in the title: confirm what is included")
    if re.search(r"\b(lot|bundle|set of|assorted|collection)\b", title or "", re.I):
        risks.append("title is a lot/bundle, not a single model")

    # --- confidence
    if contradictions:
        conf, reason = "low", "contradicted: " + "; ".join(contradictions)
        step = "ask the seller which model it actually is (rear label photo), then re-import with a model_number"
    elif not tcodes and not mn_codes:
        conf, reason = "low", "unverifiable: the title names no model number to match sold comps against"
        step = "ask Muse for the exact model number (rear label photo) and re-import it in model_number"
    elif any("bundle" in r or "lot" in r or "mixed in" in r for r in risks):
        conf, reason = "low", "mixed: " + "; ".join(r for r in risks if "bundle" in r or "lot" in r or "mixed in" in r)
        step = "ask Muse to list each included item's model as separate rows, then re-import"
    elif corro or not REQUIRE_CORROBORATION:
        conf, reason, step = "high", None, None
    else:
        conf, reason = "medium", "not corroborated: only the title states the model; the description/spec fields neither confirm nor contradict it"
        step = "get a second source for the model (rear label text in model_number, or a description line that repeats it) and re-import"
    return {"confidence": conf, "verified": conf == "high", "claimed": t, "description_claims": d, "contradictions": contradictions, "risks": risks,
            "corroborated_by": corro, "reason": reason, "next_step": step}
