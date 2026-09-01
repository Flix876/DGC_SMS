import enum
import sqlite3
from datetime import datetime, timezone, timedelta, date

from flask import current_app
from flask_login import UserMixin
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from sqlalchemy import delete, event, insert, select
from sqlalchemy.engine import Engine
from werkzeug.security import generate_password_hash, check_password_hash

from app import db, login_manager

# Jamaica timezone (GMT-05:00) for consistent timestamp handling
JAMAICA_TZ = timezone(timedelta(hours=-5))


def jamaica_now():
    """Return current datetime in Jamaica timezone (GMT-05:00)."""
    return datetime.now(JAMAICA_TZ)


# ---------------------------------------------------------------------------
# SQLite tuning – enable WAL journal mode and a generous busy-timeout so that
# concurrent gunicorn workers don't produce "database is locked" errors.
# This listener fires for every new DBAPI connection; it is a no-op for any
# non-SQLite backend.
# ---------------------------------------------------------------------------

@event.listens_for(Engine, 'connect')
def _configure_sqlite(dbapi_conn, _connection_record):
    if isinstance(dbapi_conn, sqlite3.Connection):
        dbapi_conn.execute('PRAGMA journal_mode=WAL')
        dbapi_conn.execute('PRAGMA busy_timeout=5000')


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Role(enum.Enum):
    OFFICER = 'Officer'
    CHEMIST = 'Chemist'
    SENIOR_CHEMIST = 'Senior Chemist'
    DEPUTY = 'Deputy'
    HOD = 'HOD'
    ADMIN = 'Admin'
    GOVT_CHEMIST_ASSISTANT = 'GC Assistant'
    # Procurement / Stores Management roles
    SUPER_ADMIN = 'SuperAdmin'
    VIEWER = 'Viewer'
    REQUESTOR = 'Requestor'
    DIRECTOR_HRM = 'Director HRM&A'
    DIRECTOR_PROCUREMENT = 'Director Procurement'
    EVALUATION_COMMITTEE = 'Evaluation Committee Member'
    FINANCE_OFFICER = 'Finance Officer'
    PROCUREMENT_COMMITTEE = 'Procurement Committee'
    PROCUREMENT_OFFICER = 'Procurement Officer'
    PROPERTY_MANAGEMENT = 'Property Management Officer'

    @property
    def display_name(self):
        """Full display name for the role, used in UI labels.

        Most roles use their enum value as-is; this dict provides
        overrides only where the stored value differs from the desired
        display name (e.g. 'Deputy' is shown as 'Deputy Government Chemist').
        """
        _names = {
            'Deputy': 'Deputy Government Chemist',
        }
        return _names.get(self.value, self.value)


class Branch(enum.Enum):
    TOXICOLOGY = 'Toxicology'
    PHARMACEUTICAL = 'Pharmaceutical'
    PHARMACEUTICAL_NR = 'Not Registered Pharm'
    FOOD_MILK = 'Food (Milk)'
    FOOD_ALCOHOL = 'Food (Alcohol)'


class Permission(enum.Enum):
    """Fine-grained permissions that an admin can grant to individual users.
    These supplement (not replace) role-based access: a user may perform an
    action if their role already allows it OR if they have been explicitly
    granted the corresponding permission by an admin."""
    REGISTER_SAMPLE         = 'Register Sample'
    EDIT_SAMPLE             = 'Edit Sample'
    EDIT_ACCREDITATION      = 'Edit Accreditation Status'
    ASSIGN_SAMPLE           = 'Assign Sample'
    SUBMIT_REPORT           = 'Submit Report'
    PRELIMINARY_REVIEW      = 'Preliminary Review'
    TECHNICAL_REVIEW        = 'Technical Review'
    DEPUTY_REVIEW           = 'Deputy Review'
    HOD_REVIEW              = 'HOD Review / Sign'
    MANAGE_USERS            = 'Manage Users'
    # New permissions (Feature 6)
    MULTI_ANALYST_ASSIGN    = 'Multi-Analyst Assignment'
    COA_DECERTIFY_REISSUE   = 'COA Decertify / Re-Issue'
    OOS_FLAG                = 'OOS Flag Usage'
    KPI_VIEW                = 'KPI Viewing'
    INVOICE_GENERATE        = 'Invoice Generation'
    MANAGE_DROPDOWNS        = 'Manage Dropdown Values'
    MANAGE_SETTINGS         = 'Manage Settings'
    VIEW_ALL_BRANCHES_REPORT = 'View All Branches Report'
    VIEW_ANALYST_PERFORMANCE_REPORT = 'View Analyst Performance Report'
    # HOD Workflow Return & Audit/QC permissions
    HOD_WORKFLOW_RETURN     = 'HOD Workflow Return'
    QC_DATA_DOWNLOAD        = 'QC Data Download'
    AUDIT_LOG_VIEW          = 'View Audit Log'
    AUDIT_LOG_EXPORT        = 'Export Audit Log'
    SAMPLE_COMMENT_ADD      = 'Add Sample Comments'
    SAMPLE_COMMENT_EDIT     = 'Edit Sample Comments'
    SAMPLE_COMMENT_DELETE   = 'Delete Sample Comments'
    ADD_SUPPORTING_DOCUMENT    = 'Add Supporting Documents'
    DELETE_SUPPORTING_DOCUMENT = 'Delete Supporting Documents'
    # My Preliminary Reviews — permission-based visibility scopes
    VIEW_OWN_PRELIMINARY_REVIEWS  = 'View Own Preliminary Reviews'
    VIEW_TEAM_PRELIMINARY_REVIEWS = 'View Team Preliminary Reviews'
    VIEW_ALL_PRELIMINARY_REVIEWS  = 'View All Preliminary Reviews'
    MANAGE_PRELIMINARY_REVIEWS    = 'Manage Preliminary Reviews'


# ---------------------------------------------------------------------------
# Many-to-many association tables for User ↔ Role / Branch / Permission
# ---------------------------------------------------------------------------

user_roles = db.Table(
    'user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role', db.Enum(Role), primary_key=True),
)

user_branches = db.Table(
    'user_branches',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('branch', db.Enum(Branch), primary_key=True),
)

user_permissions = db.Table(
    'user_permissions',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('permission', db.Enum(Permission), primary_key=True),
)

