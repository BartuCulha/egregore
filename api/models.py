from pydantic import BaseModel, Field
from typing import Optional


class GraphQuery(BaseModel):
    statement: str = Field(..., max_length=10240)
    parameters: dict = {}


class GraphBatch(BaseModel):
    queries: list[GraphQuery] = Field(..., min_length=1, max_length=20)


class NotifySend(BaseModel):
    to: str
    message: str


class NotifyGroup(BaseModel):
    message: str


class OrgRegister(BaseModel):
    org_name: str
    github_org: str
    telegram_chat_id: Optional[str] = None


# --- Setup flow models ---


class OrgSetup(BaseModel):
    """Founder: full org setup request."""
    github_org: str
    org_name: str
    is_personal: bool = False
    telegram_chat_id: Optional[str] = None
    repos: list[str] = []
    instance_name: Optional[str] = None
    transcript_sharing: bool = False
    hosting: bool = False
    server_type: str = "cax11"


class OrgJoin(BaseModel):
    """Joiner: join an existing org."""
    github_org: str
    repo_name: str = ""


class OrgTelegram(BaseModel):
    """Bot reports its chat_id after being added to a group."""
    org_slug: str
    chat_id: str
    group_title: Optional[str] = None
    group_username: Optional[str] = None


class GitHubCallback(BaseModel):
    """Exchange OAuth code for token."""
    code: str


class OrgInfo(BaseModel):
    """Org detection result for a single org."""
    login: str
    name: str
    has_egregore: bool
    role: str
    avatar_url: str = ""


class PersonalInfo(BaseModel):
    login: str
    has_egregore: bool


class UserInfo(BaseModel):
    login: str
    name: str
    avatar_url: str = ""


class SetupOrgsResponse(BaseModel):
    """Response for GET /api/org/setup/orgs."""
    user: UserInfo
    orgs: list[OrgInfo]
    personal: PersonalInfo


class OrgInvite(BaseModel):
    """Invite a GitHub user to an org's Egregore."""
    github_org: str
    github_username: str
    repo_name: str = "egregore-core"
    slug: str = ""  # Egregore slug — required when multiple orgs share a GitHub org
    github_token: str = ""  # GitHub token for org operations (when API key is in auth header)


class OrgAcceptInvite(BaseModel):
    """Accept an invite — invitee provides their token + invite token."""
    invite_token: str


class UserEnsure(BaseModel):
    """Ensure a user + membership exist in Supabase."""
    github_username: str
    github_name: Optional[str] = None
    display_name: Optional[str] = None
    telegram_username: Optional[str] = None
    telegram_id: Optional[int] = None
    # Onboarding harvest fields (per-org, stored on membership)
    member_role: Optional[str] = None  # engineering|design|research|operations|other
    focus: Optional[str] = None  # building|exploring|evaluating|other
    work_style: Optional[str] = None  # async|collaborative|both
    # Consent fields
    consent_session_tracking: Optional[bool] = None
    consent_transcript_sharing: Optional[bool] = None
    consent_telemetry: Optional[bool] = None
    contact_preference: Optional[str] = None  # all|none


class UserProfileUpdate(BaseModel):
    """Update user profile (Telegram handle and/or display name)."""
    telegram_username: Optional[str] = None
    display_name: Optional[str] = None


# --- Waitlist models ---


class WaitlistAdd(BaseModel):
    """Add to waitlist."""
    name: Optional[str] = None
    email: Optional[str] = None
    github_username: Optional[str] = None
    source: Optional[str] = None


class WaitlistApprove(BaseModel):
    """Approve a waitlist entry."""
    waitlist_id: int


class RemoveMemberResponse(BaseModel):
    """Response from removing a member."""
    status: str  # "removed" or "error"
    mode: str  # "revoke" or "full"
    username: str
    actions: list[str] = []  # summary of what was done
    errors: list[str] = []  # non-fatal errors


class HealthCheckin(BaseModel):
    """Health check-in from a client session at startup."""
    org_slug: str
    key_valid: Optional[bool] = None
    key_slug: Optional[str] = None
    config_slug: Optional[str] = None
    framework_version: Optional[str] = None
    memory_linked: Optional[bool] = None
    git_synced: Optional[bool] = None
    branch: Optional[str] = None
    errors: list[str] = []
    platform: Optional[str] = None
    shell: Optional[str] = None


# --- Hosting models ---


class HostingProvision(BaseModel):
    """Provision a Coder VPS for an org."""
    org_slug: str
    org_name: str
    github_org: str
    repo_name: str = "egregore-core"
    fork_url: Optional[str] = None
    memory_url: Optional[str] = None
    managed_repos: str = ""
    server_type: str = "cax11"


class HostingUser(BaseModel):
    """Create a Coder user on an org's VPS."""
    username: str
    email: Optional[str] = None
    name: Optional[str] = None


class UserKeysUpdate(BaseModel):
    """Update user API keys."""
    anthropic_api_key: Optional[str] = None


# --- Google Connector models ---


class GoogleOAuthCallback(BaseModel):
    """Exchange Google OAuth code for tokens."""
    code: str
    github_username: str


class GooglePromote(BaseModel):
    """Promote Google content to shared memory (graph + memory file)."""
    github_username: str
    google_id: str
    title: str
    service: str  # drive, gmail, calendar, docs, sheets
    content: str
    file_path: str
    summary: str
    topics: list[str] = []
    mentioned_people: list[str] = []
    related_quests: list[str] = []


class ScribeSummarize(BaseModel):
    title: str
    content: str
    type: str = "document"


class PulseSynthesize(BaseModel):
    session_id: str
    author: str
    topic: str = ""
    branch: str = ""
    tools_used: list[str] = []
    files_touched: list[str] = []
    tool_count: int = 0
    related_sessions: list[dict] = []
    other_sessions: list[dict] = []
    active_quests: list[dict] = []
    obs_raw: list[str] = []  # raw observation lines for deep analysis
