"""Deterministic title rules (no LLM): is a title in scope, and what seniority/function is it.

In scope: C-level (incl. President and General Counsel, D4), founders, VPs, and Heads.
Out of scope: board-only roles, plain "Director", managers, individual contributors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_C_ACRONYMS = {
    "ceo": "other",
    "cfo": "finance",
    "cto": "engineering",
    "coo": "operations",
    "cmo": "marketing",
    "cpo": "product",
    "ciso": "security",
    "cso": "security",
    "cro": "sales",
    "clo": "legal",
    "chro": "people",
    "cio": "engineering",
    "cdo": "data",
    "cco": "legal",
}

# Checked in order; the first function whose pattern matches the title wins. Sales comes
# before engineering so "Head of Technology Partnerships" is sales, not engineering.
_FUNCTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("security", r"secur|trust|\bciso\b"),
    ("legal", r"legal|counsel|complian|regulat|corporate affairs|policy|privacy"),
    ("finance", r"financ|account|treasur|controller|investor relations|tax\b|\bfp&a\b"),
    ("people", r"\bpeople\b|human resources|\bhr\b|talent|recruit|culture|workplace"),
    ("data", r"\bdata\b|analytics|machine learning|\bai\b|artificial intelligence"),
    ("design", r"design|\bux\b|creative"),
    ("product", r"product"),
    ("marketing", r"marketing|brand|communications|\bpr\b|growth|content|community"),
    (
        "sales",
        r"sales|revenue|business development|partnership|commercial|go-to-market|\bgtm\b"
        r"|customer success|account",
    ),
    (
        "engineering",
        r"engineer|technolog|technical|information|infrastructure|platform|architect|\bit\b",
    ),
    ("operations", r"operat|customer|support|brokerage|strategy|business"),
)

_BOARD_ONLY = re.compile(
    r"^(?:independent |lead independent |non-executive )?(?:board )?"
    r"(?:director|board member|member of the board|chair(?:man|woman|person)?"
    r"(?: of the board)?)$"
)
_FORMER = re.compile(r"\b(?:former(?:ly)?|ex-|previously|retired|outgoing)\b")


@dataclass(frozen=True)
class TitleClass:
    seniority: str  # c_level | vp | head | founder
    function: str
    also_founder: bool = False  # e.g. "Co-founder & CEO": one c_level role plus a founder role


def _clean(title: str) -> str:
    t = title.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    t = " ".join(re.sub(r"[^a-z0-9&,'/\- ]", " ", t).split())
    # Plurals from shared titles: "Co-Founders and Co-CEOs" -> "co-founder and co-ceo"
    return re.sub(r"\b(founder|ceo|cfo|cto|coo|officer)s\b", r"\1", t)


def function_for(title: str) -> str:
    t = _clean(title)
    for function, pattern in _FUNCTION_PATTERNS:
        if re.search(pattern, t):
            return function
    return "other"


def mentions_former(title: str) -> bool:
    """'Former CFO', 'ex-VP', 'retired'. ('Interim' is a current role.)"""
    return bool(_FORMER.search(_clean(title)))


def strip_company(title: str, company_name: str | None) -> str:
    """Drop "<Company> <Word>" qualifiers ("Robinhood Financial", "Robinhood UK") so a
    subsidiary's name doesn't decide the function ("Financial" is not finance)."""
    if not company_name:
        return title
    name = re.escape(_clean(company_name).split(" ")[0])
    return " ".join(re.sub(rf"\b{name}(?:\s+[a-z]+)?\b", " ", title).split())


_VP_TOKEN = re.compile(r"\b(?:vice president|vp|svp|evp|avp)\b")
# "President of Robinhood UK", "President, Robinhood Financial": a subsidiary president.
_QUALIFIED_PRESIDENT = re.compile(r"\bpresident\s*(?:,|of\b)")


def classify_title(title: str, company_name: str | None = None) -> TitleClass | None:
    """Seniority and function for an in-scope leadership title, or None if out of scope.

    C-level means the company's own C-suite. A title that also carries a VP token ("Vice
    President and Chief Information Security Officer") or a subsidiary presidency ("President
    of Robinhood UK") is VP-level. General Counsel stays C-level (D4)."""
    full = _clean(title)
    if not full or _BOARD_ONLY.match(full):
        return None
    t = strip_company(full, company_name) or full
    founder = bool(re.search(r"\b(?:co-?)?founder\b", t))
    words = set(re.findall(r"[a-z]+", t))

    acronyms = [a for a in _C_ACRONYMS if a in words]
    chief = re.search(r"\bchief\b.*\bofficer\b", t)
    president = (
        re.search(r"(?<!vice )\bpresident\b", t)
        and not re.search(r"\bvice\b", t)
        and not _QUALIFIED_PRESIDENT.search(full)
    )
    counsel = re.search(r"\bgeneral counsel\b", t)
    if (chief or acronyms) and _VP_TOKEN.search(t) and not counsel:
        return TitleClass("vp", function_for(t.replace("chief executive officer", "")),
                          also_founder=founder)  # fmt: skip
    if not (chief or acronyms or president or counsel) and _QUALIFIED_PRESIDENT.search(full):
        return TitleClass("vp", function_for(t.replace("president", "")), also_founder=founder)
    if chief or acronyms or president or counsel:
        if chief:
            # "Chair and Chief Executive Officer" -> other; "Chief Legal ... Officer" -> legal
            function = function_for(t.replace("chief executive officer", ""))
        elif acronyms:
            function = _C_ACRONYMS[acronyms[0]]
        elif counsel:
            function = "legal"
        else:
            function = function_for(t.replace("president", ""))
        return TitleClass("c_level", function, also_founder=founder)

    if re.search(r"\b(?:vice president|vp|svp|evp|avp)\b", t):
        return TitleClass("vp", function_for(t), also_founder=founder)
    if re.search(r"\bhead\b", t):
        return TitleClass("head", function_for(t), also_founder=founder)
    if founder:
        return TitleClass("founder", function_for(t.replace("founder", "")))
    return None