custom_role_permissions = db.Table(
    'custom_role_permissions',
    db.Column(
        'custom_role_id',
        db.Integer,
        db.ForeignKey('custom_roles.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    db.Column('permission', db.Enum(Permission), primary_key=True),
)

user_custom_roles = db.Table(
    'user_custom_roles',
    db.Column(
        'user_id',
        db.Integer,
        db.ForeignKey('users.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    db.Column(
        'custom_role_id',
        db.Integer,
        db.ForeignKey('custom_roles.id', ondelete='CASCADE'),
        primary_key=True,
    ),
)


class CustomRole(db.Model):
    __tablename__ = 'custom_roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    users = db.relationship(
        'User',
        secondary=user_custom_roles,
        back_populates='custom_roles_rel',
        lazy='selectin',
    )

    @property
    def permissions(self):
        rows = db.session.execute(
            select(custom_role_permissions).where(
                custom_role_permissions.c.custom_role_id == self.id
            )
        ).fetchall()
        return {row.permission for row in rows}

    @permissions.setter
    def permissions(self, values):
        db.session.execute(
            delete(custom_role_permissions).where(
                custom_role_permissions.c.custom_role_id == self.id
            )
        )
        for p in set(values or []):
            db.session.execute(
                insert(custom_role_permissions).values(
                    custom_role_id=self.id,
                    permission=p,
                )
            )

    @property
    def display_name(self):
        return self.name

    def __repr__(self):
        return f'<CustomRole {self.name}>'


class ActingRole(db.Model):
    """Temporary role assignment for a user acting in a position with an expiry date."""
    __tablename__ = 'acting_roles'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role = db.Column(db.Enum(Role), nullable=False)
    assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('acting_roles', lazy='selectin'))
    assigned_by_user = db.relationship('User', foreign_keys=[assigned_by])

    @property
    def is_active(self):
        """Return True if the acting role is currently within its valid date range."""
        today = jamaica_now().date()
        return self.start_date <= today <= self.expiry_date

    def __repr__(self):
        return f'<ActingRole user_id={self.user_id} role={self.role.value} expires={self.expiry_date}>'


class SampleStatus(enum.Enum):
    REGISTERED = 'Registered'
    ASSIGNED = 'Assigned'
    IN_PROGRESS = 'In Progress'
    REPORT_SUBMITTED = 'Report Submitted'
    UNDER_PRELIMINARY_REVIEW = 'Preliminary Review'
    UNDER_TECHNICAL_REVIEW = 'Senior Chemist Review'
    RETURNED = 'Returned for Correction'
    ACCEPTED = 'Accepted'
    DEPUTY_REVIEW = 'Deputy Review'
    DEPUTY_RETURNED = 'Returned by Deputy'
    CERTIFICATE_PREPARATION = 'Certificate Preparation'
    HOD_REVIEW = 'HOD Review'
    HOD_RETURNED = 'Returned by HOD'
    CERTIFIED = 'Certified'
    REJECTED = 'Rejected'
    COMPLETED = 'Completed'


class AssignmentStatus(enum.Enum):
    ASSIGNED = 'Assigned'
    IN_PROGRESS = 'In Progress'
    REPORT_SUBMITTED = 'Report Submitted'
    UNDER_PRELIMINARY_REVIEW = 'Preliminary Review'
    UNDER_TECHNICAL_REVIEW = 'Senior Chemist Review'
    RETURNED = 'Returned for Correction'
    ACCEPTED = 'Accepted'
    REJECTED = 'Rejected'
    COMPLETED = 'Completed'


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    first_name = db.Column(db.String(120), nullable=False)
    last_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.Enum(Role), nullable=True)       # legacy single-role
    branch = db.Column(db.Enum(Branch), nullable=True)    # legacy single-branch
    is_active_user = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=True)
    created_at = db.Column(
        db.DateTime, default=jamaica_now
    )
    # Account lockout – brute-force protection
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    # Activity tracking – updated on every authenticated request
    last_seen = db.Column(db.DateTime, nullable=True)

    # Relationships
    uploaded_samples = db.relationship(
        'Sample', backref='uploaded_by_user', lazy='dynamic',
        foreign_keys='Sample.uploaded_by'
    )
    assignments = db.relationship(
        'SampleAssignment', backref='chemist', lazy='dynamic',
        foreign_keys='SampleAssignment.chemist_id'
    )
    custom_roles_rel = db.relationship(
        'CustomRole',
        secondary=user_custom_roles,
        back_populates='users',
        lazy='selectin',
    )

    # Maximum failed attempts before account is temporarily locked
    MAX_FAILED_ATTEMPTS = 5
    # Lockout duration in minutes
    LOCKOUT_MINUTES = 15

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_locked(self):
        """Return True if account is currently locked due to failed attempts."""
        if self.locked_until is None:
            return False
        now = jamaica_now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=JAMAICA_TZ)
        locked = self.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=JAMAICA_TZ)
        return now < locked

    def record_failed_login(self):
        """Increment failed login counter; lock account if threshold reached."""
        self.failed_login_attempts = (self.failed_login_attempts or 0) + 1
        if self.failed_login_attempts >= self.MAX_FAILED_ATTEMPTS:
            self.locked_until = jamaica_now() + timedelta(
                minutes=self.LOCKOUT_MINUTES
            )

    def reset_failed_logins(self):
        """Clear failed login counter after a successful login."""
        self.failed_login_attempts = 0
        self.locked_until = None

    # ----- role / branch / permission helpers (backed by association tables) -----

    _roles = None        # in-memory cache / pending value
    _roles_dirty = False  # True only when set via the setter (needs DB write)
    _branches = None
    _branches_dirty = False
    _permissions = None
    _permissions_dirty = False

    @property
    def roles(self):
        """Return the set of Role enums for this user."""
        if self._roles is not None:
            return self._roles
        if self.id is None:
            self._roles = set()
            return self._roles
        rows = db.session.execute(
            select(user_roles).where(user_roles.c.user_id == self.id)
        ).fetchall()
        result = {row.role for row in rows}
        # Fall back to the legacy single-value column for users created before
        # the multi-role association table was introduced.
        if not result and self.role is not None:
            result = {self.role}
        self._roles = result
        # _roles_dirty intentionally NOT set – this is a DB read, not a write
        return self._roles

    @roles.setter
    def roles(self, value):
        self._roles = set(value)
        self._roles_dirty = True

    @property
    def branches(self):
        """Return the set of Branch enums for this user."""
        if self._branches is not None:
            return self._branches
        if self.id is None:
            self._branches = set()
            return self._branches
        rows = db.session.execute(
            select(user_branches).where(user_branches.c.user_id == self.id)
        ).fetchall()
        result = {row.branch for row in rows}
        # Fall back to the legacy single-value column for users created before
        # the multi-branch association table was introduced.
        if not result and self.branch is not None:
            result = {self.branch}
        self._branches = result
        # _branches_dirty intentionally NOT set – this is a DB read, not a write
        return self._branches

    @branches.setter
    def branches(self, value):
        self._branches = set(value)
        self._branches_dirty = True

    @property
    def permissions(self):
        """Return the set of explicitly-granted Permission enums for this user."""
        if self._permissions is not None:
            return self._permissions
        if self.id is None:
            self._permissions = set()
            return self._permissions
        rows = db.session.execute(
            select(user_permissions).where(user_permissions.c.user_id == self.id)
        ).fetchall()
        self._permissions = {row.permission for row in rows}
        return self._permissions

    @permissions.setter
    def permissions(self, value):
        self._permissions = set(value)
        self._permissions_dirty = True

    def has_role(self, role):
        if role in self.roles:
            return True
        # Check active acting roles
        return any(ar.role == role and ar.is_active for ar in self.acting_roles)

    def has_any_role(self, *roles):
        roles_set = set(roles)
        if self.roles & roles_set:
            return True
        # Check active acting roles
        return any(ar.role in roles_set and ar.is_active for ar in self.acting_roles)

    @property
    def active_acting_roles(self):
        """Return list of currently active acting roles."""
        return [ar for ar in self.acting_roles if ar.is_active]

    def has_branch(self, branch):
        return branch in self.branches

    def has_any_branch(self, *branches):
        return bool(self.branches & set(branches))

    def has_permission(self, permission):
        """Return True if the user holds the given permission.

        Authorization flow (explicit grants take precedence over roles):
          1. Check explicit user permission grants first — a permission an
             admin has manually enabled for this user always wins, regardless
             of what the user's role(s) would otherwise allow.
          2. Check permissions granted through the user's custom roles.
          3. Fall back to role-based access (the Admin role implies every
             permission).
          4. Otherwise, deny.
        """
        # 1. Explicit per-user grant — highest precedence.
        if permission in self.permissions:
            return True
        # 2. Custom-role grant.
        if any(permission in cr.permissions for cr in self.custom_roles_rel):
            return True
        # 3. Role-based fallback: Admins implicitly hold all permissions.
        if self.has_role(Role.ADMIN):
            return True
        # 4. No grant found — deny.
        return False

    @property
    def role_names(self):
        """Comma-separated display of roles."""
        names = {r.value for r in self.roles}
        names.update(cr.name for cr in self.custom_roles_rel)
        # Include active acting roles
        for ar in self.active_acting_roles:
            names.add(f'{ar.role.value} (Acting)')
        return ', '.join(sorted(names)) or '—'

    @property
    def branch_names(self):
        """Comma-separated display of branches."""
        return ', '.join(sorted(b.value for b in self.branches)) or '—'

    @property
    def permission_names(self):
        """Comma-separated display of explicitly-granted permissions."""
        return ', '.join(sorted(p.value for p in self.permissions)) or '—'

    @property
    def primary_branch(self):
        """Return one branch (for filtering) or None."""
        return next(iter(self.branches), None)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'

    def get_reset_token(self):
        """Generate a timed password-reset token."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        return s.dumps(self.id, salt='password-reset')

    @staticmethod
    def verify_reset_token(token, max_age=1800):
        """Return the User for a valid token, or None (default 30 min expiry)."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        try:
            user_id = s.loads(token, salt='password-reset', max_age=max_age)
        except (SignatureExpired, BadSignature):
            return None
        return db.session.get(User, user_id)

    def is_branch_head(self):
        return self.has_any_role(Role.SENIOR_CHEMIST, Role.HOD, Role.DEPUTY)

    def __repr__(self):
        return f'<User {self.username} ({self.role_names})>'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Flush pending roles/branches/permissions to the association tables after
