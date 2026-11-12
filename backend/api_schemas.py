"""Request contracts shared by the API domain routers."""
from typing import Literal
from pydantic import BaseModel, Field


class SettingsIn(BaseModel):
    openai_api_key: str = Field(min_length=8)


class ContextSettingsIn(BaseModel):
    enabled: bool
    use_ad: bool | None = None
    sync_ad: bool | None = None  # Backwards compatibility for already-open admin tabs.
    sync_time: str | None = Field(default=None, min_length=5, max_length=5, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class GlossaryImportIn(BaseModel):
    upload_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    mapping: dict[str, str] = Field(max_length=5)
    sheet: str = Field(default="", max_length=120)


class GlossaryEnabledIn(BaseModel):
    enabled: bool


class TermScheduleIn(BaseModel):
    enabled: bool
    run_time: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    max_batches: int = Field(ge=1, le=500)


class TermReviewItem(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)


class TermReviewIn(BaseModel):
    items: list[TermReviewItem] = Field(min_length=1, max_length=20)
    action: Literal['approve', 'reject', 'defer', 'reopen']
    canonical: str = Field(default='', max_length=240)
    description: str = Field(default='', max_length=4000)
    department: str = Field(default='', max_length=120)
    replace_text: bool = False


class DirectorySettingsIn(BaseModel):
    sync_ad: bool | None = None
    sync_time: str | None = Field(default=None, min_length=5, max_length=5, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class DirectoryPolicyIn(BaseModel):
    fields: list[str] = Field(max_length=3)
    domains: list[str] = Field(max_length=32)


class SpeakerSearchIn(BaseModel):
    mode: Literal["live", "cache"]


class PromptsIn(BaseModel):
    protocol_prompt: str = Field(min_length=1, max_length=30000)
    name_review_prompt: str = Field(min_length=1, max_length=30000)
    name_review_enabled: bool = True


class ModelsIn(BaseModel):
    chat_model: str | None = None
    asr_model: str | None = None


class ThemeIn(BaseModel):
    theme: str


class SmtpIn(BaseModel):
    host: str = ""
    port: int = Field(default=587, ge=1, le=65535)
    user: str = ""
    password: str = ""
    from_addr: str = ""
    starttls: bool = True
    public_url: str = ""


class QueueIn(BaseModel):
    max_jobs: int = Field(ge=1, le=8)


class LdapIn(BaseModel):
    enabled: bool = False
    url: str = ""
    bind_dn: str = ""
    bind_password: str = ""
    base_dn: str = ""
    user_filter: str = "(mail={username})"
    starttls: bool = False
    tls_verify: bool = True
    email_attr: str = "mail"


class DirectoryScopeIn(BaseModel):
    mode: Literal["all", "selected"]
    bases: list[str] = Field(default_factory=list, max_length=32)


class LdapTestIn(BaseModel):
    username: str = ""


class SpeakerNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str | None = Field(default=None, max_length=254)
    job_title: str | None = Field(default=None, max_length=200)


class ProtocolRecipientIn(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=40)
    email: str = Field(min_length=3, max_length=254)


class ProtocolIn(BaseModel):
    recipients: list[ProtocolRecipientIn] = Field(default_factory=list, max_length=200)


class SpeakerAssignIn(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=40)
    segment_indexes: list[int] = Field(min_length=1, max_length=500)


class SmtpTestIn(BaseModel):
    to: str = ""


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    setup_token: str = ""


class UserLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    archive_limit: int = Field(default=5, ge=1, le=100)
    auth_mode: str = "local"


class UserPatchIn(BaseModel):
    archive_limit: int | None = Field(default=None, ge=1, le=100)
    auth_mode: str | None = None


