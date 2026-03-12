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


class AdRequestType(StrEnum):
    UNSET = "unset"
    SINGLE_PRODUCT = "single_product"
    MULTI_PRODUCT = "multi_product"
    STORE_GENERAL = "store_general"


class ClassificationStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class PendingQuestionType(StrEnum):
    NONE = "none"
    ONBOARDING = "onboarding"
    CLASSIFICATION = "classification"
    PRODUCT_CONFIRMATION = "product_confirmation"
    CANDIDATE_SELECTION = "candidate_selection"
    MISSING_INFO = "missing_info"
    GENERATION_RETRY = "generation_retry"


class DraftProductStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class AdVariantRoundStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    PUBLISHED = "published"


class AdVariantStatus(StrEnum):
    VALID = "valid"
    FAILED = "failed"