# insert or update.  Only fires when the dirty flag is set.

@event.listens_for(User, 'after_insert')
@event.listens_for(User, 'after_update')
def _flush_user_roles_branches(mapper, connection, target):
    if target._roles_dirty:
        connection.execute(
            delete(user_roles).where(user_roles.c.user_id == target.id)
        )
        for r in target._roles:
            connection.execute(insert(user_roles).values(
                user_id=target.id, role=r
            ))
        target._roles_dirty = False
    if target._branches_dirty:
        connection.execute(
            delete(user_branches).where(user_branches.c.user_id == target.id)
        )
        for b in target._branches:
            connection.execute(insert(user_branches).values(
                user_id=target.id, branch=b
            ))
        target._branches_dirty = False
    if target._permissions_dirty:
        connection.execute(
            delete(user_permissions).where(user_permissions.c.user_id == target.id)
        )
        for p in target._permissions:
            connection.execute(insert(user_permissions).values(
                user_id=target.id, permission=p
            ))
        target._permissions_dirty = False

# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------

class Sample(db.Model):
    __tablename__ = 'samples'

    id = db.Column(db.Integer, primary_key=True)
    lab_number = db.Column(db.String(50), unique=True, nullable=False, index=True)
    sample_name = db.Column(db.String(255), nullable=False)
    sample_type = db.Column(db.Enum(Branch), nullable=False)
    description = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.String(100), nullable=True)
    parish = db.Column(db.String(100), nullable=True)
    patient_name = db.Column(db.String(255), nullable=True)  # Toxicology
    source = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.Enum(SampleStatus), nullable=False, default=SampleStatus.REGISTERED,
        index=True
    )

    # Food (Milk) specific
    milk_type = db.Column(db.String(10), nullable=True)  # 'R' = Raw, 'P' = Processed

    # Volume (Toxicology and Milk samples)
    volume = db.Column(db.String(100), nullable=True)

    # Pharmaceutical specific
    formulation_type = db.Column(db.String(100), nullable=True)
    manufacturer = db.Column(db.String(255), nullable=True)
    api = db.Column(db.String(255), nullable=True)

    # Accreditation status (selected by Deputy Government Chemist before
    # certifying – primarily for pharmaceutical samples). None = not yet set.
    is_accredited = db.Column(db.Boolean, nullable=True)

    # Food (Alcohol) specific
    alcohol_type = db.Column(db.String(100), nullable=True)
    claim_butt_number = db.Column(db.String(100), nullable=True)
    batch_lot_number = db.Column(db.String(100), nullable=True)  # Food (Alcohol)

    # Pharmaceutical & Milk shared fields
    lot_number = db.Column(db.String(100), nullable=True)
    expiration_date = db.Column(db.Date, nullable=True)

    # Toxicology – sample type dropdown (Blood, Urine, etc.)
    toxicology_sample_type_name = db.Column(db.String(100), nullable=True)

    # Toxicology – additional clinical fields
    doctors_name = db.Column(db.String(255), nullable=True)
    registration_docket_no = db.Column(db.String(100), nullable=True)
    patient_gender = db.Column(db.String(20), nullable=True)
    ward_clinic = db.Column(db.String(255), nullable=True)
    test_requested = db.Column(db.String(500), nullable=True)
    diagnosis_indicated = db.Column(db.Text, nullable=True)

    # Scanned document
    scanned_file = db.Column(db.String(500), nullable=True)
    scanned_file_original_name = db.Column(db.String(255), nullable=True)

    # Tracking
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    date_received = db.Column(db.Date, nullable=False)
    date_registered = db.Column(
        db.DateTime, default=jamaica_now
    )
    expected_report_date = db.Column(db.Date, nullable=True, index=True)

    # Summary report (pharmaceutical samples – prepared by Senior Chemist)
    summary_report = db.Column(db.Text, nullable=True)
    summary_report_file = db.Column(db.String(500), nullable=True)
    summary_report_file_original_name = db.Column(db.String(255), nullable=True)
    summary_report_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    summary_report_at = db.Column(db.DateTime, nullable=True)

    # Deputy Government Chemist review
    deputy_review_comments = db.Column(db.Text, nullable=True)
    deputy_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    deputy_reviewed_at = db.Column(db.DateTime, nullable=True)

    # Certificate of Analysis (prepared by Deputy)
    certificate_text = db.Column(db.Text, nullable=True)
    certificate_file = db.Column(db.String(500), nullable=True)
    certificate_file_original_name = db.Column(db.String(255), nullable=True)
    certificate_prepared_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    certificate_prepared_at = db.Column(db.DateTime, nullable=True)
    coa_reference = db.Column(db.String(255), nullable=True)  # Certificate reference number

    # Government Chemist (HOD) review & signing
    hod_review_comments = db.Column(db.Text, nullable=True)
    hod_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    hod_reviewed_at = db.Column(db.DateTime, nullable=True)
    certified_at = db.Column(db.DateTime, nullable=True)
    certified_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )

    # COA Decertify / Re-Issue (Feature 5) – audit trail
    coa_version = db.Column(db.Integer, nullable=False, default=1)   # increments on re-issue
    decertified_at = db.Column(db.DateTime, nullable=True)
    decertified_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    decertify_reason = db.Column(db.Text, nullable=True)
    reissued_at = db.Column(db.DateTime, nullable=True)
    reissued_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )

    # HOD Workflow Return – allows HOD to return to any prior stage
    returned_to_stage = db.Column(db.String(50), nullable=True)
    hod_return_reason = db.Column(db.Text, nullable=True)
    returned_by_hod_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    assignments = db.relationship(
        'SampleAssignment', backref='sample', lazy='dynamic',
        cascade='all, delete-orphan'
    )
    history = db.relationship(
        'SampleHistory', backref='sample', lazy='dynamic',
        cascade='all, delete-orphan', order_by='SampleHistory.created_at.desc()'
    )
    summary_report_user = db.relationship(
        'User', foreign_keys=[summary_report_by]
    )
    deputy_reviewer = db.relationship(
        'User', foreign_keys=[deputy_reviewed_by]
    )
    certificate_preparer = db.relationship(
        'User', foreign_keys=[certificate_prepared_by]
    )
    hod_reviewer = db.relationship(
        'User', foreign_keys=[hod_reviewed_by]
    )
    certifier = db.relationship(
        'User', foreign_keys=[certified_by]
    )
    decertifier = db.relationship(
        'User', foreign_keys=[decertified_by]
    )
    reissuer = db.relationship(
        'User', foreign_keys=[reissued_by]
    )
    comments = db.relationship(
        'SampleComment', backref='sample', lazy='dynamic',
        cascade='all, delete-orphan', order_by='SampleComment.created_at'
    )

    def __repr__(self):
        return f'<Sample {self.lab_number} - {self.sample_name}>'


