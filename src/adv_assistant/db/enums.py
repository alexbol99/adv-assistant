from enum import StrEnum


class Language(StrEnum):
    HE = "he"
    EN = "en"
    RU = "ru"
    AR = "ar"


class AdDraftStatus(StrEnum):
    DRAFT = "DRAFT"
    GENERATING = "GENERATING"
    PREVIEW_READY = "PREVIEW_READY"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
