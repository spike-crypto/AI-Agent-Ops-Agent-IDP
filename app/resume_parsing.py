"""Small, dependency-free helpers for turning an uploaded resume into a candidate."""

import hashlib
import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
# Words that show up in resume file names but are never part of a person's name.
_FILE_NOISE = {
    "naukri", "resume", "cv", "profile", "updated", "final", "latest", "new", "copy", "linkedin",
    "indeed", "monster", "shine", "biodata", "doc", "pdf", "my", "of",
}
# Lines that look like two to four plain words but are headings or job titles, not names.
_NOT_A_NAME = {
    "resume", "curriculum", "vitae", "profile", "summary", "objective", "contact", "experience",
    "education", "skills", "developer", "engineer", "analyst", "designer", "manager", "intern",
    "consultant", "specialist", "scientist", "architect", "lead", "senior", "junior", "full", "stack",
    "software", "data", "graphic", "personal", "details", "information",
}


def find_email(text: str) -> str:
    match = _EMAIL.search(text)
    return match.group(0).lower() if match else ""


def _tidy(name: str) -> str:
    # Keep the person's own capitalisation unless it is ALL CAPS or all lower case.
    return name.title() if name.isupper() or name.islower() else name


def guess_name(filename: str, text: str) -> str:
    """Best guess at the candidate's name: a name-like line at the top of the resume,
    otherwise the file name with job-board prefixes and tags like [4Y 0M] removed."""
    for raw in text.splitlines()[:6]:
        line = re.split(r"\s[-|–,]\s|\s{2,}|\t", raw.strip())[0].strip()
        words = line.split()
        if (2 <= len(words) <= 4 and len(line) <= 40
                and all(w.replace(".", "").replace("'", "").isalpha() for w in words)
                and not any(w.lower().strip(".") in _NOT_A_NAME for w in words)):
            return _tidy(line)

    stem = filename.rsplit(".", 1)[0]
    stem = _BRACKETS.sub(" ", stem)
    stem = re.sub(r"[_\-.+]+", " ", stem)
    stem = re.sub(r"\d+", " ", stem)
    words = [w for w in stem.split() if w.lower() not in _FILE_NOISE]
    name = " ".join(words).strip()
    return _tidy(name)[:80] if name else "Unnamed candidate"


def fingerprint(text: str) -> str:
    """Same resume text (ignoring spacing and case) gives the same fingerprint."""
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