# ---------------------------------------------------------------------------
# Sample Assignment  (one sample → many chemists)
# ---------------------------------------------------------------------------

class SampleAssignment(db.Model):
    __tablename__ = 'sample_assignments'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    chemist_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False, index=True
    )
    assigned_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    test_name = db.Column(db.String(255), nullable=False)
    test_reference = db.Column(db.String(255), nullable=True)
    status = db.Column(
        db.Enum(AssignmentStatus), nullable=False,
        default=AssignmentStatus.ASSIGNED
    )

    assigned_date = db.Column(
        db.DateTime, default=jamaica_now
    )
    expected_completion = db.Column(db.Date, nullable=True)
    date_completed = db.Column(db.DateTime, nullable=True)

    # Report
    report_text = db.Column(db.Text, nullable=True)
    report_file = db.Column(db.String(500), nullable=True)
    report_file_original_name = db.Column(db.String(255), nullable=True)
    report_submitted_at = db.Column(db.DateTime, nullable=True)
    all_samples_returned = db.Column(db.String(10), nullable=True)
    return_quantity = db.Column(db.String(100), nullable=True)
    test_date = db.Column(db.Date, nullable=True)
    meets_specifications = db.Column(db.String(20), nullable=True)  # 'Yes', 'No', 'N/A'
    report_comments = db.Column(db.Text, nullable=True)

    # Senior Chemist review flag
    out_of_spec = db.Column(db.Boolean, nullable=True, default=None)

    # OOS Investigation flag set at assignment time (Feature 4)
    oos_investigation = db.Column(db.Boolean, nullable=False, default=False)

    # Preliminary review (by Officer / Senior Chemist Technologist)
    preliminary_review_comments = db.Column(db.Text, nullable=True)
    preliminary_reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    preliminary_reviewed_at = db.Column(db.DateTime, nullable=True)
    preliminary_review_checklist = db.Column(db.Text, nullable=True)  # JSON

    # Technical review (by Senior Chemist)
    review_comments = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)

    # Track which review stage returned from ('preliminary' or 'technical')
    return_stage = db.Column(db.String(20), nullable=True)
    comments = db.Column(db.Text, nullable=True)
    quantity_volume = db.Column(db.String(100), nullable=True)

    # Optional comment left when a Senior Chemist returns the assignment from
    # the sample detail page (distinct from review_comments set during the
    # formal Senior Chemist review form).
    return_to_analyst_comments = db.Column(db.Text, nullable=True)

    # Relationships
    assigner = db.relationship(
        'User', foreign_keys=[assigned_by], backref='made_assignments'
    )
    preliminary_reviewer = db.relationship(
        'User', foreign_keys=[preliminary_reviewed_by],
        backref='preliminary_reviewed_assignments'
    )
    reviewer = db.relationship(
        'User', foreign_keys=[reviewed_by], backref='reviewed_assignments'
    )

    def __repr__(self):
        return f'<Assignment {self.id}: Sample {self.sample_id} → User {self.chemist_id}>'


# ---------------------------------------------------------------------------
# Audit / History
# ---------------------------------------------------------------------------

class SampleHistory(db.Model):
    __tablename__ = 'sample_history'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    action = db.Column(db.String(255), nullable=False)
    details = db.Column(db.Text, nullable=True)
    performed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    created_at = db.Column(
        db.DateTime, default=jamaica_now
    )
    # Enhanced audit fields
    action_type = db.Column(db.String(100), nullable=True)     # e.g. 'Original Submission', 'Resubmission'
    object_affected = db.Column(db.String(255), nullable=True)  # e.g. 'Report', 'COA', 'Sample'
    change_description = db.Column(db.Text, nullable=True)      # what changed and why

    performer = db.relationship('User', foreign_keys=[performed_by])

    def __repr__(self):
        return f'<History {self.action} on Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Review History  (logs every review iteration for full traceability)
# ---------------------------------------------------------------------------

class ReviewHistory(db.Model):
    """Stores a snapshot of every review performed so that previous reviews
    are never lost when a report is returned and re-reviewed."""
    __tablename__ = 'review_history'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    assignment_id = db.Column(
        db.Integer, db.ForeignKey('sample_assignments.id'), nullable=True
    )
    review_type = db.Column(db.String(50), nullable=False)   # 'preliminary', 'technical', 'deputy', 'hod'
    review_number = db.Column(db.Integer, nullable=False, default=1)
    action = db.Column(db.String(50), nullable=False)        # 'approved', 'returned', 'accepted', 'rejected', 'sign'
    reviewer_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    reviewed_at = db.Column(db.DateTime, default=jamaica_now)
    comments = db.Column(db.Text, nullable=True)
    checklist_data = db.Column(db.Text, nullable=True)       # JSON for preliminary review checklist

    # Relationships
    sample = db.relationship('Sample', backref=db.backref(
        'review_histories', lazy='dynamic',
        order_by='ReviewHistory.reviewed_at.desc()',
        cascade='all, delete-orphan',
    ))
    assignment = db.relationship('SampleAssignment', backref=db.backref(
        'review_histories', lazy='dynamic',
        order_by='ReviewHistory.reviewed_at.desc()',
        cascade='all, delete-orphan',
    ))
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])

    def __repr__(self):
        return f'<ReviewHistory #{self.review_number} {self.review_type} {self.action} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(500), nullable=True)
    is_read = db.Column(db.Boolean, default=False, index=True)
    email_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(
        db.DateTime, default=jamaica_now
    )

    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic'))

    def __repr__(self):
        return f'<Notification {self.title} for User {self.user_id}>'


# ---------------------------------------------------------------------------
# App Settings (key/value store)
# ---------------------------------------------------------------------------

class Setting(db.Model):
    __tablename__ = 'settings'

    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.String(500), nullable=False, default='')

    @staticmethod
    def get(key, default=''):
        row = db.session.get(Setting, key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = db.session.get(Setting, key)
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=key, value=value))

    @staticmethod
    def get_bool(key, default=True):
        val = Setting.get(key)
        if val == '':
            return default
        return val.lower() in ('true', '1', 'yes')

    def __repr__(self):
        return f'<Setting {self.key}={self.value}>'


# ---------------------------------------------------------------------------
# KPI Targets
# ---------------------------------------------------------------------------

# Canonical list of KPI metric keys and their human-readable labels.
KPI_METRICS = [
    ('pct_improvement_planned_targets',
     '% improvement in the achievement of planned targets'),
    ('pct_compliance_sop',
     '% compliance with standard operating procedures, quality control, '
     'environmental and accreditation standards'),
    ('complaints_resolved',
     '# of complaints resolved per quarter'),
    ('analysts_qualified',
     '# of analyst with required technical skills and competencies'),
    ('equipment_inspections',
     '# of equipment maintenance inspections conducted per quarter'),
    ('pharma_coas',
     '# Pharmaceutical COA\'s generated'),
    ('milk_coas',
     '# of milk COA\'s generated'),
    ('toxicology_roas',
     '# of toxicology ROA\'s generated'),
    ('alcohol_coas',
     '# Alcohol COA\'s generated'),
    ('avg_days_pharma_coa',
     'Average days taken to generate pharmaceutical COA\'s'),
    ('avg_days_milk_coa',
     'Average days taken to generate milk COA\'s'),
    ('avg_days_toxicology_roa',
     'Average days taken to generate toxicology ROA\'s'),
    ('avg_days_alcohol_coa',
     'Average days taken to generate Alcohol COA\'s'),
    ('avg_days_alcohol_determination',
     'Average days taken to generate Alcohol Determination COA\'s (target: 5 days)'),
    ('avg_days_alcohol_denatured',
     'Average days taken to generate Denatured Alcohol COA\'s (target: 1 day)'),
    ('avg_days_alcohol_det_denatured',
     'Average days taken to generate Alcohol Determination & Denatured COA\'s (target: 5 days)'),
    ('out_of_spec_pharma',
     '# Pharmaceutical samples out of specification'),
    ('out_of_spec_milk',
     '# Milk samples out of specification'),
    ('out_of_spec_toxicology',
     '# Toxicology samples out of specification'),
    ('out_of_spec_alcohol',
     '# Alcohol samples out of specification'),
    # Feature 3 – Pharmaceutical Tests Performed
    ('pharma_tests_performed',
     '# of Pharmaceutical Tests Performed'),
]

# Keys whose "Actual" value is auto-computed from Sample data.
AUTO_ACTUAL_KEYS = {
    'pharma_coas', 'milk_coas', 'toxicology_roas', 'alcohol_coas',
    'avg_days_pharma_coa', 'avg_days_milk_coa', 'avg_days_toxicology_roa',
    'avg_days_alcohol_coa',
    'avg_days_alcohol_determination', 'avg_days_alcohol_denatured',
    'avg_days_alcohol_det_denatured',
    'out_of_spec_pharma', 'out_of_spec_milk',
    'out_of_spec_toxicology', 'out_of_spec_alcohol',
    'pharma_tests_performed',
}


class KpiTarget(db.Model):
    """Stores per-year / per-quarter KPI targets and optional manual actuals."""
    __tablename__ = 'kpi_targets'

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    quarter = db.Column(db.Integer, nullable=False)          # 1-4
    kpi_key = db.Column(db.String(100), nullable=False)
    target_value = db.Column(db.Float, nullable=True)
    actual_override = db.Column(db.Float, nullable=True)     # for manual KPIs

    __table_args__ = (
        db.UniqueConstraint('year', 'quarter', 'kpi_key',
                            name='uq_kpi_target'),
    )

    def __repr__(self):
        return (f'<KpiTarget {self.kpi_key} '
                f'Y{self.year} Q{self.quarter} '
                f'T={self.target_value} A={self.actual_override}>')


# ---------------------------------------------------------------------------
# Non-Working Days (holidays, emergency closures) – for TAT calculation
# ---------------------------------------------------------------------------

class NonWorkingDay(db.Model):
    """Calendar entries for public holidays and emergency closure days.
    These dates are excluded from turnaround time (TAT) calculations."""
    __tablename__ = 'non_working_days'

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True, index=True)
    description = db.Column(db.String(255), nullable=False)
    day_type = db.Column(db.String(50), nullable=False, default='holiday')  # 'holiday' or 'emergency'
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    creator = db.relationship('User', foreign_keys=[created_by])

    def __repr__(self):
        return f'<NonWorkingDay {self.date} - {self.description}>'


def fetch_non_working_days(start_date, end_date):
    """Return the set of non-working dates (holidays/emergency closures) between
    *start_date* and *end_date* (inclusive).  Use this to pre-fetch the holiday
    set once before calling :func:`calculate_working_days` in a loop."""
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()
    return {
        row.date for row in NonWorkingDay.query.filter(
            NonWorkingDay.date >= start_date,
            NonWorkingDay.date <= end_date,
        ).all()
    }


def calculate_working_days(start_date, end_date, non_working_dates=None):
    """Calculate working days between two dates, excluding weekends and
    non-working days (public holidays, emergency closures).

    Pass a pre-fetched set of non-working dates as *non_working_dates* to avoid
    a database query on each call when processing multiple samples.  If
    *non_working_dates* is ``None`` the function queries the database itself.
    """
    if not start_date or not end_date:
        return None
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    if non_working_dates is None:
        # Fall back to a single DB query when no pre-fetched set is supplied.
        non_working_dates = fetch_non_working_days(start_date, end_date)

    count = 0
    # TAT starts the day *after* the provided start date.
    current = start_date + timedelta(days=1)
    while current <= end_date:
        # Exclude weekends (Mon-Fri are 0-4; Sat=5, Sun=6) and non-working days
        if current.weekday() < 5 and current not in non_working_dates:
            count += 1
        current += timedelta(days=1)
    return count


def add_working_days(start_date, n, non_working_dates=None):
    """Return the date that is *n* working days after *start_date*,
    excluding weekends and non-working days.

    Pass a pre-fetched set of non-working dates as *non_working_dates* to avoid
    a database query on each call when processing multiple samples.
    """
    if not start_date or n is None or n <= 0:
        return start_date
    if isinstance(start_date, datetime):
        start_date = start_date.date()

    if non_working_dates is None:
        # Upper bound: each working day needs at most ~2 calendar days on average
        # (weekends) plus a 10-day buffer for public holidays.
        rough_end = start_date + timedelta(days=int(n) * 2 + 10)
        non_working_dates = fetch_non_working_days(start_date, rough_end)

    count = 0
    current = start_date
    while count < n:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in non_working_dates:
            count += 1
    return current


# ---------------------------------------------------------------------------
# Supporting Documents (additional uploads by officers)
# ---------------------------------------------------------------------------

class SupportingDocument(db.Model):
    """Extra supporting documents uploaded for a sample."""
    __tablename__ = 'supporting_documents'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    uploaded_at = db.Column(db.DateTime, default=jamaica_now)

    sample = db.relationship('Sample', backref=db.backref(
        'supporting_documents', lazy='dynamic', cascade='all, delete-orphan'
    ))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])

    def __repr__(self):
        return f'<SupportingDocument {self.original_name} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Sample Image (photographs of a sample – previewed on the detail page)
# ---------------------------------------------------------------------------

class SampleImage(db.Model):
    """Image files (photographs) uploaded for a sample.

    Used primarily for pharmaceutical samples so that users can view
    pictures of the physical sample alongside the submission form.
    """
    __tablename__ = 'sample_images'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.String(500), nullable=True)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    uploaded_at = db.Column(db.DateTime, default=jamaica_now)

    sample = db.relationship('Sample', backref=db.backref(
        'images', lazy='dynamic', cascade='all, delete-orphan'
    ))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])

    def __repr__(self):
        return f'<SampleImage {self.original_name} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Document Version (tracks every file upload as a new version)
# ---------------------------------------------------------------------------

class DocumentVersion(db.Model):
    """Tracks every uploaded file version for full retention.
    New uploads never overwrite previous documents; each is stored as a
    new version linked to the parent record."""
    __tablename__ = 'document_versions'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    document_type = db.Column(db.String(50), nullable=False)  # 'scanned_file', 'report', 'certificate', 'summary_report', 'supporting'
    version_number = db.Column(db.Integer, nullable=False, default=1)
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=False)
    upload_label = db.Column(db.String(50), nullable=True)  # 'original', 'revised', 'resubmission'
    resubmission_type = db.Column(db.String(50), nullable=True)  # 'preliminary', 'technical', 'deputy', 'hod', 'unspecified'
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    uploaded_at = db.Column(db.DateTime, default=jamaica_now)
    assignment_id = db.Column(
        db.Integer, db.ForeignKey('sample_assignments.id'), nullable=True
    )

    sample = db.relationship('Sample', backref=db.backref(
        'document_versions', lazy='dynamic', cascade='all, delete-orphan'
    ))
    uploader = db.relationship('User', foreign_keys=[uploaded_by])
    assignment = db.relationship('SampleAssignment', backref=db.backref(
        'document_versions', lazy='dynamic'
    ))

    def __repr__(self):
        return f'<DocumentVersion v{self.version_number} {self.document_type} for Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Back-Date Request (controlled back-dating with approval workflow)
# ---------------------------------------------------------------------------

class BackDateRequest(db.Model):
    """Tracks requests to back-date entries, requiring HOD/Deputy approval."""
    __tablename__ = 'back_date_requests'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    assignment_id = db.Column(
        db.Integer, db.ForeignKey('sample_assignments.id'), nullable=True
    )
    field_name = db.Column(db.String(100), nullable=False)  # e.g. 'date_received', 'report_date'
    original_date = db.Column(db.String(50), nullable=False)
    proposed_date = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, nullable=True)
    requested_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    requested_at = db.Column(db.DateTime, default=jamaica_now)
    status = db.Column(db.String(20), nullable=False, default='pending')  # 'pending', 'approved', 'denied'
    decided_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_comments = db.Column(db.Text, nullable=True)

    sample = db.relationship('Sample', backref=db.backref(
        'back_date_requests', lazy='dynamic', cascade='all, delete-orphan'
    ))
    assignment = db.relationship('SampleAssignment', backref=db.backref(
        'back_date_requests', lazy='dynamic'
    ))
    requester = db.relationship('User', foreign_keys=[requested_by])
    decider = db.relationship('User', foreign_keys=[decided_by])

    def __repr__(self):
        return f'<BackDateRequest {self.field_name} for Sample {self.sample_id} ({self.status})>'


# ---------------------------------------------------------------------------
# Delete Request  (HOD-approval workflow for sample / assignment deletion)
# ---------------------------------------------------------------------------

class DeleteRequest(db.Model):
    """A request by an authorised user to delete a sample or assignment.

    The record is submitted by a Senior Chemist, Deputy, Officer, or
    GC Assistant and must be approved by the HOD before the deletion is
    actually performed.  The deletion snapshot is stored here so the audit
    trail survives even after the entity is gone.
    """
    __tablename__ = 'delete_requests'

    id = db.Column(db.Integer, primary_key=True)
    # 'sample' or 'assignment'
    request_type = db.Column(db.String(20), nullable=False)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=True
    )
    assignment_id = db.Column(
        db.Integer, db.ForeignKey('sample_assignments.id'), nullable=True
    )
    reason = db.Column(db.Text, nullable=True)
    # JSON snapshot of the entity at request time (kept even after deletion)
    entity_snapshot = db.Column(db.Text, nullable=True)
    requested_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    requested_at = db.Column(db.DateTime, default=jamaica_now)
    # 'pending', 'approved', 'denied'
    status = db.Column(db.String(20), nullable=False, default='pending')
    decided_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_comments = db.Column(db.Text, nullable=True)
    # Human-readable label kept for display after the entity is deleted
    entity_label = db.Column(db.String(255), nullable=True)

    # Relationships
    sample = db.relationship('Sample', foreign_keys=[sample_id], backref=db.backref(
        'delete_requests', lazy='dynamic'
    ))
    assignment = db.relationship('SampleAssignment', foreign_keys=[assignment_id], backref=db.backref(
        'delete_requests', lazy='dynamic'
    ))
    requester = db.relationship('User', foreign_keys=[requested_by])
    decider = db.relationship('User', foreign_keys=[decided_by])

    def __repr__(self):
        return f'<DeleteRequest {self.request_type} {self.entity_label} ({self.status})>'


# ---------------------------------------------------------------------------
# Audit Log  (permanent, survives sample deletion)
# ---------------------------------------------------------------------------

class AuditLog(db.Model):
    """Permanent audit trail for destructive actions (e.g. sample deletion).

    Unlike SampleHistory (which is cascade-deleted with its sample), AuditLog
    records are *never* deleted and serve as a tamper-resistant record of who
    did what and when.
    """
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(100), nullable=False)          # e.g. 'BULK_DELETE'
    entity_type = db.Column(db.String(100), nullable=False)     # e.g. 'Sample'
    entity_id = db.Column(db.Integer, nullable=True)            # FK-free – record may be gone
    entity_label = db.Column(db.String(255), nullable=True)     # human-readable, e.g. lab_number
    details = db.Column(db.Text, nullable=True)                 # JSON or free-text snapshot
    performed_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    performed_at = db.Column(db.DateTime, default=jamaica_now)

    # Enhanced audit fields
    human_description = db.Column(db.Text, nullable=True)       # Plain English description
    user_role = db.Column(db.String(50), nullable=True)         # Role at time of action
    user_department = db.Column(db.String(100), nullable=True)  # Branch/section
    report_type = db.Column(db.String(50), nullable=True)       # Branch enum value
    previous_value = db.Column(db.Text, nullable=True)          # Previous value (field changes)
    new_value = db.Column(db.Text, nullable=True)               # New value (field changes)
    previous_stage = db.Column(db.String(50), nullable=True)    # Prior workflow stage
    new_stage = db.Column(db.String(50), nullable=True)         # New workflow stage
    comments = db.Column(db.Text, nullable=True)                # User-entered reason/remarks
    ip_address = db.Column(db.String(45), nullable=True)        # Request IP
    user_agent = db.Column(db.String(500), nullable=True)       # Browser/device info
    success = db.Column(db.Boolean, default=True)               # Whether action succeeded

    performer = db.relationship('User', foreign_keys=[performed_by])

    def __repr__(self):
        return f'<AuditLog {self.action} {self.entity_type}#{self.entity_id}>'


# ---------------------------------------------------------------------------
# Sample Comments  (per-sample discussion thread)
# ---------------------------------------------------------------------------

class SampleComment(db.Model):
    """A comment attached to a specific sample, forming a discussion thread."""
    __tablename__ = 'sample_comments'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    comment_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=jamaica_now)
    updated_at = db.Column(db.DateTime, nullable=True)
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=True
    )
    deleted_at = db.Column(db.DateTime, nullable=True)

    author = db.relationship('User', foreign_keys=[user_id], backref='sample_comments')
    deleter = db.relationship('User', foreign_keys=[deleted_by])

    def __repr__(self):
        return f'<SampleComment {self.id} on Sample {self.sample_id}>'


# ---------------------------------------------------------------------------
# Direct Messages  (in-app messenger)
# ---------------------------------------------------------------------------

class DirectMessage(db.Model):
    """A private message sent from one user to another."""
    __tablename__ = 'direct_messages'

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False, index=True
    )
    recipient_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False, index=True
    )
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    sender = db.relationship(
        'User', foreign_keys=[sender_id],
        backref=db.backref('sent_messages', lazy='dynamic')
    )
    recipient = db.relationship(
        'User', foreign_keys=[recipient_id],
        backref=db.backref('received_messages', lazy='dynamic')
    )

    def __repr__(self):
        return f'<DirectMessage from {self.sender_id} to {self.recipient_id}>'


# ---------------------------------------------------------------------------
# Financial Year Utilities
# ---------------------------------------------------------------------------

# Financial year starts in April
FISCAL_YEAR_START_MONTH = 4


def fiscal_year_for_date(d):
    """Return the financial year (integer) for a given date.
    Financial year runs April 1 – March 31.
    E.g. April 2025 → FY 2025, March 2026 → FY 2025."""
    if isinstance(d, datetime):
        d = d.date()
    if d.month >= FISCAL_YEAR_START_MONTH:
        return d.year
    return d.year - 1


def fiscal_quarter_for_date(d):
    """Return the fiscal quarter (1-4) for a given date.
    Q1: Apr-Jun, Q2: Jul-Sep, Q3: Oct-Dec, Q4: Jan-Mar."""
    if isinstance(d, datetime):
        d = d.date()
    month = d.month
    if 4 <= month <= 6:
        return 1
    elif 7 <= month <= 9:
        return 2
    elif 10 <= month <= 12:
        return 3
    else:  # Jan-Mar
        return 4


def fiscal_quarter_months(quarter):
    """Return (month_start, month_end) for a given fiscal quarter.
    Q1: (4,6), Q2: (7,9), Q3: (10,12), Q4: (1,3)."""
    mapping = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}
    return mapping.get(quarter, (1, 3))


def fiscal_year_date_range(year, quarter=None):
    """Return (start_datetime, end_datetime) for a fiscal year or specific quarter.
    Start times are at midnight (00:00:00).
    End times are at end-of-day (23:59:59) to include all samples certified during that day.
    If quarter is None, returns the full fiscal year range."""
    if quarter:
        month_start, month_end = fiscal_quarter_months(quarter)
        if quarter == 4:
            start = datetime(year + 1, month_start, 1, 0, 0, 0)
            # End of March at end-of-day
            end = datetime(year + 1, 3, 31, 23, 59, 59)
        else:
            start = datetime(year, month_start, 1, 0, 0, 0)
            # Last day of end month at end-of-day
            if month_end == 6:
                end = datetime(year, 6, 30, 23, 59, 59)
            elif month_end == 9:
                end = datetime(year, 9, 30, 23, 59, 59)
            elif month_end == 12:
                end = datetime(year, 12, 31, 23, 59, 59)
            else:
                end = datetime(year, month_end, 31, 23, 59, 59)
        return start, end
    else:
        # Full fiscal year: April 1 of year to March 31 of year+1
        return datetime(year, 4, 1, 0, 0, 0), datetime(year + 1, 3, 31, 23, 59, 59)


# ---------------------------------------------------------------------------
# Invoice  (Feature 9 – full invoicing system)
# ---------------------------------------------------------------------------

# Pharmaceutical test pricing table (Feature 10)
PHARMA_TEST_PRICES = {
    'Acidity': 3500,
    'Alcohol Content': 2500,
    'Assay by HPLC': 4500,
    'Assay by polarimetry': 3500,
    'Assay by Titration': 3500,
    'Assay by UV': 3500,
    'Assay Potentiometric Titration': 3500,
    'Conductivity': 1000,
    'Deliverable Volume': 1000,
    'Density': 1200,
    'Disintegration (Tablets and Capsule)': 1500,
    'Dissolution and HPLC Analysis': 5000,
    'Dissolution UV Analysis': 5000,
    'Dose and Uniformity of Dose of Oral Drops': 1000,
    'Identification by Chemical Reaction': 1000,
    'Identification by HPLC': 1000,
    'Identification by IR': 1000,
    'Identification by Thin Layer Chromatography (TLC)': 1000,
    'Identification by UV': 1000,
    'Loss on Drying': 1200,
    'Minimum Fill': 1000,
    'Neutralizing Capacity by Titration': 3500,
    'Non Volatile matter': 1200,
    'Organic Stabilizer': 1200,
    'pH': 1000,
    'Related Substances by Thin Layer Chromatography': 1000,
    'Residue on Ignition': 2500,
    'Specific Gravity': 1200,
    'Uniformity of Content by HPLC': 6750,
    'Uniformity of Content by UV': 5250,
    'Uniformity of Delivered Doses from Multidose Containers': 1000,
    'Weight Variation (Capsules and Tablets)': 1000,
}


class Invoice(db.Model):
    """Invoice associated with a sample (Feature 9)."""
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    sample_id = db.Column(
        db.Integer, db.ForeignKey('samples.id'), nullable=False
    )
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    created_by = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False
    )
    created_at = db.Column(db.DateTime, default=jamaica_now)
    notes = db.Column(db.Text, nullable=True)

    # Relationships
    sample = db.relationship('Sample', backref=db.backref(
        'invoices', lazy='dynamic', cascade='all, delete-orphan'
    ))
    creator = db.relationship('User', foreign_keys=[created_by])
    items = db.relationship(
        'InvoiceItem', backref='invoice', lazy='dynamic',
        cascade='all, delete-orphan'
    )

    @property
    def grand_total(self):
        """Sum of all line item totals."""
        return sum(item.line_total for item in self.items.all())

    def __repr__(self):
        return f'<Invoice {self.invoice_number} for Sample {self.sample_id}>'


class InvoiceItem(db.Model):
    """A single line item on an invoice (Feature 9)."""
    __tablename__ = 'invoice_items'

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(
        db.Integer, db.ForeignKey('invoices.id'), nullable=False
    )
    test_name = db.Column(db.String(255), nullable=False)
    test_type = db.Column(db.String(100), nullable=True)   # e.g. 'Pharmaceutical'
    unit_cost = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    quantity = db.Column(db.Integer, nullable=False, default=1)

    @property
    def line_total(self):
        return float(self.unit_cost) * self.quantity

    def __repr__(self):
        return f'<InvoiceItem {self.test_name} x{self.quantity} @ {self.unit_cost}>'


# ---------------------------------------------------------------------------
# Dropdown Configuration  (Feature 11 – admin-managed dropdown values)
# ---------------------------------------------------------------------------

class DropdownConfig(db.Model):
    """Admin-configurable dropdown list items.

    *category* groups entries by their use-case (e.g. 'api', 'test_type',
    'invoice_test', …).  *value* is the stored / form value.
    *label* is the human-readable display text (defaults to value).
    *sort_order* controls list order.
    """
    __tablename__ = 'dropdown_configs'

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(100), nullable=False, index=True)
    value = db.Column(db.String(255), nullable=False)
    label = db.Column(db.String(255), nullable=True)
    branch = db.Column(db.String(100), nullable=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=jamaica_now)

    __table_args__ = (
        db.UniqueConstraint('category', 'value', 'branch', name='uq_dropdown_cat_value_branch'),
    )

    @staticmethod
    def choices_for(category, branch=None):
        """Return WTForms-style (value, label) choices for a given category, sorted A-Z.

        If *branch* is provided, only entries matching that branch (or entries
        with no branch set, i.e. NULL) are returned.
        """
        q = DropdownConfig.query.filter_by(category=category, is_active=True)
        if branch:
            q = q.filter(
                db.or_(DropdownConfig.branch == branch, DropdownConfig.branch.is_(None))
            )
        rows = q.order_by(db.func.lower(DropdownConfig.label), DropdownConfig.label).all()
        return [(r.value, r.label or r.value) for r in rows]

    def __repr__(self):
        return f'<DropdownConfig {self.category}:{self.value}>'
